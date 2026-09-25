#!/usr/bin/env python3
"""Validate complete E5 evidence and emit the professor-defined CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path


CONFIG = "layer_aware"
STATE_COUNTS = (10, 100, 1000, 10000, 100000)
SCHEMA = "pqc-energy-trading.e5-condition-evidence.v1"
RAW_FIELDS = (
    "iteration",
    "phase",
    "scientific",
    "n_states",
    "blob_bytes",
    "total_bytes",
    "scan_cpu_ms",
    "success",
    "error",
)
FINAL_FIELDS = (
    "config",
    "n_states",
    "blob_bytes_median",
    "total_bytes",
    "scan_cpu_ms",
)
RECORD_LAYOUT = "uint64-big-endian state_id || unchanged E2 penalty blob"
BLOB_SOURCE = "real E2 layer_aware penalty transition"
METHODOLOGY = {
    "definition": "deterministic sequential worst-case identifier scan to final state",
    "nonmatching_payload_treatment": "skip fixed payload by memory offset without parsing",
    "cache_policy": "mmap prefault plus discarded warm-up scans",
    "clock": "Linux CLOCK_PROCESS_CPUTIME_ID user plus system process CPU",
    "timed_region": "identifier scan and matching payload copy only",
    "percentile": "linear interpolation rank p*(n-1)",
}
ARTIFACT_VALIDATION_RECEIPT = {
    "physically_generated": True,
    "fsynced_and_closed": True,
    "total_bytes_measured_with_os_stat": True,
    "sha256_computed": True,
    "exact_byte_size_recorded": True,
    "all_sequential_ids_validated": True,
    "all_payloads_match_canonical_e2_blob": True,
    "record_count_validated": True,
    "no_trailing_bytes": True,
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def percentile(values: list[float], probability: float) -> float:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("percentiles require finite non-negative samples")
    ordered = sorted(values)
    rank = probability * (len(ordered) - 1)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


def discover(root: Path, prefix: str, suffix: str) -> dict[int, Path]:
    found: dict[int, Path] = {}
    for path in root.rglob(f"{prefix}_layer_aware_n*{suffix}"):
        number_text = path.name.removeprefix(f"{prefix}_layer_aware_n").removesuffix(suffix)
        if not number_text.isdigit():
            continue
        number = int(number_text)
        if number in found:
            raise ValueError(f"duplicate {prefix} evidence for n_states={number}")
        found[number] = path
    return found


def validate_artifact(path: Path, n_states: int, blob_bytes: int, blob_sha256: str) -> int:
    record_bytes = 8 + blob_bytes
    expected_total = n_states * record_bytes
    if path.stat().st_size != expected_total:
        raise ValueError(f"artifact size mismatch for n_states={n_states}")
    observed_lengths: list[int] = []
    with path.open("rb") as source:
        for expected_id in range(n_states):
            identifier = source.read(8)
            if len(identifier) != 8 or int.from_bytes(identifier, "big") != expected_id:
                raise ValueError(f"artifact state ID mismatch for n_states={n_states}")
            payload = source.read(blob_bytes)
            if len(payload) != blob_bytes:
                raise ValueError(f"artifact payload truncated for n_states={n_states}")
            if hashlib.sha256(payload).hexdigest() != blob_sha256:
                raise ValueError(f"artifact payload identity mismatch for n_states={n_states}")
            observed_lengths.append(len(payload))
        if source.read(1):
            raise ValueError(f"artifact trailing bytes for n_states={n_states}")
    return int(percentile([float(value) for value in observed_lengths], 0.5))


def validate_scientific_environment(environment: dict[str, object]) -> None:
    if environment.get("git_dirty") is not False or not environment.get("git_commit"):
        raise ValueError("scientific Git provenance is invalid")
    if not environment.get("go_version") or not environment.get("kernel"):
        raise ValueError("scientific software provenance is incomplete")
    timing = environment.get("timing")
    if not isinstance(timing, dict) or not timing.get("cpu_model"):
        raise ValueError("scientific CPU provenance is incomplete")
    if timing.get("process_affinity") != [2, 4, 6, 8, 10, 12, 14]:
        raise ValueError("scientific CPU affinity is invalid")

    def uniform(name: str, required: bool = True) -> str | None:
        record = timing.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"{name} provenance is invalid")
        if record.get("status") == "unavailable" and not required:
            return None
        values = record.get("values_by_policy")
        if record.get("status") != "available" or not isinstance(values, dict) or not values:
            raise ValueError(f"{name} provenance is incomplete")
        unique = set(values.values())
        if len(unique) != 1:
            raise ValueError(f"{name} differs across CPU policies")
        return str(next(iter(unique)))

    uniform("scaling_driver")
    if uniform("governor") != "performance":
        raise ValueError("scientific governor is not performance")
    epp = uniform("energy_performance_preference", required=False)
    if epp is not None and epp != "performance":
        raise ValueError("scientific EPP is not performance")
    if uniform("scaling_min_freq_khz") != uniform("scaling_max_freq_khz"):
        raise ValueError("scientific scaling frequencies are not pinned")
    if timing.get("boost_status") != "available" or timing.get("boost_enabled") is not False:
        raise ValueError("scientific boost state is invalid")
    if timing.get("ac_status") == "available" and timing.get("ac_online") is not True:
        raise ValueError("scientific AC state is offline")
    if timing.get("ac_status") not in ("available", "unavailable"):
        raise ValueError("scientific AC provenance is invalid")


def summarize_condition(
    manifest_path: Path, artifact_root: Path | None
) -> tuple[str, int, int, int, float, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("scientific") is not True:
        raise ValueError(f"non-scientific or invalid manifest {manifest_path}")
    condition = manifest.get("condition", {})
    n_states = condition.get("n_states")
    if (
        condition.get("config") != CONFIG
        or n_states not in STATE_COUNTS
        or condition.get("warmup_iterations") != 100
        or condition.get("measured_iterations") != 1000
        or condition.get("worst_case_target_state") != n_states - 1
    ):
        raise ValueError(f"condition metadata mismatch in {manifest_path}")
    environment = manifest.get("environment", {})
    if not isinstance(environment, dict):
        raise ValueError(f"scientific environment missing in {manifest_path}")
    validate_scientific_environment(environment)

    samples_meta = manifest.get("samples", {})
    if samples_meta.get("filename") != f"samples_layer_aware_n{n_states}.csv":
        raise ValueError(f"sample filename mismatch for n_states={n_states}")
    sample_path = manifest_path.parent / samples_meta.get("filename", "")
    sample_hash, sample_size = sha256_file(sample_path)
    if sample_hash != samples_meta.get("sha256") or sample_size != samples_meta.get("bytes"):
        raise ValueError(f"sample identity mismatch for n_states={n_states}")
    with sample_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != RAW_FIELDS:
            raise ValueError(f"raw schema mismatch for n_states={n_states}")
        rows = list(reader)
    warmup = [row for row in rows if row["phase"] == "warmup"]
    measured = [row for row in rows if row["phase"] == "measured"]
    expected_measured = condition["measured_iterations"]
    if len(warmup) != 100 or len(measured) != expected_measured:
        raise ValueError(f"sample phase count mismatch for n_states={n_states}")
    if (
        samples_meta.get("row_count") != len(rows)
        or samples_meta.get("warmup_count") != len(warmup)
        or samples_meta.get("measured_count") != len(measured)
        or samples_meta.get("failure_count") != 0
    ):
        raise ValueError(f"sample manifest counts mismatch for n_states={n_states}")
    for phase_rows, count in ((warmup, 100), (measured, expected_measured)):
        if sorted(int(row["iteration"]) for row in phase_rows) != list(range(count)):
            raise ValueError(f"missing or duplicate iteration for n_states={n_states}")
    if any(
        row["scientific"] != "true"
        or row["success"] != "true"
        or row["error"] != ""
        or int(row["n_states"]) != n_states
        or not math.isfinite(float(row["scan_cpu_ms"]))
        or float(row["scan_cpu_ms"]) < 0
        for row in rows
    ):
        raise ValueError(f"failed or inconsistent sample for n_states={n_states}")
    total_values = {int(row["total_bytes"]) for row in rows}
    blob_values = [int(row["blob_bytes"]) for row in rows]
    if len(total_values) != 1:
        raise ValueError(f"sample total_bytes values are inconsistent for n_states={n_states}")
    if any(value <= 0 for value in blob_values):
        raise ValueError(f"sample blob_bytes values invalid for n_states={n_states}")

    blob_meta = manifest.get("blob", {})
    blob_bytes = blob_meta.get("bytes")
    if (
        blob_meta.get("source") != BLOB_SOURCE
        or blob_bytes != 5387
        or set(blob_values) != {blob_bytes}
        or not SHA256_PATTERN.fullmatch(str(blob_meta.get("sha256", "")))
    ):
        raise ValueError(f"frozen E2 blob size mismatch for n_states={n_states}")
    artifact_meta = manifest.get("storage_artifact", {})
    artifact_bytes = artifact_meta.get("bytes")
    artifact_sha256 = str(artifact_meta.get("sha256", ""))
    if artifact_meta.get("filename") != f"watchtower_layer_aware_n{n_states}.bin":
        raise ValueError(f"artifact filename mismatch for n_states={n_states}")
    if artifact_meta.get("record_layout") != RECORD_LAYOUT:
        raise ValueError(f"artifact record layout mismatch for n_states={n_states}")
    if artifact_meta.get("validation") != ARTIFACT_VALIDATION_RECEIPT:
        raise ValueError(f"artifact validation receipt mismatch for n_states={n_states}")
    if not isinstance(artifact_bytes, int) or artifact_bytes <= 0:
        raise ValueError(f"artifact measured byte size is invalid for n_states={n_states}")
    if not SHA256_PATTERN.fullmatch(artifact_sha256):
        raise ValueError(f"artifact SHA-256 syntax is invalid for n_states={n_states}")
    if total_values != {artifact_bytes}:
        raise ValueError(f"sample total_bytes differs from measured manifest value for n_states={n_states}")
    if artifact_bytes != n_states * (8 + blob_bytes):
        raise ValueError(f"artifact record-size integrity mismatch for n_states={n_states}")
    observed_blob_median = float(blob_meta.get("observed_blob_bytes_median", math.nan))
    if not math.isfinite(observed_blob_median) or observed_blob_median != blob_bytes:
        raise ValueError(f"manifest blob median mismatch for n_states={n_states}")

    if artifact_root is not None:
        filename = artifact_meta.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError(f"artifact filename missing for n_states={n_states}")
        candidates = list(artifact_root.rglob(filename))
        if len(candidates) != 1:
            raise ValueError(f"missing or duplicate artifact for n_states={n_states}")
        artifact_path = candidates[0]
        artifact_hash, artifact_size = sha256_file(artifact_path)
        if artifact_hash != artifact_sha256 or artifact_size != artifact_bytes:
            raise ValueError(f"artifact identity mismatch for n_states={n_states}")
        deep_blob_median = validate_artifact(
            artifact_path, n_states, blob_bytes, blob_meta["sha256"]
        )
        if deep_blob_median != observed_blob_median:
            raise ValueError(f"deep artifact blob median mismatch for n_states={n_states}")

    timings = [float(row["scan_cpu_ms"]) for row in measured]
    median = percentile(timings, 0.5)
    p95 = percentile(timings, 0.95)
    p99 = percentile(timings, 0.99)
    methodology = manifest.get("scan_methodology", {})
    if not isinstance(methodology, dict) or any(
        methodology.get(key) != expected for key, expected in METHODOLOGY.items()
    ):
        raise ValueError(f"registered scan methodology mismatch for n_states={n_states}")
    for key, calculated in (
        ("measured_median_ms", median),
        ("measured_p95_ms", p95),
        ("measured_p99_ms", p99),
    ):
        if not math.isclose(float(methodology.get(key, math.nan)), calculated, abs_tol=5e-10):
            raise ValueError(f"manifest {key} mismatch for n_states={n_states}")
    stable_environment = json.dumps(environment, sort_keys=True, separators=(",", ":"))
    return CONFIG, n_states, int(observed_blob_median), artifact_bytes, median, stable_environment


def finalize(evidence_root: Path, output: Path, artifact_root: Path | None = None) -> None:
    manifests = discover(evidence_root, "manifest", ".json")
    samples = discover(evidence_root, "samples", ".csv")
    if set(manifests) != set(STATE_COUNTS) or set(samples) != set(STATE_COUNTS):
        raise ValueError("E5 evidence must contain exactly all five scientific datapoints")
    audited = [summarize_condition(manifests[n_states], artifact_root) for n_states in STATE_COUNTS]
    if len({row[5] for row in audited}) != 1:
        raise ValueError("scientific environment differs across E5 datapoints")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination, lineterminator="\n")
        writer.writerow(FINAL_FIELDS)
        for config, n_states, blob_median, total_bytes, scan_median, _ in audited:
            writer.writerow((config, n_states, blob_median, total_bytes, f"{scan_median:.9f}"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    finalize(arguments.evidence_root, arguments.output, arguments.artifact_root)


if __name__ == "__main__":
    main()
