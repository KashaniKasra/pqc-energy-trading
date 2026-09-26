#!/usr/bin/env python3
"""Audit complete E9 evidence and emit the professor-defined final CSV."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path
from typing import Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.e9.environment import validate_scientific_environment
from src.e9.network import (
    CONFIG_VALUES,
    E9_PER_CHANNEL_RTT_COMPENSATION_MS,
    FINAL_FIELDS,
    HOP_VALUES,
    MESSAGE_BYTES_BY_CONFIG,
    PHASE_MEASURED,
    PHASE_WARMUP,
    RAW_FIELDS,
    RTT_VALUES_MS,
    NetworkCondition,
    PingVerificationRecord,
    SummaryRow,
    expected_end_to_end_rtt_ms,
    parse_ping_samples,
    percentile,
    validate_final_rows,
    validate_ping_gate,
    write_final_csv,
)


CONDITION_SCHEMA = "pqc-energy-trading.e9-condition-evidence.v4"
FROZEN_SCHEMA = "pqc-energy-trading.raw-e9-evidence-manifest.v1"
MEASUREMENT_COMMIT = "0e514a9bc563129912cf1d189024cf988a9091a2"
WARMUP_ITERATIONS = 100
MEASURED_ITERATIONS = 1000
PING_SAMPLES = 20
SOURCE_FILE_COUNT = 180
CONDITION_COUNT = 60
EVIDENCE_NAME = re.compile(
    r"^(manifest|completion|ping)_"
    r"(classical|uniform_mldsa|layer_aware)_"
    r"rtt(5|20|50|100)_hops([1-5])\.(json|csv|txt)$"
)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def condition_stem(config: str, rtt_ms: int, hops: int) -> str:
    return f"{config}_rtt{rtt_ms}_hops{hops}"


def expected_filenames() -> set[str]:
    return {
        f"{role}_{condition_stem(config, rtt_ms, hops)}.{extension}"
        for config in CONFIG_VALUES
        for rtt_ms in RTT_VALUES_MS
        for hops in HOP_VALUES
        for role, extension in (
            ("manifest", "json"),
            ("completion", "csv"),
            ("ping", "txt"),
        )
    }


class EvidenceRoot:
    """Read an exact plain source set or canonical frozen gzip evidence."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.contents: dict[str, bytes] = {}
        frozen_manifest = self.root / "evidence_manifest.json"
        if frozen_manifest.exists():
            self._load_frozen(frozen_manifest)
        else:
            self._load_plain()
        self.require_complete()

    def _load_plain(self) -> None:
        if not self.root.is_dir():
            raise ValueError(f"E9 evidence root is not a directory: {self.root}")
        files = sorted(path for path in self.root.rglob("*") if path.is_file())
        for path in files:
            if EVIDENCE_NAME.fullmatch(path.name) is None:
                raise ValueError(f"unrecognized plain E9 evidence file {path.name}")
            if path.name in self.contents:
                raise ValueError(f"duplicate logical E9 evidence file {path.name}")
            self.contents[path.name] = path.read_bytes()

    def _load_frozen(self, manifest_path: Path) -> None:
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("invalid frozen E9 evidence manifest") from error
        expected = {
            "schema": FROZEN_SCHEMA,
            "experiment": "E9",
            "measurement_commit": MEASUREMENT_COMMIT,
            "condition_count": CONDITION_COUNT,
            "source_file_count": SOURCE_FILE_COUNT,
            "configs": list(CONFIG_VALUES),
            "rtt_ms_per_payment_channel": list(RTT_VALUES_MS),
            "hops_payment_channel_count": list(HOP_VALUES),
            "expected_conditions": [
                {"config": config, "rtt_ms": rtt_ms, "hops": hops}
                for config in CONFIG_VALUES
                for rtt_ms in RTT_VALUES_MS
                for hops in HOP_VALUES
            ],
            "warmup_iterations_per_condition": WARMUP_ITERATIONS,
            "measured_iterations_per_condition": MEASURED_ITERATIONS,
            "ping_samples_per_condition": PING_SAMPLES,
            "raw_sample_rows_total": CONDITION_COUNT
            * (WARMUP_ITERATIONS + MEASURED_ITERATIONS),
            "warmup_rows_total": CONDITION_COUNT * WARMUP_ITERATIONS,
            "measured_rows_total": CONDITION_COUNT * MEASURED_ITERATIONS,
            "failure_count": 0,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"frozen E9 evidence manifest {key} mismatch")
        compression = manifest.get("compression")
        if compression != {
            "command": "LC_ALL=C gzip -9 -n",
            "deterministic_header": True,
            "format": "gzip",
            "level": 9,
            "original_sha256_semantics": (
                "SHA-256 of decompressed/original scientific evidence bytes"
            ),
            "tracked_sha256_semantics": "SHA-256 of tracked gzip container bytes",
        }:
            raise ValueError("frozen E9 evidence compression policy mismatch")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or len(entries) != SOURCE_FILE_COUNT:
            raise ValueError("frozen E9 manifest must contain exactly 180 entries")
        logical_names = [entry.get("original_filename") for entry in entries]
        if logical_names != sorted(logical_names) or len(set(logical_names)) != SOURCE_FILE_COUNT:
            raise ValueError("frozen E9 entries are unsorted or duplicated")
        tracked_names: set[str] = set()
        for entry in entries:
            logical = entry.get("original_filename")
            match = EVIDENCE_NAME.fullmatch(logical) if isinstance(logical, str) else None
            if match is None:
                raise ValueError(f"invalid frozen E9 logical filename {logical!r}")
            role, config, rtt_text, hops_text, _ = match.groups()
            evidence_role = "completion_samples" if role == "completion" else role
            tracked_name = entry.get("tracked_filename")
            expected_tracked = f"raw/e9/{logical}.gz"
            if (
                entry.get("evidence_role") != evidence_role
                or entry.get("config") != config
                or entry.get("rtt_ms") != int(rtt_text)
                or entry.get("hops") != int(hops_text)
                or entry.get("compression") != "gzip"
                or tracked_name != expected_tracked
                or tracked_name in tracked_names
            ):
                raise ValueError(f"frozen E9 classification mismatch for {logical}")
            tracked_path = self.root / Path(str(tracked_name)).name
            try:
                compressed = tracked_path.read_bytes()
            except OSError as error:
                raise ValueError(f"missing tracked gzip for {logical}") from error
            if (
                len(compressed) != entry.get("tracked_size")
                or sha256(compressed) != entry.get("tracked_sha256")
            ):
                raise ValueError(f"tracked gzip identity mismatch for {logical}")
            try:
                original = gzip.decompress(compressed)
            except OSError as error:
                raise ValueError(f"invalid gzip evidence for {logical}") from error
            if (
                len(original) != entry.get("original_size")
                or sha256(original) != entry.get("original_sha256")
            ):
                raise ValueError(f"original evidence identity mismatch for {logical}")
            self.contents[logical] = original
            tracked_names.add(str(tracked_name))
        actual_files = {path.name for path in self.root.iterdir()}
        expected_files = {Path(name).name for name in tracked_names} | {
            "evidence_manifest.json"
        }
        if actual_files != expected_files:
            raise ValueError("raw/e9 contains missing or unmanifested files")

    def require_complete(self) -> None:
        if set(self.contents) != expected_filenames():
            raise ValueError("E9 evidence is not the exact 60-condition Cartesian product")

    def read(self, logical_name: str) -> bytes:
        try:
            return self.contents[logical_name]
        except KeyError as error:
            raise ValueError(f"missing logical E9 evidence {logical_name}") from error


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"missing or invalid {label}")
    return value


