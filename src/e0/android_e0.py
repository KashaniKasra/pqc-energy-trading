#!/usr/bin/env python3
"""Validation and deterministic finalization for isolated Android E0 evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ATTEMPT_SCHEMA = "pqc-energy-trading.e0-android-attempt.v1"
ANDROID_RESULT_SCHEMA = (
    "device",
    "scheme",
    "operation",
    "n_iter",
    "median_ms",
    "p95_ms",
    "p99_ms",
    "stddev_ms",
    "freq_mhz_median",
    "temp_c_max",
)
DEVICE_ID = "xiaomi_redmi_note_9_m2003j15sc"
EXPECTED_DEVICE = {
    "manufacturer": "Xiaomi",
    "model_name": "Redmi Note 9",
    "model": "M2003J15SC",
    "soc": "MediaTek Helio G85",
    "android": "12",
    "architecture": "aarch64",
    "selected_cpu": 7,
    "selected_core": "Cortex-A75",
    "selected_cpu_max_mhz": 2000,
    "go": "1.27.3",
    "liboqs": "0.15.0",
    "liboqs_go": "v0.15.0",
    "governor": "schedutil",
    "rooted": False,
}
WARMUP_ITERATIONS = 100
MEASURED_ITERATIONS = 1000
FREQUENCY_INTERVAL_MS = 1
SCIENTIFIC_STABILIZATION_FREQUENCY_INTERVAL_MS = 100
MIN_FREQUENCY_SAMPLES = 10
MAX_SAMPLE_GAP_MULTIPLIER = 3
BASELINE_FRACTION = 0.20
ROLLING_FRACTION = 0.10
DROP_THRESHOLD_PERCENT = 10.0
COOLDOWN_SECONDS = 180
SCIENTIFIC_LIMITATIONS = [
    "Android is a separate supervised device study and is not the Orange Pi/meter result.",
    "The non-rooted phone remains under schedutil; the sustained-frequency gate detects variation but does not prove thermal causation.",
    "temp_c_max is the maximum of CPU HAL observations immediately before and after a run, not a continuously sampled peak.",
]

SCHEME_OPERATIONS = (
    ("ML-KEM-768", "keygen"),
    ("ML-KEM-768", "encaps"),
    ("ML-KEM-768", "decaps"),
    ("ML-DSA-44", "keygen"),
    ("ML-DSA-44", "sign"),
    ("ML-DSA-44", "verify"),
    ("ML-DSA-65", "keygen"),
    ("ML-DSA-65", "sign"),
    ("ML-DSA-65", "verify"),
    ("ML-DSA-87", "keygen"),
    ("ML-DSA-87", "sign"),
    ("ML-DSA-87", "verify"),
    ("Falcon-512", "keygen"),
    ("Falcon-512", "sign"),
    ("Falcon-512", "verify"),
    ("SPHINCS+-SHA2-128s", "keygen"),
    ("SPHINCS+-SHA2-128s", "sign"),
    ("SPHINCS+-SHA2-128s", "verify"),
    ("ECDSA-P-256", "keygen"),
    ("ECDSA-P-256", "sign"),
    ("ECDSA-P-256", "verify"),
)


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class FrequencyAssessment:
    count: int
    median_mhz: float
    reference_mhz: float
    sustained_min_mhz: float
    drop_percent: float
    accepted: bool
    max_gap_ns: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_receipt(path: Path, relative_to: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"evidence file must be a regular non-symlink: {path}")
    relative = path.relative_to(relative_to)
    return {
        "path": relative.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _median(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        raise EvidenceError("median requires at least one value")
    return float(statistics.median(materialized))


def percentile(values: list[float], probability: float) -> float:
    if not values:
        raise EvidenceError("percentile requires values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = probability * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


def parse_phase_events(path: Path) -> tuple[int, int]:
    events: list[dict[str, object]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid phase events: {exc}") from exc
    names = [event.get("event") for event in events]
    if names == ["measured_start", "measured_end"]:
        measured = events
    elif names == ["stabilization_start", "stabilization_ready", "measured_start", "measured_end"]:
        measured = events[2:]
    else:
        raise EvidenceError("invalid phase event sequence")
    try:
        start = int(measured[0]["monotonic_ns"])
        end = int(measured[1]["monotonic_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("invalid phase monotonic timestamp") from exc
    if start <= 0 or end <= start:
        raise EvidenceError("invalid measured-phase interval")
    return start, end


def read_frequency_samples(path: Path) -> list[tuple[int, int]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            legacy = ["monotonic_ns", "freq_khz", "success", "error"]
            scientific = [
                "scheduled_monotonic_ns", "actual_monotonic_ns", "freq_khz",
                "success", "missed", "error", "phase",
            ]
            if reader.fieldnames not in (legacy, scientific):
                raise EvidenceError("invalid frequency CSV schema")
            samples: list[tuple[int, int]] = []
            previous = -1
            for row in reader:
                if None in row.values():
                    raise EvidenceError("missing frequency CSV value")
                if row["success"] != "true" or row["error"] != "" or row.get("missed", "false") != "false":
                    raise EvidenceError("frequency sampler recorded an error")
                timestamp = int(row.get("actual_monotonic_ns", row.get("monotonic_ns", "")))
                frequency = int(row["freq_khz"])
                if timestamp <= previous or frequency <= 0:
                    raise EvidenceError("non-monotonic timestamp or non-positive frequency")
                previous = timestamp
                samples.append((timestamp, frequency))
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"invalid frequency samples: {exc}") from exc
    return samples


def assess_frequency(
    samples: list[tuple[int, int]], measured_start_ns: int, measured_end_ns: int
) -> FrequencyAssessment:
    selected = [(timestamp, value) for timestamp, value in samples if measured_start_ns <= timestamp <= measured_end_ns]
    if len(selected) < MIN_FREQUENCY_SAMPLES:
        raise EvidenceError(
            f"measured phase has {len(selected)} frequency samples; requires {MIN_FREQUENCY_SAMPLES}"
        )
    interval_ns = FREQUENCY_INTERVAL_MS * 1_000_000
    maximum_gap = MAX_SAMPLE_GAP_MULTIPLIER * interval_ns
    if selected[0][0] - measured_start_ns > maximum_gap:
        raise EvidenceError("frequency coverage starts too late")
    if measured_end_ns - selected[-1][0] > maximum_gap:
        raise EvidenceError("frequency coverage ends too early")
    gaps = [right[0] - left[0] for left, right in zip(selected, selected[1:])]
    max_gap = max(gaps, default=0)
    if max_gap > maximum_gap:
        raise EvidenceError("frequency sampling gap exceeds configured limit")

    values_mhz = [value / 1000.0 for _, value in selected]
    baseline_count = max(1, math.ceil(BASELINE_FRACTION * len(values_mhz)))
    window_count = max(3, math.ceil(ROLLING_FRACTION * len(values_mhz)))
    later = values_mhz[baseline_count:]
    if len(later) < window_count:
        raise EvidenceError("insufficient post-baseline frequency samples")
    reference = _median(values_mhz[:baseline_count])
    rolling = [_median(later[index : index + window_count]) for index in range(len(later) - window_count + 1)]
    sustained_minimum = min(rolling)
    drop = 100.0 * max(0.0, reference - sustained_minimum) / reference
    return FrequencyAssessment(
        count=len(selected),
        median_mhz=_median(values_mhz),
        reference_mhz=reference,
        sustained_min_mhz=sustained_minimum,
        drop_percent=drop,
        accepted=drop <= DROP_THRESHOLD_PERCENT,
        max_gap_ns=max_gap,
    )


_VALUE_PATTERN = re.compile(r"mValue\s*[=:]\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
_TYPE_PATTERN = re.compile(r"mType\s*[=:]\s*(\d+)")


def parse_current_hal_cpu_temperatures(text: str) -> list[float]:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "Current temperatures from HAL:")
    except StopIteration as exc:
        raise EvidenceError("Current temperatures from HAL section is missing") from exc
    values: list[float] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.endswith(":") and "Temperature{" not in stripped:
            break
        if "Temperature{" not in stripped:
            continue
        type_match = _TYPE_PATTERN.search(stripped)
        value_match = _VALUE_PATTERN.search(stripped)
        if type_match is None or value_match is None:
            raise EvidenceError("malformed HAL temperature entry")
        # android.os.Temperature.TYPE_CPU is exactly 0. Other types, including
        # battery and cached-status sections, are intentionally excluded.
        if int(type_match.group(1)) != 0:
            continue
        value = float(value_match.group(1))
        if not math.isfinite(value):
            raise EvidenceError("non-finite CPU HAL temperature")
        values.append(value)
    if not values:
        raise EvidenceError("no CPU (mType=0) entry in Current temperatures from HAL")
    return values


def read_timing_samples(path: Path) -> list[float]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["iteration", "elapsed_ms"]:
                raise EvidenceError("invalid timing CSV schema")
            values: list[float] = []
            for expected, row in enumerate(reader, start=1):
                if None in row.values() or int(row["iteration"]) != expected:
                    raise EvidenceError("invalid timing iteration sequence")
                value = float(row["elapsed_ms"])
                if not math.isfinite(value) or value <= 0:
                    raise EvidenceError("timing sample must be finite and positive")
                values.append(value)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"invalid timing samples: {exc}") from exc
    if len(values) != MEASURED_ITERATIONS:
        raise EvidenceError(f"expected {MEASURED_ITERATIONS} timing samples, got {len(values)}")
    return values


def timing_statistics(samples: list[float]) -> dict[str, float]:
    return {
        "median_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
        "p99_ms": percentile(samples, 0.99),
        "stddev_ms": statistics.stdev(samples),
    }


def validate_receipt(attempt_dir: Path, receipt: object) -> Path:
    if not isinstance(receipt, dict) or set(receipt) != {"path", "bytes", "sha256"}:
        raise EvidenceError("invalid evidence file receipt")
    relative = Path(str(receipt["path"]))
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise EvidenceError("unsafe evidence file path")
    path = attempt_dir / relative
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"missing regular evidence file {relative}")
    if path.stat().st_size != receipt["bytes"] or sha256_file(path) != receipt["sha256"]:
        raise EvidenceError(f"evidence file receipt mismatch for {relative}")
    return path


def validate_accepted_attempt(attempt_dir: Path, manifest: dict[str, object]) -> dict[str, object]:
    required = {
        "schema",
        "scientific",
        "status",
        "attempt",
        "scheme",
        "operation",
        "device",
        "methodology",
        "provenance",
        "files",
        "telemetry",
    }
    if set(manifest) != required or manifest["schema"] != ATTEMPT_SCHEMA:
        raise EvidenceError("invalid attempt manifest schema or fields")
    if manifest["scientific"] is not True or manifest["status"] != "accepted":
        raise EvidenceError("finalization requires accepted scientific attempts")
    condition = (manifest["scheme"], manifest["operation"])
    if condition not in SCHEME_OPERATIONS:
        raise EvidenceError("unknown E0 Android condition")
    try:
        path_attempt = int(attempt_dir.name.removeprefix("attempt_"))
    except ValueError as exc:
        raise EvidenceError("invalid attempt directory name") from exc
    safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", str(manifest["scheme"]))
    if (
        manifest["attempt"] != path_attempt
        or attempt_dir.name != f"attempt_{path_attempt:03d}"
        or attempt_dir.parent.name != manifest["operation"]
        or attempt_dir.parent.parent.name != safe_scheme
    ):
        raise EvidenceError("attempt manifest identity does not match its path")
    if manifest["device"] != EXPECTED_DEVICE:
        raise EvidenceError("unexpected Android device provenance")
    provenance = manifest["provenance"]
    if not isinstance(provenance, dict):
        raise EvidenceError("missing Android provenance")
    if provenance.get("git_tracked_clean") is not True:
        raise EvidenceError("Android collection Git tree was not tracked-clean")
    commit = provenance.get("git_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise EvidenceError("invalid Android collection Git commit")
    for key in ("benchmark_sha256", "frequency_sampler_sha256"):
        value = provenance.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise EvidenceError(f"invalid {key}")
    if provenance.get("governor") != "schedutil" or provenance.get("cpu_max_khz") != 2000000:
        raise EvidenceError("unexpected Android CPU provenance")
    if provenance.get("scientific_limitations") != SCIENTIFIC_LIMITATIONS:
        raise EvidenceError("unexpected or missing Android scientific limitations")
    expected_methodology = {
        "warmup_iterations": WARMUP_ITERATIONS,
        "measured_iterations": MEASURED_ITERATIONS,
        "gomaxprocs": 1,
        "cpu_affinity": [7],
        "frequency_interval_ms": FREQUENCY_INTERVAL_MS,
        "frequency_sampler_cpu": 0,
        "minimum_measured_frequency_samples": MIN_FREQUENCY_SAMPLES,
        "maximum_sample_gap_multiplier": MAX_SAMPLE_GAP_MULTIPLIER,
        "loaded_reference": "median of first ceil(20% of measured-phase frequency samples)",
        "sustained_floor": "minimum rolling median; window=max(3,ceil(10% of measured-phase samples)) after baseline",
        "drop_threshold_percent": DROP_THRESHOLD_PERCENT,
        "drop_rejection": "strictly greater than 10%; exactly 10% is accepted",
        "temperature": "maximum CPU mType=0 value observed in Current temperatures from HAL before and after the run; not a continuous peak",
        "cooldown_between_schemes_seconds": COOLDOWN_SECONDS,
        "cooldown_before_rejected_retry_seconds": COOLDOWN_SECONDS,
    }
    if manifest["methodology"] != expected_methodology:
        raise EvidenceError("unexpected Android E0 methodology")
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != {
        "timing_samples",
        "phase_events",
        "frequency_samples",
        "temperature_before",
        "temperature_after",
        "stdout",
        "stderr",
    }:
        raise EvidenceError("unexpected evidence file inventory")
    paths = {role: validate_receipt(attempt_dir, receipt) for role, receipt in files.items()}
    expected_inventory = {Path(receipt["path"]).name for receipt in files.values()} | {"manifest.json"}
    actual_inventory = {path.name for path in attempt_dir.iterdir()}
    if actual_inventory != expected_inventory:
        raise EvidenceError("attempt contains missing or unmanifested files")
    samples = read_timing_samples(paths["timing_samples"])
    start, end = parse_phase_events(paths["phase_events"])
    frequency = assess_frequency(read_frequency_samples(paths["frequency_samples"]), start, end)
    if not frequency.accepted:
        raise EvidenceError("attempt marked accepted despite frequency drop >10%")
    before = parse_current_hal_cpu_temperatures(paths["temperature_before"].read_text(encoding="utf-8"))
    after = parse_current_hal_cpu_temperatures(paths["temperature_after"].read_text(encoding="utf-8"))
    maximum_temperature = max(before + after)
    expected_telemetry = {
        "frequency_sample_count": frequency.count,
        "frequency_mhz_median": format(frequency.median_mhz, ".9f"),
        "frequency_reference_mhz": format(frequency.reference_mhz, ".9f"),
        "frequency_sustained_min_mhz": format(frequency.sustained_min_mhz, ".9f"),
        "frequency_drop_percent": format(frequency.drop_percent, ".9f"),
        "frequency_max_gap_ns": frequency.max_gap_ns,
        "temperature_before_cpu_c": [format(value, ".9f") for value in before],
        "temperature_after_cpu_c": [format(value, ".9f") for value in after],
        "temp_c_max": format(maximum_temperature, ".9f"),
    }
    if manifest["telemetry"] != expected_telemetry:
        raise EvidenceError("manifest telemetry does not match raw evidence")
    return {
        "condition": condition,
        "samples": samples,
        "frequency": frequency,
        "temp_c_max": maximum_temperature,
    }


def atomic_write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to overwrite {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finalize_android(evidence_root: Path, output: Path) -> bytes:
    evidence_root = evidence_root.resolve()
    manifests = sorted(evidence_root.glob("*/*/attempt_*/manifest.json"))
    if not manifests:
        raise EvidenceError("no Android E0 attempt manifests found")
    accepted: dict[tuple[str, str], dict[str, object]] = {}
    accepted_provenance: dict[str, object] | None = None
    expected_files: set[Path] = set()
    for path in manifests:
        expected_files.add(path)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != ATTEMPT_SCHEMA:
            raise EvidenceError(f"unexpected manifest schema at {path}")
        status = manifest.get("status")
        if status == "rejected":
            expected_rejected_fields = {
                "schema",
                "scientific",
                "status",
                "attempt",
                "scheme",
                "operation",
                "device",
                "methodology",
                "provenance",
                "files",
                "telemetry",
                "rejection_reason",
            }
            files = manifest.get("files")
            condition = (manifest.get("scheme"), manifest.get("operation"))
            if (
                set(manifest) != expected_rejected_fields
                or manifest.get("scientific") is not True
                or condition not in SCHEME_OPERATIONS
                or manifest.get("device") != EXPECTED_DEVICE
                or manifest.get("methodology") != methodology_manifest_value()
                or not isinstance(manifest.get("provenance"), dict)
                or not isinstance(files, dict)
                or not isinstance(manifest.get("telemetry"), dict)
                or not isinstance(manifest.get("rejection_reason"), str)
                or not manifest["rejection_reason"]
            ):
                raise EvidenceError(f"invalid rejected-attempt manifest at {path}")
            try:
                attempt_number = int(path.parent.name.removeprefix("attempt_"))
            except ValueError as exc:
                raise EvidenceError(f"invalid rejected-attempt path at {path}") from exc
            safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", str(manifest["scheme"]))
            if (
                manifest["attempt"] != attempt_number
                or path.parent.name != f"attempt_{attempt_number:03d}"
                or path.parent.parent.name != manifest["operation"]
                or path.parent.parent.parent.name != safe_scheme
            ):
                raise EvidenceError(f"rejected-attempt identity mismatch at {path}")
            for receipt in files.values():
                expected_files.add(validate_receipt(path.parent, receipt))
            expected_inventory = {Path(receipt["path"]).name for receipt in files.values()} | {"manifest.json"}
            if {entry.name for entry in path.parent.iterdir()} != expected_inventory:
                raise EvidenceError(f"rejected attempt contains unmanifested files at {path}")
            continue
        if status != "accepted":
            raise EvidenceError(f"incomplete attempt status at {path}")
        result = validate_accepted_attempt(path.parent, manifest)
        if accepted_provenance is None:
            accepted_provenance = manifest["provenance"]
        elif manifest["provenance"] != accepted_provenance:
            raise EvidenceError("accepted attempts have inconsistent provenance")
        for receipt in manifest["files"].values():
            expected_files.add(path.parent / receipt["path"])
        condition = result["condition"]
        if condition in accepted:
            raise EvidenceError(f"duplicate accepted attempt for {condition}")
        accepted[condition] = result
    if set(accepted) != set(SCHEME_OPERATIONS):
        missing = set(SCHEME_OPERATIONS) - set(accepted)
        raise EvidenceError(f"incomplete Android E0 conditions: {sorted(missing)}")
    actual_files = {path for path in evidence_root.rglob("*") if path.is_file() or path.is_symlink()}
    if any(path.is_symlink() for path in actual_files) or actual_files != expected_files:
        raise EvidenceError("Android evidence root contains missing, extra, or symlink files")

    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(ANDROID_RESULT_SCHEMA)
    for condition in SCHEME_OPERATIONS:
        result = accepted[condition]
        samples = result["samples"]
        statistics_values = timing_statistics(samples)
        frequency = result["frequency"]
        writer.writerow(
            [
                DEVICE_ID,
                condition[0],
                condition[1],
                str(len(samples)),
                format(statistics_values["median_ms"], ".9f"),
                format(statistics_values["p95_ms"], ".9f"),
                format(statistics_values["p99_ms"], ".9f"),
                format(statistics_values["stddev_ms"], ".9f"),
                format(frequency.median_mhz, ".9f"),
                format(result["temp_c_max"], ".9f"),
            ]
        )
    content = buffer.getvalue().encode("utf-8")
    atomic_write_exclusive(output, content)
    return content


def methodology_manifest_value() -> dict[str, object]:
    return {
        "warmup_iterations": WARMUP_ITERATIONS,
        "measured_iterations": MEASURED_ITERATIONS,
        "gomaxprocs": 1,
        "cpu_affinity": [7],
        "frequency_interval_ms": FREQUENCY_INTERVAL_MS,
        "frequency_sampler_cpu": 0,
        "minimum_measured_frequency_samples": MIN_FREQUENCY_SAMPLES,
        "maximum_sample_gap_multiplier": MAX_SAMPLE_GAP_MULTIPLIER,
        "loaded_reference": "median of first ceil(20% of measured-phase frequency samples)",
        "sustained_floor": "minimum rolling median; window=max(3,ceil(10% of measured-phase samples)) after baseline",
        "drop_threshold_percent": DROP_THRESHOLD_PERCENT,
        "drop_rejection": "strictly greater than 10%; exactly 10% is accepted",
        "temperature": "maximum CPU mType=0 value observed in Current temperatures from HAL before and after the run; not a continuous peak",
        "cooldown_between_schemes_seconds": COOLDOWN_SECONDS,
        "cooldown_before_rejected_retry_seconds": COOLDOWN_SECONDS,
    }
