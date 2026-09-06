#!/usr/bin/env python3
"""Validate and summarize retained E1 evidence.

The default output contains per-round candidate timing statistics. SPHINCS+
fixed-profile outcomes are exposed separately with ``--fixed-outcomes`` because
the saturation run is reportable but is not a valid normal-latency data point.

``--working-e1`` emits the exact final E1 schema to stdout, but leaves unresolved
or unmeasured fields empty. It populates only values supported by retained
evidence or the verified Fabric endorsement policy. This mode cannot write an
output file, so it cannot be mistaken for the completed
``data/e1_fabric.csv`` deliverable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys


CONFIGS = {
    "ecdsa": "ECDSA",
    "ml-dsa-44": "ML-DSA-44",
    "ml-dsa-65": "ML-DSA-65",
}
FIXED_CONFIGS = {
    **CONFIGS,
    "sphincs": "SLH-DSA",
}
ROUNDS = ("50-tps", "200-tps")
MINIMUM_SAMPLES = 1_000
PROVENANCE_STATUS = "candidate_historical_preflight_not_captured"
FINAL_CONFIGS = ("ECDSA", "ML-DSA-44", "ML-DSA-65", "SLH-DSA")
FINAL_FIELDS = (
    "config",
    "identity_bytes",
    "endorse_median_ms",
    "endorse_p95_ms",
    "commit_median_ms",
    "tps_sustained",
    "tx_success_rate",
    "tx_error_rate",
    "tx_bytes_mean",
    "endorsements_per_tx",
    "block_bytes_mean",
    "block_utilisation",
)
IDENTITY_PEERS = {
    "peer0.org1.example.com",
    "peer1.org1.example.com",
    "peer0.org2.example.com",
    "peer1.org2.example.com",
}
IDENTITY_FIELDS = [
    "config",
    "run_label",
    "peer",
    "identity_type",
    "identity_path",
    "bytes",
    "sha256",
]
PUBLIC_KEY_DISPERSION_REVIEW_PERCENT = 3.0
SWEEP_DURATION_SECONDS = 60
WINDOW_FRACTION = 0.20
SUCCESS_RATE_MINIMUM = 0.99
THROUGHPUT_FRACTION_MINIMUM = 0.95
LATENCY_P95_RATIO_MAXIMUM = 2.0
E2E_FIELDS = ["start_offset_ms", "latency_ms", "status", "tx_id"]
TRANSACTION_EVIDENCE_FIELDS = [
    "config", "run_label", "benchmark_label", "block_number", "tx_index",
    "channel_header_type", "tx_id", "envelope_bytes", "envelope_sha256",
    "validation_code", "validation_name", "endorsements",
    "block_classification", "accepted_for_tx_mean",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(sorted_samples: list[float], fraction: float) -> float:
    rank = fraction * (len(sorted_samples) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_samples[lower]
    weight = rank - lower
    return sorted_samples[lower] + weight * (
        sorted_samples[upper] - sorted_samples[lower]
    )


def load_samples(path: Path) -> list[float]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["latency_ms"]:
            raise ValueError(f"{path}: expected only a latency_ms column")
        samples = []
        for line_number, row in enumerate(reader, start=2):
            try:
                value = float(row["latency_ms"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: invalid latency") from error
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"{path}:{line_number}: latency must be finite and non-negative"
                )
            samples.append(value)

    if len(samples) < MINIMUM_SAMPLES:
        raise ValueError(
            f"{path}: {len(samples)} samples is below the required {MINIMUM_SAMPLES}"
        )
    return sorted(samples)


def one_matching_file(raw_dir: Path, pattern: str) -> Path:
    matches = sorted(raw_dir.glob(pattern))
    if len(matches) != 1:
        raise ValueError(
            f"{raw_dir}/{pattern}: expected exactly one source file, found {len(matches)}"
        )
    return matches[0]


def load_caliper_results(
    log_path: Path, round_labels: tuple[str, ...] | list[str] = ROUNDS
) -> dict[str, dict[str, str]]:
    rows: dict[str, set[tuple[str, str, str, str]]] = {
        round_label: set() for round_label in round_labels
    }
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    for raw_line in log_path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = ansi_escape.sub("", raw_line).strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 8 or cells[0] not in rows:
            continue
        success, fail, send_rate, throughput = cells[1], cells[2], cells[3], cells[7]
        if not (success.isdigit() and fail.isdigit()):
            continue
        for label, value in (("send rate", send_rate), ("throughput", throughput)):
            try:
                parsed = float(value)
            except ValueError as error:
                raise ValueError(f"{log_path}: invalid {label} value {value!r}") from error
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError(f"{log_path}: invalid {label} value {value!r}")
        rows[cells[0]].add((success, fail, send_rate, throughput))

    result = {}
    for round_label, values in rows.items():
        if len(values) != 1:
            raise ValueError(
                f"{log_path}: expected one consistent Caliper result for {round_label}, "
                f"found {len(values)}"
            )
        success, fail, send_rate, throughput = next(iter(values))
        result[round_label] = {
            "caliper_success": success,
            "caliper_fail": fail,
            "send_rate_tps": send_rate,
            "throughput_tps": throughput,
        }
    return result


def transaction_rates(success_text: str, fail_text: str) -> tuple[str, str]:
    success = int(success_text)
    fail = int(fail_text)
    total = success + fail
    if total == 0:
        raise ValueError("Caliper round contains no completed requests")
    return f"{success / total:.9f}", f"{fail / total:.9f}"


def relative(path: Path, project_root: Path) -> str:
    return str(path.relative_to(project_root))


def one_csv_row(path: Path) -> tuple[list[str], dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        rows = list(reader)
    if len(rows) != 1:
        raise ValueError(f"{path}: expected exactly one data row, found {len(rows)}")
    return reader.fieldnames, rows[0]


def parse_uint(path: Path, field: str, value: str) -> int:
    if not value.isdigit():
        raise ValueError(f"{path}: {field} must be an unsigned integer")
    return int(value)


def validated_ecdsa_block_result(project_root: Path) -> tuple[str, str]:
    metadata_path = project_root / "meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    working = metadata["e1_benchmark"]["block_utilisation"]["working_result"]

    if working["config"] != "ECDSA" or not working["terminal_partial_included"]:
        raise ValueError(
            f"{metadata_path}: ECDSA working result must include the terminal partial "
            "ordinary block"
        )

    summary_path = project_root / working["summary_source"]
    if not summary_path.is_file():
        raise ValueError(f"missing ECDSA block summary: {summary_path}")
    if sha256_file(summary_path) != working["summary_sha256"]:
        raise ValueError(f"{summary_path}: SHA-256 does not match meta.json")

    _, summary = one_csv_row(summary_path)
    if (
        summary["config"] != "ecdsa"
        or summary["run_label"] != "blockutil-300"
        or summary["benchmark_label"] != "blockutil-300-tps"
        or summary["measurement_status"] != "diagnostic_unreviewed"
    ):
        raise ValueError(f"{summary_path}: unexpected ECDSA diagnostic provenance")

    raw_path = project_root / summary["raw_blocks_file"]
    if not raw_path.is_file():
        raise ValueError(f"missing raw ECDSA block evidence: {raw_path}")
    if summary["raw_blocks_file"] != working["raw_blocks_source"]:
        raise ValueError(f"{summary_path}: raw source does not match meta.json")
    raw_hash = sha256_file(raw_path)
    if raw_hash != summary["raw_blocks_sha256"] or raw_hash != working["raw_blocks_sha256"]:
        raise ValueError(f"{raw_path}: SHA-256 provenance mismatch")

    with raw_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        required = {
            "config",
            "run_label",
            "benchmark_label",
            "block_number",
            "block_bytes",
            "transaction_count",
            "header_types",
            "classification",
            "accepted_for_mean",
        }
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise ValueError(f"{raw_path}: unexpected per-block schema")
        block_rows = list(reader)

    if not block_rows:
        raise ValueError(f"{raw_path}: no per-block evidence")

    accepted_bytes: list[int] = []
    excluded_count = 0
    block_numbers: list[int] = []
    for row in block_rows:
        if (
            row["config"] != "ecdsa"
            or row["run_label"] != summary["run_label"]
            or row["benchmark_label"] != summary["benchmark_label"]
        ):
            raise ValueError(f"{raw_path}: mixed block provenance")

        block_number = parse_uint(raw_path, "block_number", row["block_number"])
        block_bytes = parse_uint(raw_path, "block_bytes", row["block_bytes"])
        transaction_count = parse_uint(
            raw_path, "transaction_count", row["transaction_count"]
        )
        header_types = row["header_types"].split(";") if row["header_types"] else []
        if len(header_types) != transaction_count:
            raise ValueError(
                f"{raw_path}: block {block_number} transaction/header count mismatch"
            )

        accepted = row["accepted_for_mean"] == "true"
        if row["accepted_for_mean"] not in {"true", "false"}:
            raise ValueError(f"{raw_path}: block {block_number} has invalid acceptance flag")
        if accepted != (row["classification"] == "ordinary_transaction"):
            raise ValueError(
                f"{raw_path}: block {block_number} acceptance/classification mismatch"
            )
        if accepted:
            if block_number == 0 or any(header_type != "3" for header_type in header_types):
                raise ValueError(
                    f"{raw_path}: accepted block {block_number} is not wholly ordinary"
                )
            accepted_bytes.append(block_bytes)
        else:
            excluded_count += 1
        block_numbers.append(block_number)

    if len(set(block_numbers)) != len(block_numbers):
        raise ValueError(f"{raw_path}: duplicate block numbers")
    if sorted(block_numbers) != list(range(min(block_numbers), max(block_numbers) + 1)):
        raise ValueError(f"{raw_path}: measured block interval is not contiguous")

    expected_start = parse_uint(summary_path, "start_block", summary["start_block"])
    expected_end = parse_uint(summary_path, "end_block", summary["end_block"])
    expected_count = parse_uint(
        summary_path, "ordinary_block_count", summary["ordinary_block_count"]
    )
    expected_excluded = parse_uint(
        summary_path, "excluded_block_count", summary["excluded_block_count"]
    )
    preferred = parse_uint(
        summary_path,
        "effective_preferred_max_bytes",
        summary["effective_preferred_max_bytes"],
    )
    if preferred == 0:
        raise ValueError(f"{summary_path}: PreferredMaxBytes must be positive")
    if (
        min(block_numbers) != expected_start
        or max(block_numbers) != expected_end
        or len(accepted_bytes) != expected_count
        or excluded_count != expected_excluded
    ):
        raise ValueError(f"{summary_path}: per-block counts/range do not reconcile")

    mean_text = f"{sum(accepted_bytes) / len(accepted_bytes):.6f}"
    utilisation_text = f"{(sum(accepted_bytes) / len(accepted_bytes)) / preferred:.9f}"
    if (
        mean_text != summary["block_bytes_mean"]
        or utilisation_text != summary["block_utilisation"]
    ):
        raise ValueError(f"{summary_path}: summary does not regenerate from raw blocks")

    if (
        mean_text != working["block_bytes_mean"]
        or utilisation_text != working["block_utilisation"]
        or preferred != working["effective_preferred_max_bytes"]
        or expected_count != working["ordinary_block_count"]
        or expected_excluded != working["excluded_block_count"]
    ):
        raise ValueError(f"{metadata_path}: working ECDSA values do not match evidence")
    return mean_text, utilisation_text


def validate_supporting_signcert_evidence(project_root: Path) -> None:
    metadata_path = project_root / "meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    measurements = metadata["e1_benchmark"]["data_provenance"][
        "supporting_signcert_evidence"
    ]["measurements"]
    if set(measurements) != set(FINAL_CONFIGS):
        raise ValueError(f"{metadata_path}: identity evidence is not complete")

    for display_config, provenance in measurements.items():
        evidence_path = project_root / provenance["source"]
        if not evidence_path.is_file():
            raise ValueError(f"missing {display_config} identity evidence: {evidence_path}")
        if sha256_file(evidence_path) != provenance["sha256"]:
            raise ValueError(f"{evidence_path}: SHA-256 does not match meta.json")

        with evidence_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != IDENTITY_FIELDS:
                raise ValueError(f"{evidence_path}: unexpected identity schema")
            rows = list(reader)

        if len(rows) != 4 or {row["peer"] for row in rows} != IDENTITY_PEERS:
            raise ValueError(f"{evidence_path}: expected one row for each of four peers")
        for row in rows:
            if (
                row["config"] != provenance["file_config"]
                or row["run_label"] != provenance["run_label"]
                or row["identity_type"] != "msp_signcert_pem"
                or not row["identity_path"]
            ):
                raise ValueError(f"{evidence_path}: inconsistent identity provenance")
            if parse_uint(evidence_path, "bytes", row["bytes"]) == 0:
                raise ValueError(f"{evidence_path}: identity size must be positive")
            if re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None:
                raise ValueError(f"{evidence_path}: invalid certificate SHA-256")

        if "generation_log" in provenance:
            log_path = project_root / provenance["generation_log"]
            if not log_path.is_file():
                raise ValueError(f"missing {display_config} identity log: {log_path}")
            if sha256_file(log_path) != provenance["generation_log_sha256"]:
                raise ValueError(f"{log_path}: SHA-256 does not match meta.json")


def validated_public_key_rows(project_root: Path) -> list[dict[str, str]]:
    metadata_path = project_root / "meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    public_keys = metadata["e1_benchmark"]["identity_bytes"]
    measurements = public_keys["measurements"]
    if public_keys["definition"] != "public key bytes only":
        raise ValueError(f"{metadata_path}: identity_bytes definition is not public-key only")
    if set(measurements) != set(FINAL_CONFIGS):
        raise ValueError(f"{metadata_path}: public-key evidence is incomplete")

    rows = []
    for config in FINAL_CONFIGS:
        measurement = measurements[config]
        values = measurement["per_peer_bytes"]
        if len(values) != 4 or any(not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError(f"{metadata_path}: {config} needs four positive key sizes")
        mean = sum(values) / len(values)
        minimum = min(values)
        maximum = max(values)
        dispersion = 100 * (maximum - minimum) / mean
        expected = (
            f"{mean:.6f}", minimum, maximum, f"{dispersion:.6f}"
        )
        observed = (
            measurement["mean_bytes"],
            measurement["min_bytes"],
            measurement["max_bytes"],
            measurement["dispersion_percent"],
        )
        if observed != expected:
            raise ValueError(f"{metadata_path}: {config} public-key summary does not regenerate")

        if "generation_log" in measurement:
            log_path = project_root / measurement["generation_log"]
            if not log_path.is_file() or sha256_file(log_path) != measurement["generation_log_sha256"]:
                raise ValueError(f"{config}: public-key generation-log provenance mismatch")
            matches = [
                int(value)
                for value in re.findall(r"public_key_bytes=(\d+)", log_path.read_text(encoding="utf-8"))
            ]
            if matches != values:
                raise ValueError(f"{log_path}: public-key sizes do not match meta.json")

        rows.append(
            {
                "config": config,
                "identity_bytes_mean": measurement["mean_bytes"],
                "identity_bytes_min": str(minimum),
                "identity_bytes_max": str(maximum),
                "dispersion_percent": measurement["dispersion_percent"],
                "dispersion_flag": (
                    "review_required_over_3_percent"
                    if dispersion > PUBLIC_KEY_DISPERSION_REVIEW_PERCENT
                    else "within_3_percent"
                ),
                "public_key_representation": measurement["representation"],
                "provenance": measurement["provenance"],
            }
        )
    return rows


def validated_endorsement_policy(project_root: Path) -> list[dict[str, str]]:
    configtx_path = project_root / "env" / "fabric" / "e1" / "configtx.yaml"
    setup_path = project_root / "env" / "fabric" / "e1" / "setup_fabric_e1.sh"
    configtx = configtx_path.read_text(encoding="utf-8")
    setup = setup_path.read_text(encoding="utf-8")
    required_fragments = (
        "Application: &ApplicationDefaults",
        'Rule: "MAJORITY Endorsement"',
        'Rule: "OR(\'Org1MSP.peer\')"',
        'Rule: "OR(\'Org2MSP.peer\')"',
        "- *Org1",
        "- *Org2",
    )
    missing = [fragment for fragment in required_fragments if fragment not in configtx]
    if missing:
        raise ValueError(f"{configtx_path}: endorsement-policy evidence missing: {missing}")
    if "--signature-policy" in setup or "--channel-config-policy" in setup:
        raise ValueError(f"{setup_path}: a custom chaincode endorsement policy is present")
    return [{
        "chaincode_policy": "/Channel/Application/Endorsement",
        "policy_type": "ImplicitMeta",
        "rule": "MAJORITY Endorsement",
        "organization_subpolicies": "Org1MSP.peer;Org2MSP.peer",
        "endorsements_per_tx_configured_minimum": "2",
        "configtx_source": relative(configtx_path, project_root),
        "configtx_sha256": sha256_file(configtx_path),
        "setup_source": relative(setup_path, project_root),
        "setup_sha256": sha256_file(setup_path),
    }]


def build_working_e1_rows(project_root: Path) -> list[dict[str, str]]:
    """Build an explicitly incomplete E1-schema view from supported evidence."""
    validate_supporting_signcert_evidence(project_root)
    public_key_rows = validated_public_key_rows(project_root)
    fixed_outcomes = build_fixed_outcome_rows(project_root)
    endorsement_policy = validated_endorsement_policy(project_root)[0]
    block_mean, block_utilisation = validated_ecdsa_block_result(project_root)
    rows = [{field: "" for field in FINAL_FIELDS} for _ in FINAL_CONFIGS]
    for row, config in zip(rows, FINAL_CONFIGS):
        row["config"] = config
    for row, key_row in zip(rows, public_key_rows):
        row["identity_bytes"] = key_row["identity_bytes_mean"]
        row["endorsements_per_tx"] = endorsement_policy[
            "endorsements_per_tx_configured_minimum"
        ]
    common_50_outcomes = {
        row["config"]: row for row in fixed_outcomes if row["round_label"] == "50-tps"
    }
    for row in rows:
        outcome = common_50_outcomes[row["config"]]
        row["tx_success_rate"] = outcome["tx_success_rate"]
        row["tx_error_rate"] = outcome["tx_error_rate"]
    rows[0]["block_bytes_mean"] = block_mean
    rows[0]["block_utilisation"] = block_utilisation
    return rows


def build_rows(project_root: Path) -> list[dict[str, str]]:
    raw_dir = project_root / "raw" / "e1"
    rows = []

    for file_label, display_label in CONFIGS.items():
        log_path = raw_dir / f"{file_label}_caliper_run.log"
        if not log_path.is_file():
            raise ValueError(f"missing Caliper log: {log_path}")
        caliper_results = load_caliper_results(log_path)

        for round_label in ROUNDS:
            endorse_path = one_matching_file(
                raw_dir, f"{file_label}_endorse_{round_label}_worker0_*.csv"
            )
            commit_path = one_matching_file(
                raw_dir, f"{file_label}_commit_{round_label}_worker0_*.csv"
            )
            endorse = load_samples(endorse_path)
            commit = load_samples(commit_path)

            if int(caliper_results[round_label]["caliper_fail"]) != 0:
                raise ValueError(
                    f"{log_path}: {round_label} contains failed requests and is not a "
                    "valid candidate timing source"
                )

            tx_success_rate, tx_error_rate = transaction_rates(
                caliper_results[round_label]["caliper_success"],
                caliper_results[round_label]["caliper_fail"],
            )
            rows.append(
                {
                    "config": display_label,
                    "round_label": round_label,
                    **caliper_results[round_label],
                    "tx_success_rate": tx_success_rate,
                    "tx_error_rate": tx_error_rate,
                    "endorse_n": str(len(endorse)),
                    "endorse_median_ms": f"{percentile(endorse, 0.50):.6f}",
                    "endorse_p95_ms": f"{percentile(endorse, 0.95):.6f}",
                    "endorse_p99_ms": f"{percentile(endorse, 0.99):.6f}",
                    "commit_n": str(len(commit)),
                    "commit_median_ms": f"{percentile(commit, 0.50):.6f}",
                    "commit_p95_ms": f"{percentile(commit, 0.95):.6f}",
                    "commit_p99_ms": f"{percentile(commit, 0.99):.6f}",
                    "endorse_source": relative(endorse_path, project_root),
                    "endorse_sha256": sha256_file(endorse_path),
                    "commit_source": relative(commit_path, project_root),
                    "commit_sha256": sha256_file(commit_path),
                    "log_source": relative(log_path, project_root),
                    "log_sha256": sha256_file(log_path),
                    "provenance_status": PROVENANCE_STATUS,
                }
            )
    return rows


def build_fixed_outcome_rows(project_root: Path) -> list[dict[str, str]]:
    raw_dir = project_root / "raw" / "e1"
    rows = []
    for file_label, display_label in FIXED_CONFIGS.items():
        log_path = raw_dir / f"{file_label}_caliper_run.log"
        results = load_caliper_results(log_path)
        log_text = log_path.read_text(encoding="utf-8")
        gateway_limit_observed = "exceeding concurrency limit (500)" in log_text
        if file_label == "sphincs" and not gateway_limit_observed:
            raise ValueError(f"{log_path}: expected retained Gateway saturation evidence")

        for round_label in ROUNDS:
            result = results[round_label]
            tx_success_rate, tx_error_rate = transaction_rates(
                result["caliper_success"], result["caliper_fail"]
            )
            if file_label == "sphincs":
                saturation_status = "saturation_observed_gateway_concurrency_limit"
                latency_candidate = "false"
            else:
                saturation_status = "not_observed_in_retained_run"
                latency_candidate = "true"
            rows.append(
                {
                    "config": display_label,
                    "implementation": (
                        "SPHINCS+-SHA2-128s-simple"
                        if file_label == "sphincs"
                        else display_label
                    ),
                    "round_label": round_label,
                    **result,
                    "tx_success_rate": tx_success_rate,
                    "tx_error_rate": tx_error_rate,
                    "saturation_status": saturation_status,
                    "normal_latency_candidate": latency_candidate,
                    "log_source": relative(log_path, project_root),
                    "log_sha256": sha256_file(log_path),
                }
            )
    return rows


def load_e2e_samples(path: Path) -> list[dict[str, float | str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != E2E_FIELDS:
            raise ValueError(f"{path}: unexpected end-to-end timing schema")
        rows = []
        seen_ids = set()
        for line_number, row in enumerate(reader, start=2):
            try:
                start_offset = float(row["start_offset_ms"])
                latency = float(row["latency_ms"])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: invalid timing") from error
            if not math.isfinite(start_offset) or start_offset < 0 or not math.isfinite(latency) or latency < 0:
                raise ValueError(f"{path}:{line_number}: timing must be finite and non-negative")
            if row["status"] not in {"success", "failure"}:
                raise ValueError(f"{path}:{line_number}: invalid status")
            if not row["tx_id"] or row["tx_id"] in seen_ids:
                raise ValueError(f"{path}:{line_number}: missing or duplicate transaction ID")
            seen_ids.add(row["tx_id"])
            rows.append({
                "start_offset_ms": start_offset,
                "latency_ms": latency,
                "status": row["status"],
            })
    if not rows:
        raise ValueError(f"{path}: no end-to-end samples")
    return rows


def build_sustainability_rows(project_root: Path, run_namespace: str) -> list[dict[str, str]]:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", run_namespace) is None:
        raise ValueError("run namespace contains unsupported characters")
    raw_dir = project_root / "raw" / "e1"
    log_path = raw_dir / f"{run_namespace}_caliper_run.log"
    e2e_paths = sorted(raw_dir.glob(f"{run_namespace}_e2e_sphincs-*-tps_worker0_*.csv"))
    by_round: dict[str, Path] = {}
    for path in e2e_paths:
        match = re.fullmatch(
            rf"{re.escape(run_namespace)}_e2e_(sphincs-(\d+)-tps)_worker0_\d+\.csv",
            path.name,
        )
        if match is None or match.group(1) in by_round:
            raise ValueError(f"{path}: ambiguous sweep timing filename")
        by_round[match.group(1)] = path
    if not by_round:
        raise ValueError(f"{raw_dir}: no end-to-end timing files for {run_namespace}")

    round_labels = sorted(by_round, key=lambda label: int(label.split("-")[1]))
    caliper_results = load_caliper_results(log_path, round_labels)
    rows = []
    for round_label in round_labels:
        offered_rate = int(round_label.split("-")[1])
        result = caliper_results[round_label]
        success = int(result["caliper_success"])
        fail = int(result["caliper_fail"])
        samples = load_e2e_samples(by_round[round_label])
        if len(samples) != success + fail:
            raise ValueError(
                f"{by_round[round_label]}: sample count does not reconcile with Caliper outcomes"
            )

        beginning_limit = SWEEP_DURATION_SECONDS * 1000 * WINDOW_FRACTION
        end_start = SWEEP_DURATION_SECONDS * 1000 * (1 - WINDOW_FRACTION)
        successful = [sample for sample in samples if sample["status"] == "success"]
        beginning = sorted(
            float(sample["latency_ms"])
            for sample in successful
            if float(sample["start_offset_ms"]) < beginning_limit
        )
        end = sorted(
            float(sample["latency_ms"])
            for sample in successful
            if end_start <= float(sample["start_offset_ms"]) < SWEEP_DURATION_SECONDS * 1000
        )
        if len(beginning) < 5 or len(end) < 5:
            raise ValueError(
                f"{by_round[round_label]}: beginning/end windows each require at least five successful samples"
            )

        beginning_p95 = percentile(beginning, 0.95)
        end_p95 = percentile(end, 0.95)
        ratio = end_p95 / beginning_p95 if beginning_p95 > 0 else math.inf
        tx_success_rate, tx_error_rate = transaction_rates(
            result["caliper_success"], result["caliper_fail"]
        )
        successful_throughput = success / SWEEP_DURATION_SECONDS
        success_pass = float(tx_success_rate) >= SUCCESS_RATE_MINIMUM
        throughput_pass = successful_throughput >= THROUGHPUT_FRACTION_MINIMUM * offered_rate
        latency_pass = ratio <= LATENCY_P95_RATIO_MAXIMUM
        sustainable = success_pass and throughput_pass and latency_pass

        rows.append({
            "round_label": round_label,
            "offered_rate_tps": str(offered_rate),
            "duration_seconds": str(SWEEP_DURATION_SECONDS),
            "caliper_success": str(success),
            "caliper_fail": str(fail),
            "tx_success_rate": tx_success_rate,
            "tx_error_rate": tx_error_rate,
            "successful_throughput_tps": f"{successful_throughput:.6f}",
            "caliper_reported_throughput_tps": result["throughput_tps"],
            "beginning_window_ms": "[0,12000)",
            "ending_window_ms": "[48000,60000)",
            "beginning_success_n": str(len(beginning)),
            "ending_success_n": str(len(end)),
            "beginning_p95_e2e_ms": f"{beginning_p95:.6f}",
            "ending_p95_e2e_ms": f"{end_p95:.6f}",
            "ending_to_beginning_p95_ratio": f"{ratio:.6f}",
            "success_rate_pass": str(success_pass).lower(),
            "throughput_pass": str(throughput_pass).lower(),
            "latency_stability_pass": str(latency_pass).lower(),
            "sustainable": str(sustainable).lower(),
            "e2e_source": relative(by_round[round_label], project_root),
            "e2e_sha256": sha256_file(by_round[round_label]),
            "log_source": relative(log_path, project_root),
            "log_sha256": sha256_file(log_path),
        })

    passing_rates = [int(row["offered_rate_tps"]) for row in rows if row["sustainable"] == "true"]
    highest = str(max(passing_rates)) if passing_rates else ""
    for row in rows:
        row["highest_tested_sustainable_tps"] = highest
    return rows


def validate_transaction_summary(project_root: Path, summary_argument: str) -> list[dict[str, str]]:
    summary_path = Path(summary_argument)
    if not summary_path.is_absolute():
        summary_path = project_root / summary_path
    summary_path = summary_path.resolve()
    if not summary_path.is_relative_to(project_root):
        raise ValueError("transaction summary must be inside the project")
    _, summary = one_csv_row(summary_path)
    required_summary_fields = {
        "ordinary_transaction_count", "valid_transaction_count",
        "invalid_transaction_count", "tx_bytes_mean", "endorsements_per_tx",
        "endorsements_min", "endorsements_max", "raw_transactions_file",
        "raw_transactions_sha256",
    }
    if not required_summary_fields.issubset(summary):
        raise ValueError(f"{summary_path}: summary predates serialized transaction evidence")

    transaction_path = (project_root / summary["raw_transactions_file"]).resolve()
    if not transaction_path.is_relative_to(project_root) or not transaction_path.is_file():
        raise ValueError(f"{summary_path}: invalid transaction evidence path")
    if sha256_file(transaction_path) != summary["raw_transactions_sha256"]:
        raise ValueError(f"{transaction_path}: SHA-256 does not match summary")

    with transaction_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != TRANSACTION_EVIDENCE_FIELDS:
            raise ValueError(f"{transaction_path}: unexpected transaction evidence schema")
        rows = list(reader)

    accepted_bytes = []
    endorsements = []
    valid_count = 0
    invalid_count = 0
    for row in rows:
        if (
            row["config"] != summary["config"]
            or row["run_label"] != summary["run_label"]
            or row["benchmark_label"] != summary["benchmark_label"]
        ):
            raise ValueError(f"{transaction_path}: mixed transaction provenance")
        accepted = row["accepted_for_tx_mean"] == "true"
        if row["accepted_for_tx_mean"] not in {"true", "false"}:
            raise ValueError(f"{transaction_path}: invalid acceptance flag")
        if not accepted:
            continue
        if row["channel_header_type"] != "3" or row["block_classification"] != "ordinary_transaction":
            raise ValueError(f"{transaction_path}: accepted row is not an endorser transaction")
        envelope_bytes = parse_uint(transaction_path, "envelope_bytes", row["envelope_bytes"])
        endorsement_count = parse_uint(transaction_path, "endorsements", row["endorsements"])
        if envelope_bytes == 0 or re.fullmatch(r"[0-9a-f]{64}", row["envelope_sha256"]) is None:
            raise ValueError(f"{transaction_path}: invalid serialized-envelope evidence")
        accepted_bytes.append(envelope_bytes)
        endorsements.append(endorsement_count)
        if row["validation_code"] == "0":
            valid_count += 1
        else:
            invalid_count += 1

    if not accepted_bytes:
        raise ValueError(f"{transaction_path}: no accepted ordinary transactions")
    regenerated = {
        "ordinary_transaction_count": str(len(accepted_bytes)),
        "valid_transaction_count": str(valid_count),
        "invalid_transaction_count": str(invalid_count),
        "tx_bytes_mean": f"{sum(accepted_bytes) / len(accepted_bytes):.6f}",
        "endorsements_per_tx": f"{sum(endorsements) / len(endorsements):.6f}",
        "endorsements_min": str(min(endorsements)),
        "endorsements_max": str(max(endorsements)),
    }
    for field, value in regenerated.items():
        if summary[field] != value:
            raise ValueError(f"{summary_path}: {field} does not regenerate from transactions")
    return [{
        "config": summary["config"],
        "run_label": summary["run_label"],
        "benchmark_label": summary["benchmark_label"],
        **regenerated,
        "raw_transactions_file": summary["raw_transactions_file"],
        "raw_transactions_sha256": summary["raw_transactions_sha256"],
        "summary_source": relative(summary_path, project_root),
        "summary_sha256": sha256_file(summary_path),
    }]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--working-e1",
        action="store_true",
        help=(
            "print the exact final E1 schema with supported working values and "
            "unresolved fields left empty; cannot be written with --output"
        ),
    )
    mode.add_argument(
        "--fixed-outcomes",
        action="store_true",
        help="report fixed-profile success/error rates and saturation status",
    )
    mode.add_argument(
        "--identity-public-keys",
        action="store_true",
        help="validate and report public-key-only identity_bytes evidence",
    )
    mode.add_argument(
        "--endorsement-policy",
        action="store_true",
        help="validate and report the configured chaincode endorsement requirement",
    )
    mode.add_argument(
        "--sustainability",
        metavar="RUN_NAMESPACE",
        help="evaluate a completed SPHINCS+ sweep from namespaced raw evidence",
    )
    mode.add_argument(
        "--block-transactions",
        metavar="SUMMARY_CSV",
        help="validate exact serialized transaction sizes and endorsement counts",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write CSV to this path; stdout is used when omitted",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing --output file explicitly",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    if args.working_e1 and args.output is not None:
        print(
            "ERROR: --working-e1 is intentionally stdout-only while E1 fields remain "
            "unresolved",
            file=sys.stderr,
        )
        return 1
    try:
        if args.working_e1:
            rows = build_working_e1_rows(project_root)
        elif args.fixed_outcomes:
            rows = build_fixed_outcome_rows(project_root)
        elif args.identity_public_keys:
            validate_supporting_signcert_evidence(project_root)
            rows = validated_public_key_rows(project_root)
        elif args.endorsement_policy:
            rows = validated_endorsement_policy(project_root)
        elif args.sustainability:
            rows = build_sustainability_rows(project_root, args.sustainability)
        elif args.block_transactions:
            rows = validate_transaction_summary(project_root, args.block_transactions)
        else:
            rows = build_rows(project_root)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    fieldnames = list(FINAL_FIELDS) if args.working_e1 else list(rows[0])
    if args.output is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return 0

    output_path = args.output
    if not output_path.is_absolute():
        output_path = project_root / output_path
    if output_path.exists() and not args.replace:
        print(f"ERROR: refusing to replace existing output: {output_path}", file=sys.stderr)
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