def _require_close(actual: object, expected: float, label: str) -> None:
    try:
        value = float(actual)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label}") from error
    if not math.isfinite(value) or not math.isclose(
        value, expected, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(f"{label} mismatch")


def _parse_manifest(evidence: EvidenceRoot, name: str) -> dict[str, object]:
    try:
        value = json.loads(evidence.read(name))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid condition manifest {name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"condition manifest is not an object: {name}")
    return value


def audit_condition(
    evidence: EvidenceRoot,
    config: str,
    rtt_ms: int,
    hops: int,
) -> tuple[SummaryRow, str]:
    """Validate one scientific condition and return its independently computed row."""
    stem = condition_stem(config, rtt_ms, hops)
    manifest = _parse_manifest(evidence, f"manifest_{stem}.json")
    if manifest.get("schema") != CONDITION_SCHEMA:
        raise ValueError(f"condition {stem} must use E9 schema v4")
    if manifest.get("scientific") is not True:
        raise ValueError(f"condition {stem} is not scientific")

    condition_meta = _require_mapping(manifest.get("condition"), "condition provenance")
    expected_condition = {
        "config": config,
        "configured_per_channel_rtt_ms": rtt_ms,
        "per_channel_rtt_compensation_ms": E9_PER_CHANNEL_RTT_COMPENSATION_MS,
        "effective_one_way_tc_delay_ms": (rtt_ms - E9_PER_CHANNEL_RTT_COMPENSATION_MS) / 2.0,
        "hops": hops,
        "expected_end_to_end_rtt_ms": float(rtt_ms * hops),
        "prepared_message_bytes": MESSAGE_BYTES_BY_CONFIG[config],
        "warmup_iterations": WARMUP_ITERATIONS,
        "measured_iterations": MEASURED_ITERATIONS,
    }
    if dict(condition_meta) != expected_condition:
        raise ValueError(f"condition metadata mismatch for {stem}")
    condition = NetworkCondition(rtt_ms, hops)
    if expected_end_to_end_rtt_ms(condition) != float(rtt_ms * hops):
        raise ValueError(f"logical RTT semantics mismatch for {stem}")

    environment = _require_mapping(manifest.get("environment"), "environment provenance")
    software = _require_mapping(environment.get("software"), "software provenance")
    if (
        software.get("git_commit") != MEASUREMENT_COMMIT
        or software.get("git_dirty") is not False
    ):
        raise ValueError(f"Git provenance mismatch for {stem}")
    timing = _require_mapping(environment.get("timing"), "timing environment")
    validate_scientific_environment(timing)
    stable_environment = json.dumps(environment, sort_keys=True, separators=(",", ":"))

    ping_meta = _require_mapping(manifest.get("ping_verification"), "ping provenance")
    ping_name = f"ping_{stem}.txt"
    if ping_meta.get("filename") != ping_name:
        raise ValueError(f"ping filename mismatch for {stem}")
    ping_raw = evidence.read(ping_name)
    if len(ping_raw) != ping_meta.get("bytes") or sha256(ping_raw) != ping_meta.get("sha256"):
        raise ValueError(f"ping raw identity mismatch for {stem}")
    try:
        ping_text = ping_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"ping output is not UTF-8 for {stem}") from error
    ping_samples = parse_ping_samples(ping_text)
    if (
        len(ping_samples) != PING_SAMPLES
        or ping_meta.get("sample_count") != PING_SAMPLES
        or list(ping_samples) != ping_meta.get("samples_ms")
        or any(not math.isfinite(sample) or sample <= 0 for sample in ping_samples)
    ):
        raise ValueError(f"ping sample provenance mismatch for {stem}")
    expected_rtt = float(rtt_ms * hops)
    ping_median = percentile(ping_samples, 0.50)
    ping_deviation = abs(ping_median - expected_rtt) / expected_rtt * 100.0
    _require_close(
        ping_meta.get("measured_end_to_end_rtt_ms"), ping_median, "ping median"
    )
    _require_close(ping_meta.get("deviation_pct"), ping_deviation, "ping deviation")
    if ping_meta.get("pass") is not True or ping_deviation >= 10.0:
        raise ValueError(f"ping failed strict <10% gate for {stem}")
    verification = PingVerificationRecord(
        config=config,
        condition=condition,
        samples_ms=ping_samples,
        expected_end_to_end_rtt_ms=expected_rtt,
        measured_end_to_end_rtt_ms=ping_median,
        deviation_pct=ping_deviation,
        passed=True,
        raw_output=ping_text,
        raw_output_sha256=sha256(ping_raw),
        raw_output_bytes=len(ping_raw),
    )
    validate_ping_gate(config, condition, verification)

    completion_meta = _require_mapping(
        manifest.get("completion_samples"), "completion provenance"
    )
    completion_name = f"completion_{stem}.csv"
    if completion_meta.get("filename") != completion_name:
        raise ValueError(f"completion filename mismatch for {stem}")
    completion_raw = evidence.read(completion_name)
    if (
        len(completion_raw) != completion_meta.get("bytes")
        or sha256(completion_raw) != completion_meta.get("sha256")
    ):
        raise ValueError(f"completion raw identity mismatch for {stem}")
    try:
        reader = csv.DictReader(io.StringIO(completion_raw.decode("utf-8"), newline=""))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError(f"invalid completion CSV for {stem}") from error
    if tuple(reader.fieldnames or ()) != RAW_FIELDS or len(rows) != 1100:
        raise ValueError(f"completion schema/count mismatch for {stem}")
    if completion_meta.get("row_count") != len(rows):
        raise ValueError(f"completion manifest row_count mismatch for {stem}")

    warmup = [row for row in rows if row["phase"] == PHASE_WARMUP]
    measured = [row for row in rows if row["phase"] == PHASE_MEASURED]
    if len(warmup) != WARMUP_ITERATIONS or len(measured) != MEASURED_ITERATIONS:
        raise ValueError(f"completion phase counts mismatch for {stem}")
    for phase_rows, count, label in (
        (warmup, WARMUP_ITERATIONS, "warmup"),
        (measured, MEASURED_ITERATIONS, "measured"),
    ):
        try:
            iterations = sorted(int(row["iteration"]) for row in phase_rows)
        except ValueError as error:
            raise ValueError(f"invalid {label} iteration for {stem}") from error
        if iterations != list(range(count)):
            raise ValueError(f"missing/duplicate {label} iterations for {stem}")
    timings: list[float] = []
    for row in rows:
        if (
            row["config"] != config
            or row["rtt_ms"] != str(rtt_ms)
            or row["hops"] != str(hops)
            or row["scientific"] != "true"
            or row["success"] != "true"
            or row["error"] != ""
            or None in row
        ):
            raise ValueError(f"failed or inconsistent completion row for {stem}")
        try:
            timing = float(row["completion_ms"])
        except ValueError as error:
            raise ValueError(f"invalid completion timing for {stem}") from error
        if not math.isfinite(timing) or timing <= 0:
            raise ValueError(f"non-positive/non-finite completion timing for {stem}")
        if row["phase"] == PHASE_MEASURED:
            timings.append(timing)
    median = percentile(timings, 0.50)
    p95 = percentile(timings, 0.95)
    p99 = percentile(timings, 0.99)
    return (
        SummaryRow(
            config=config,
            rtt_ms=rtt_ms,
            hops=hops,
            completion_median_ms=median,
            completion_p95_ms=p95,
            n_iter=len(timings),
            completion_p99_ms=p99,
            scientific=True,
            ping_verification=verification,
        ),
        stable_environment,
    )


def audit(evidence_root: Path) -> list[SummaryRow]:
    evidence = EvidenceRoot(evidence_root)
    audited = [
        audit_condition(evidence, config, rtt_ms, hops)
        for config in CONFIG_VALUES
        for rtt_ms in RTT_VALUES_MS
        for hops in HOP_VALUES
    ]
    if len({environment for _, environment in audited}) != 1:
        raise ValueError("scientific environment differs across E9 conditions")
    rows = [row for row, _ in audited]
    validate_final_rows(rows)
    return rows


def finalize(evidence_root: Path, output: Path) -> None:
    rows = audit(evidence_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    write_final_csv(buffer, rows)
    content = buffer.getvalue().encode("utf-8")
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    try:
        temporary.write_bytes(content)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    finalize(arguments.evidence_root, arguments.output)


if __name__ == "__main__":
    main()
