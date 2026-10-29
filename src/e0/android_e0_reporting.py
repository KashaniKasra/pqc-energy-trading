#!/usr/bin/env python3
"""Fail-closed v3 Android E0 scientific aggregation and reporting."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from pathlib import Path

from android_e0 import (
    DEVICE_ID,
    EXPECTED_DEVICE,
    MEASURED_ITERATIONS,
    SCHEME_OPERATIONS,
    SCIENTIFIC_LIMITATIONS,
    EvidenceError,
    atomic_write_exclusive,
    percentile,
    timing_statistics,
)
from android_e0_blocks import (
    BLOCK_ATTEMPT_SCHEMA,
    accepted_block_reporting_evidence,
    validate_condition_aggregation,
    validate_rejected_block,
)
from android_e0_campaign import (
    CAMPAIGN_FILE,
    LOCK_FILE,
    reconstruct_campaign,
    state_snapshot,
    validate_lock_document,
)

ANDROID_V3_RESULT_SCHEMA = (
    "device",
    "scheme",
    "operation",
    "n_iter",
    "median_ms",
    "p95_ms",
    "p99_ms",
    "stddev_ms",
    "freq_mhz_median",
    "freq_mhz_p05",
    "freq_mhz_p95",
    "temp_c_max",
)

FREQUENCY_COMPARISON_SCHEMA = (
    "device",
    "platform",
    "scheme",
    "operation",
    "freq_mhz_median",
    "freq_mhz_p05",
    "freq_mhz_p95",
    "reference_scheme",
    "reference_operation",
    "freq_median_difference_percent",
    "comparison_basis",
)

PLATFORM_ID = "android_12_aarch64"
PERCENTILE_METHOD = "linear interpolation at rank p*(n-1) after ascending sort"
SIGNATURE_OPERATIONS = frozenset({"keygen", "sign", "verify"})


@dataclass(frozen=True)
class AndroidConditionResult:
    scheme: str
    operation: str
    timings_ms: tuple[float, ...]
    frequencies_khz: tuple[int, ...]
    temperatures_c: tuple[float, ...]


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid block manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"block manifest is not an object: {path}")
    return value


def _validate_scientific_identity(manifest: dict[str, object]) -> None:
    if manifest.get("schema") != BLOCK_ATTEMPT_SCHEMA or manifest.get("scientific") is not True:
        raise EvidenceError("reporting requires scientific v3 block evidence")
    if manifest.get("device") != EXPECTED_DEVICE:
        raise EvidenceError("unexpected Android device identity in v3 evidence")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise EvidenceError("missing Android scientific provenance")
    expected_provenance_fields = {
        "git_commit",
        "git_tracked_clean",
        "benchmark_sha256",
        "frequency_sampler_sha256",
        "go_version_output",
        "governor",
        "cpu_max_khz",
        "benchmark_transport",
        "telemetry_transport",
        "adb_serial",
        "scientific_limitations",
    }
    if set(provenance) != expected_provenance_fields:
        raise EvidenceError("invalid Android scientific provenance fields")
    if provenance.get("git_tracked_clean") is not True:
        raise EvidenceError("Android scientific evidence was not collected from a tracked-clean tree")
    commit = provenance.get("git_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise EvidenceError("invalid Android scientific Git commit")
    for name in ("benchmark_sha256", "frequency_sampler_sha256"):
        digest = provenance.get(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise EvidenceError(f"invalid Android scientific {name}")
    if provenance.get("governor") != "schedutil" or provenance.get("cpu_max_khz") != 2_000_000:
        raise EvidenceError("unexpected Android CPU provenance")
    if provenance.get("go_version_output") != "go version go1.27.3 android/arm64":
        raise EvidenceError("unexpected Android Go runtime provenance")
    if provenance.get("benchmark_transport") != "Termux SSH":
        raise EvidenceError("unexpected Android benchmark transport provenance")
    if provenance.get("telemetry_transport") != (
        "ADB; frequency sampled on-device and retrieved after the measured run"
    ):
        raise EvidenceError("unexpected Android telemetry transport provenance")
    if not isinstance(provenance.get("adb_serial"), str) or not provenance["adb_serial"]:
        raise EvidenceError("missing Android ADB device identity")
    if provenance.get("scientific_limitations") != SCIENTIFIC_LIMITATIONS:
        raise EvidenceError("unexpected or missing Android scientific limitations")


def _condition_directory(root: Path, scheme: str, operation: str) -> Path:
    safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", scheme)
    return root / safe_scheme / operation


def load_v3_results(evidence_root: Path) -> list[AndroidConditionResult]:
    root = evidence_root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise EvidenceError("Android v3 evidence root must be a regular directory")
    campaign_files: set[Path] = set()
    if (root / CAMPAIGN_FILE).exists():
        if (root / LOCK_FILE).exists():
            raise EvidenceError("cannot finalize a campaign with an active or stale writer lock")
        campaign_state = reconstruct_campaign(root)
        snapshot_path = root / "campaign_state.json"
        if snapshot_path.is_symlink() or not snapshot_path.is_file():
            raise EvidenceError("missing or unsafe campaign state snapshot")
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError("missing or invalid campaign state snapshot") from exc
        if snapshot != state_snapshot(campaign_state):
            raise EvidenceError("campaign state snapshot disagrees with validated evidence")
        campaign_files.update({root / CAMPAIGN_FILE, snapshot_path})
        recovery = root / "lock_recoveries"
        if recovery.exists():
            if not recovery.is_dir() or recovery.is_symlink():
                raise EvidenceError("unsafe campaign lock recovery directory")
            for path in recovery.iterdir():
                if not path.is_file() or path.is_symlink() or re.fullmatch(r"lock-\d+\.json", path.name) is None:
                    raise EvidenceError("invalid campaign lock recovery evidence")
                try:
                    validate_lock_document(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError) as exc:
                    raise EvidenceError("invalid campaign lock recovery JSON") from exc
                campaign_files.add(path)
    manifest_paths = sorted(root.glob("*/*/block_*/attempt_*/manifest.json"))
    if not manifest_paths:
        raise EvidenceError("no Android v3 block manifests found")

    manifests_by_condition: dict[tuple[str, str], list[tuple[Path, dict[str, object]]]] = {}
    expected_files: set[Path] = set(campaign_files)
    common_provenance: dict[str, object] | None = None
    for path in manifest_paths:
        manifest = _load_manifest(path)
        _validate_scientific_identity(manifest)
        provenance = manifest["provenance"]
        if common_provenance is None:
            common_provenance = provenance
        elif provenance != common_provenance:
            raise EvidenceError("Android v3 blocks have inconsistent scientific provenance")
        condition = (manifest.get("scheme"), manifest.get("operation"))
        if condition not in SCHEME_OPERATIONS:
            raise EvidenceError(f"unknown Android v3 condition at {path}")
        expected_files.add(path)
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise EvidenceError(f"missing v3 file receipts at {path}")
        for receipt in files.values():
            if not isinstance(receipt, dict) or not isinstance(receipt.get("path"), str):
                raise EvidenceError(f"invalid v3 file receipt at {path}")
            relative = Path(receipt["path"])
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                raise EvidenceError(f"unsafe v3 evidence path at {path}")
            expected_files.add(path.parent / relative)
        manifests_by_condition.setdefault(condition, []).append((path, manifest))

    if set(manifests_by_condition) != set(SCHEME_OPERATIONS):
        missing = sorted(set(SCHEME_OPERATIONS) - set(manifests_by_condition))
        extra = sorted(set(manifests_by_condition) - set(SCHEME_OPERATIONS))
        raise EvidenceError(f"incomplete Android v3 condition set; missing={missing}, extra={extra}")

    results: list[AndroidConditionResult] = []
    for scheme, operation in SCHEME_OPERATIONS:
        condition_dir = _condition_directory(root, scheme, operation)
        aggregated = validate_condition_aggregation(condition_dir)
        if len(aggregated) != MEASURED_ITERATIONS:
            raise EvidenceError(f"{scheme}/{operation} does not contain exactly 1000 accepted timings")
        timings: list[float] = []
        frequencies: list[int] = []
        temperatures: list[float] = []
        accepted_blocks = 0
        for path, manifest in sorted(
            manifests_by_condition[(scheme, operation)],
            key=lambda item: (int(item[1]["block_index"]), int(item[1]["attempt_index"])),
        ):
            if manifest.get("status") == "accepted":
                evidence = accepted_block_reporting_evidence(path.parent, manifest)
                timings.extend(evidence["timings_ms"])
                frequencies.extend(evidence["frequencies_khz"])
                temperatures.extend(evidence["temperatures_c"])
                accepted_blocks += 1
            elif manifest.get("status") == "rejected":
                validate_rejected_block(path.parent, manifest)
            else:
                raise EvidenceError(f"incomplete v3 block status at {path}")
        if timings != aggregated or len(timings) != MEASURED_ITERATIONS:
            raise EvidenceError(f"{scheme}/{operation} accepted timing population is inconsistent")
        if accepted_blocks == 0 or not frequencies or not temperatures:
            raise EvidenceError(f"{scheme}/{operation} lacks accepted frequency or temperature evidence")
        if any(value <= 0 for value in frequencies) or any(not math.isfinite(value) for value in temperatures):
            raise EvidenceError(f"{scheme}/{operation} contains invalid reporting telemetry")
        results.append(
            AndroidConditionResult(
                scheme,
                operation,
                tuple(timings),
                tuple(frequencies),
                tuple(temperatures),
            )
        )

    actual_files = {path for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if any(path.is_symlink() for path in actual_files) or actual_files != expected_files:
        raise EvidenceError("Android v3 evidence contains missing, extra, or symlink files")
    return results


def _frequency_statistics(values_khz: tuple[int, ...]) -> dict[str, float]:
    values_mhz = [value / 1000.0 for value in values_khz]
    return {
        "median": percentile(values_mhz, 0.50),
        "p05": percentile(values_mhz, 0.05),
        "p95": percentile(values_mhz, 0.95),
    }


def render_android_results(results: list[AndroidConditionResult]) -> bytes:
    if [(item.scheme, item.operation) for item in results] != list(SCHEME_OPERATIONS):
        raise EvidenceError("Android reporting results are incomplete or not canonically ordered")
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(ANDROID_V3_RESULT_SCHEMA)
    for result in results:
        if len(result.timings_ms) != MEASURED_ITERATIONS or not result.frequencies_khz or not result.temperatures_c:
            raise EvidenceError("incomplete Android condition statistics population")
        timing = timing_statistics(list(result.timings_ms))
        frequency = _frequency_statistics(result.frequencies_khz)
        writer.writerow(
            [
                DEVICE_ID,
                result.scheme,
                result.operation,
                str(MEASURED_ITERATIONS),
                format(timing["median_ms"], ".9f"),
                format(timing["p95_ms"], ".9f"),
                format(timing["p99_ms"], ".9f"),
                format(timing["stddev_ms"], ".9f"),
                format(frequency["median"], ".9f"),
                format(frequency["p05"], ".9f"),
                format(frequency["p95"], ".9f"),
                format(max(result.temperatures_c), ".9f"),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def render_frequency_comparison(android_results: bytes) -> bytes:
    try:
        reader = csv.DictReader(android_results.decode("utf-8").splitlines())
        if tuple(reader.fieldnames or ()) != ANDROID_V3_RESULT_SCHEMA:
            raise EvidenceError("frequency comparison requires the exact Android result schema")
        rows = list(reader)
    except UnicodeDecodeError as exc:
        raise EvidenceError("Android results are not UTF-8") from exc
    if [(row["scheme"], row["operation"]) for row in rows] != list(SCHEME_OPERATIONS):
        raise EvidenceError("frequency comparison input is incomplete or not canonically ordered")
    references = {
        row["operation"]: row
        for row in rows
        if row["scheme"] == "ECDSA-P-256" and row["operation"] in SIGNATURE_OPERATIONS
    }
    if set(references) != SIGNATURE_OPERATIONS:
        raise EvidenceError("ECDSA signature-operation frequency references are incomplete")

    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(FREQUENCY_COMPARISON_SCHEMA)
    for row in rows:
        if row["scheme"] == "ML-KEM-768":
            reference_scheme = ""
            reference_operation = ""
            difference = ""
            basis = "not_comparable_kem_operation"
        else:
            reference = references[row["operation"]]
            reference_value = Decimal(reference["freq_mhz_median"])
            candidate_value = Decimal(row["freq_mhz_median"])
            if reference_value <= 0:
                raise EvidenceError("frequency comparison reference must be positive")
            reference_scheme = "ECDSA-P-256"
            reference_operation = row["operation"]
            difference = format((candidate_value - reference_value) * Decimal(100) / reference_value, ".9f")
            basis = "same_signature_operation_relative_to_ecdsa_p256"
        writer.writerow(
            [
                row["device"],
                PLATFORM_ID,
                row["scheme"],
                row["operation"],
                row["freq_mhz_median"],
                row["freq_mhz_p05"],
                row["freq_mhz_p95"],
                reference_scheme,
                reference_operation,
                difference,
                basis,
            ]
        )
    return buffer.getvalue().encode("utf-8")


def finalize_android_v3(evidence_root: Path, output: Path, comparison_output: Path) -> tuple[bytes, bytes]:
    if output.resolve() == comparison_output.resolve():
        raise EvidenceError("Android result and comparison outputs must differ")
    if output.exists() or output.is_symlink() or comparison_output.exists() or comparison_output.is_symlink():
        raise FileExistsError("refusing to overwrite an Android reporting output")
    results = load_v3_results(evidence_root)
    result_content = render_android_results(results)
    comparison_content = render_frequency_comparison(result_content)
    atomic_write_exclusive(output, result_content)
    try:
        atomic_write_exclusive(comparison_output, comparison_content)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return result_content, comparison_content
