#!/usr/bin/env python3
"""Validate and summarize retained E1 evidence.

The default output contains per-round candidate timing statistics. SPHINCS+
fixed-profile outcomes are exposed separately with ``--fixed-outcomes`` because
the saturation run is reportable but is not a valid normal-latency data point.

``--latency-professor-review`` produces the separate 50-TPS and 200-TPS
populations requested for professor review. It never combines rounds and leaves
SPHINCS+ latency fields empty because that fixed-profile run was saturated.

``--working-e1`` emits the exact final E1 schema to stdout, but leaves unresolved
or unmeasured fields empty. It populates only values supported by retained
evidence or the verified Fabric endorsement policy. This mode cannot write an
output file, so it cannot be mistaken for the completed
``data/e1_fabric.csv`` deliverable.

``--pq-verification-audits`` validates the three retained one-shot runtime
trace audits, including trace-line hashes, process/organization coverage, and
clean source/image/patch provenance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
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
LATENCY_REVIEW_FIELDS = (
    "config",
    "offered_tps",
    "total",
    "success",
    "fail",
    "tx_success_rate",
    "tx_error_rate",
    "endorse_sample_count",
    "endorse_median_ms",
    "endorse_p95_ms",
    "endorse_p99_ms",
    "commit_sample_count",
    "commit_median_ms",
    "commit_p95_ms",
    "commit_p99_ms",
    "status",
    "notes",
    "endorse_source",
    "endorse_sha256",
    "commit_source",
    "commit_sha256",
    "log_source",
    "log_sha256",
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
SUSTAINABILITY_PROFILE_SPECS = {
    ("sphincs", ("sphincs-1-tps", "sphincs-2-tps", "sphincs-5-tps", "sphincs-10-tps", "sphincs-20-tps")): {
        "path": "env/caliper/e1/benchmark_sphincs_sweep.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-3-tps", "sphincs-4-tps")): {
        "path": "env/caliper/e1/benchmark_sphincs_refine_3_4.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-35-tps",)): {
        "path": "env/caliper/e1/benchmark_sphincs_boundary_35.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-28-tps",)): {
        "path": "env/caliper/e1/benchmark_sphincs_boundary_28.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-24-tps",)): {
        "path": "env/caliper/e1/benchmark_sphincs_boundary_24.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-22-tps",)): {
        "path": "env/caliper/e1/benchmark_sphincs_boundary_22.yaml",
        "run_type": "sweep",
    },
    ("sphincs", ("sphincs-23-tps",)): {
        "path": "env/caliper/e1/benchmark_sphincs_boundary_23.yaml",
        "run_type": "sweep",
    },
    ("ecdsa", ("sustained-222-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_222.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-223-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_223.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-224-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_224.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-227-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_227.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-228-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_228.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-229-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_229.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-230-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_230.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-237-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_237.yaml",
        "run_type": "sustainability",
    },
    ("ecdsa", ("sustained-250-tps",)): {
        "path": "env/caliper/e1/benchmark_ecdsa_sustained_250.yaml",
        "run_type": "sustainability",
    },
    ("ml-dsa-44", ("sustained-200-tps",)): {
        "path": "env/caliper/e1/benchmark_ml_dsa_sustained_200.yaml",
        "run_type": "sustainability",
    },
    ("ml-dsa-44", ("sustained-250-tps",)): {
        "path": "env/caliper/e1/benchmark_ml_dsa_sustained_250.yaml",
        "run_type": "sustainability",
    },
    ("ml-dsa-44", ("sustained-300-tps",)): {
        "path": "env/caliper/e1/benchmark_ml_dsa_sustained_300.yaml",
        "run_type": "sustainability",
    },
    ("ml-dsa-65", ("sustained-200-tps",)): {
        "path": "env/caliper/e1/benchmark_ml_dsa_sustained_200.yaml",
        "run_type": "sustainability",
    },
}
CLEAN_FIXED_PROFILE_SPEC = {
    "path": "env/caliper/e1/benchmark.yaml",
    "run_type": "fixed-profile",
    "require_image_labels": "true",
}
TRANSACTION_EVIDENCE_FIELDS = [
    "config", "run_label", "benchmark_label", "block_number", "tx_index",
    "channel_header_type", "tx_id", "envelope_bytes", "envelope_sha256",
    "validation_code", "validation_name", "endorsements",
    "block_classification", "accepted_for_tx_mean",
]
PQ_VERIFICATION_AUDITS = {
    "ml-dsa-44": {
        "algorithm": "ML-DSA-44",
        "run_namespace": "pq-verify-v1_ml-dsa-44",
    },
    "ml-dsa-65": {
        "algorithm": "ML-DSA-65",
        "run_namespace": "pq-verify-v1_ml-dsa-65",
    },
    "sphincs": {
        "algorithm": "SPHINCS+-SHA2-128s-simple",
        "run_namespace": "pq-verify-v1_sphincs",
    },
}
PQ_VERIFICATION_FIELDS = [
    "config", "algorithm", "container", "implementation", "function",
    "result", "trace_line_sha256",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_git_file(project_root: Path, commit: str, relative_path: str) -> str:
    """Hash a tracked file exactly as it existed at a recorded run commit."""
    result = subprocess.run(
        ["git", "-C", str(project_root), "show", f"{commit}:{relative_path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"cannot recover {relative_path} at recorded commit {commit}: {detail}"
        )
    return hashlib.sha256(result.stdout).hexdigest()


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


def load_samples(path: Path, minimum_samples: int = MINIMUM_SAMPLES) -> list[float]:
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

    if len(samples) < minimum_samples:
        raise ValueError(
            f"{path}: {len(samples)} samples is below the required {minimum_samples}"
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


def config_from_run_namespace(run_namespace: str) -> str:
    for config in sorted(FIXED_CONFIGS, key=len, reverse=True):
        if run_namespace.endswith(f"_{config}"):
            return config
    raise ValueError(f"{run_namespace}: namespace does not end with a supported config")


def rate_from_round_label(round_label: str) -> int:
    match = re.fullmatch(r"(?:sphincs|sustained)-(\d+)-tps", round_label)
    if match is None:
        raise ValueError(f"unsupported sustainability round label: {round_label}")
    return int(match.group(1))


def configured_sustainability_rate(benchmark_path: Path, round_label: str) -> int:
    """Return an immutable profile's configured fixed rate for one measured round."""
    lines = benchmark_path.read_text(encoding="utf-8").splitlines()
    headers = [
        index for index, line in enumerate(lines)
        if re.fullmatch(r"    - label: .+", line)
    ]
    matching = [
        index for index in headers
        if lines[index] == f"    - label: {round_label}"
    ]
    if len(matching) != 1:
        raise ValueError(
            f"{benchmark_path}: expected exactly one round labelled {round_label}"
        )
    start = matching[0]
    later_headers = [index for index in headers if index > start]
    end = later_headers[0] if later_headers else len(lines)
    block = lines[start:end]
    label_rate = rate_from_round_label(round_label)
    durations = [
        match.group(1)
        for line in block
        if (match := re.fullmatch(r"      txDuration: ([0-9]+)", line))
    ]
    controller_types = [
        match.group(1)
        for line in block
        if (match := re.fullmatch(r"        type: ([a-z-]+)", line))
    ]
    configured_tps = [
        match.group(1)
        for line in block
        if (match := re.fullmatch(r"          tps: ([0-9]+)", line))
    ]
    workload_round_labels = [
        match.group(1)
        for line in block
        if (match := re.fullmatch(r"          roundLabel: (.+)", line))
    ]
    if (
        durations != [str(SWEEP_DURATION_SECONDS)]
        or controller_types != ["fixed-rate"]
        or configured_tps != [str(label_rate)]
        or workload_round_labels != [round_label]
    ):
        raise ValueError(
            f"{benchmark_path}: configured fixed-rate TPS, duration, or roundLabel "
            f"does not match {round_label}"
        )
    return label_rate


def validated_adjacent_integer_boundary(
    passing: dict[str, str], failing: dict[str, str]
) -> str:
    """Return the lower rate only for an adjacent pass/fail integer boundary."""
    if passing["config"] != failing["config"]:
        raise ValueError("sustainability boundary mixes configurations")
    passing_rate = int(passing["offered_tps"])
    failing_rate = int(failing["offered_tps"])
    if failing_rate != passing_rate + 1:
        raise ValueError("sustainability boundary rates are not adjacent integers")
    if passing["sustainable"] != "true" or failing["sustainable"] != "false":
        raise ValueError("sustainability boundary does not have pass/fail ordering")
    if passing["highest_tested_sustainable_tps"] != str(passing_rate):
        raise ValueError("passing boundary row does not identify its sustainable rate")
    return str(passing_rate)


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


def required_prefixed_value(log_path: Path, log_text: str, key: str) -> str:
    match = re.search(
        rf"^\[E1-PQ-VERIFY\] {re.escape(key)}=(.+)$", log_text, re.MULTILINE
    )
    if match is None:
        raise ValueError(f"{log_path}: missing {key}")
    return match.group(1)


def validate_pq_verification_audits(project_root: Path) -> list[dict[str, str]]:
    """Validate retained one-shot liboqs verification traces and provenance."""
    metadata = json.loads((project_root / "meta.json").read_text(encoding="utf-8"))
    software = metadata["software_pins"]
    images = metadata["e1_benchmark"]["installed_images_at_metadata_update"]
    expected_containers = {
        "orderer.example.com",
        "peer0.org1.example.com",
        "peer1.org1.example.com",
        "peer0.org2.example.com",
        "peer1.org2.example.com",
    }
    rows_out = []
    for config, spec in PQ_VERIFICATION_AUDITS.items():
        namespace = spec["run_namespace"]
        csv_path = project_root / "raw" / "e1" / f"{namespace}_pq_verification_audit.csv"
        log_path = project_root / "raw" / "e1" / f"{namespace}_pq_verification_audit.log"
        log_bytes = log_path.read_bytes()
        log_text = log_bytes.decode("utf-8", errors="strict")
        with csv_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != PQ_VERIFICATION_FIELDS:
                raise ValueError(f"{csv_path}: unexpected PQ verification schema")
            trace_rows = list(reader)
        if len(trace_rows) != len(expected_containers):
            raise ValueError(f"{csv_path}: expected five retained traces")
        containers = {row["container"] for row in trace_rows}
        if containers != expected_containers:
            raise ValueError(f"{csv_path}: missing or unexpected containers")

        trace_lines: dict[str, bytes] = {}
        for raw_line in log_bytes.splitlines():
            if b"\t" not in raw_line or b"E1_PQ_VERIFY_TRACE" not in raw_line:
                continue
            container_bytes, trace_line = raw_line.split(b"\t", 1)
            container = container_bytes.decode("utf-8", errors="strict")
            if container in trace_lines:
                raise ValueError(f"{log_path}: duplicate retained trace for {container}")
            trace_lines[container] = trace_line
        if set(trace_lines) != expected_containers:
            raise ValueError(f"{log_path}: retained trace coverage does not match CSV")

        for row in trace_rows:
            trace_line = trace_lines[row["container"]]
            expected_trace = (
                "E1_PQ_VERIFY_TRACE implementation=liboqs "
                f"function=oqs.Signature.Verify algorithm={spec['algorithm']} "
                "result=success"
            ).encode()
            if (
                row["config"] != config
                or row["algorithm"] != spec["algorithm"]
                or row["implementation"] != "liboqs"
                or row["function"] != "oqs.Signature.Verify"
                or row["result"] != "success"
                or expected_trace not in trace_line
                or row["trace_line_sha256"] != hashlib.sha256(trace_line).hexdigest()
            ):
                raise ValueError(f"{csv_path}: invalid or unreconciled trace row")

        required_values = {
            "run_namespace": namespace,
            "configuration": config,
            "algorithm": spec["algorithm"],
            "purpose": "functional_verification_path_audit_not_performance_measurement",
            "controlled_cpu_or_ac_required": "false",
            "project_tracked_state_before_log_creation": "clean",
            "project_tracked_status_sha256_before_log_creation": hashlib.sha256(b"").hexdigest(),
            "fabric_source_commit": software["hyperledger_fabric"]["commit"],
            "liboqs_commit": software["liboqs"]["commit"],
            "fabric_pq_patch_sha256": images["fabric_pq_patch_sha256"],
            "peer_image_id": images["peer"],
            "orderer_image_id": images["orderer"],
            "negative_regression_test": "TestPQVerifierDispatchRejectsClassicalFallback",
            "evidence_csv_sha256": sha256_file(csv_path),
        }
        for key, expected in required_values.items():
            observed = required_prefixed_value(log_path, log_text, key)
            if observed != expected:
                raise ValueError(
                    f"{log_path}: {key}={observed!r}, expected {expected!r}"
                )
        project_commit = required_prefixed_value(log_path, log_text, "project_git_commit")
        if re.fullmatch(r"[0-9a-f]{40}", project_commit) is None:
            raise ValueError(f"{log_path}: invalid project commit")
        for line in (
            "[E1] Removing previous E1 containers and generated artifacts...",
            "[E1] Previous generated state removed.",
            "[E1] Chaincode smoke test passed.",
            "[E1] Fabric E1 setup completed successfully.",
        ):
            if line not in log_text:
                raise ValueError(f"{log_path}: missing fresh functional-audit marker: {line}")

        rows_out.append({
            "config": config,
            "algorithm": spec["algorithm"],
            "run_namespace": namespace,
            "trace_count": str(len(trace_rows)),
            "orderer_success_count": "1",
            "org1_peer_success_count": "2",
            "org2_peer_success_count": "2",
            "implementation": "liboqs",
            "function": "oqs.Signature.Verify",
            "result": "success",
            "classical_fallback_regression": "TestPQVerifierDispatchRejectsClassicalFallback",
            "project_git_commit": project_commit,
            "project_tracked_state": "clean",
            "fabric_commit": software["hyperledger_fabric"]["commit"],
            "liboqs_commit": software["liboqs"]["commit"],
            "fabric_pq_patch_sha256": images["fabric_pq_patch_sha256"],
            "peer_image_id": images["peer"],
            "orderer_image_id": images["orderer"],
            "raw_csv_source": relative(csv_path, project_root),
            "raw_csv_sha256": sha256_file(csv_path),
            "raw_log_source": relative(log_path, project_root),
            "raw_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
            "status": "validated_functional_pq_verify_evidence",
        })
    return rows_out


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
    validated_endorsement_policy(project_root)
    block_mean, block_utilisation = validated_ecdsa_block_result(project_root)
    metadata = json.loads((project_root / "meta.json").read_text(encoding="utf-8"))
    ecdsa_transaction_summary = metadata["e1_benchmark"]["block_utilisation"][
        "ecdsa_transaction_evidence"
    ]["raw_summary"]
    ecdsa_transaction = validate_transaction_summary(
        project_root, ecdsa_transaction_summary
    )[0]
    ml_dsa_44_transaction_summary = metadata["e1_benchmark"]["block_utilisation"][
        "ml_dsa_44_transaction_evidence"
    ]["raw_summary"]
    ml_dsa_44_transaction = validate_transaction_summary(
        project_root, ml_dsa_44_transaction_summary
    )[0]
    ml_dsa_65_transaction_summary = metadata["e1_benchmark"]["block_utilisation"][
        "ml_dsa_65_transaction_evidence"
    ]["raw_summary"]
    ml_dsa_65_transaction = validate_transaction_summary(
        project_root, ml_dsa_65_transaction_summary
    )[0]
    sphincs_transaction_summary = metadata["e1_benchmark"]["caliper"][
        "sphincs_low_rate_sweep"
    ]["transaction_evidence_20_tps"]["raw_summary"]
    sphincs_transaction = validate_transaction_summary(
        project_root, sphincs_transaction_summary
    )[0]
    rows = [{field: "" for field in FINAL_FIELDS} for _ in FINAL_CONFIGS]
    for row, config in zip(rows, FINAL_CONFIGS):
        row["config"] = config
    for row, key_row in zip(rows, public_key_rows):
        row["identity_bytes"] = key_row["identity_bytes_mean"]
    common_50_outcomes = {
        row["config"]: row for row in fixed_outcomes if row["round_label"] == "50-tps"
    }
    for row in rows:
        outcome = common_50_outcomes[row["config"]]
        row["tx_success_rate"] = outcome["tx_success_rate"]
        row["tx_error_rate"] = outcome["tx_error_rate"]
    rows[0]["block_bytes_mean"] = block_mean
    rows[0]["block_utilisation"] = block_utilisation
    rows[0]["tx_bytes_mean"] = ecdsa_transaction["tx_bytes_mean"]
    rows[0]["endorsements_per_tx"] = ecdsa_transaction["endorsements_per_tx"]
    rows[1]["tx_bytes_mean"] = ml_dsa_44_transaction["tx_bytes_mean"]
    rows[1]["endorsements_per_tx"] = ml_dsa_44_transaction["endorsements_per_tx"]
    rows[2]["tx_bytes_mean"] = ml_dsa_65_transaction["tx_bytes_mean"]
    rows[2]["endorsements_per_tx"] = ml_dsa_65_transaction["endorsements_per_tx"]
    rows[3]["tx_bytes_mean"] = sphincs_transaction["tx_bytes_mean"]
    rows[3]["endorsements_per_tx"] = sphincs_transaction["endorsements_per_tx"]
    ecdsa_boundary = metadata["e1_benchmark"]["caliper"][
        "prepared_remaining_profiles"
    ]["ecdsa_integer_boundary"]
    ecdsa_passing = build_sustainability_rows(
        project_root, ecdsa_boundary["highest_sustainable_run_namespace"]
    )[0]
    ecdsa_failing = build_sustainability_rows(
        project_root, ecdsa_boundary["first_failing_run_namespace"]
    )[0]
    ecdsa_sustained = validated_adjacent_integer_boundary(
        ecdsa_passing, ecdsa_failing
    )
    if (
        ecdsa_boundary["status"] != "complete"
        or str(ecdsa_boundary["highest_sustainable_integer_tps"]) != ecdsa_sustained
        or str(ecdsa_boundary["first_failing_integer_tps"])
        != ecdsa_failing["offered_tps"]
        or str(ecdsa_boundary["final_tps_sustained"]) != ecdsa_sustained
    ):
        raise ValueError("ECDSA integer sustained-TPS boundary metadata is inconsistent")
    rows[0]["tps_sustained"] = ecdsa_sustained
    boundary = metadata["e1_benchmark"]["caliper"]["sphincs_low_rate_sweep"]
    if boundary["integer_boundary_search"]["status"] != "complete":
        raise ValueError("SPHINCS+ integer sustained-TPS boundary is not complete")
    rows[3]["tps_sustained"] = str(boundary["highest_tested_sustainable_tps"])
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


def build_latency_professor_review_rows(project_root: Path) -> list[dict[str, str]]:
    """Build separate fixed-profile populations without selecting a final one."""
    raw_dir = project_root / "raw" / "e1"
    timing_candidates = {
        (row["config"], row["round_label"]): row
        for row in build_rows(project_root)
    }
    outcomes = build_fixed_outcome_rows(project_root)
    rows = []

    for outcome in outcomes:
        round_label = outcome["round_label"]
        success = int(outcome["caliper_success"])
        fail = int(outcome["caliper_fail"])
        total = success + fail
        if outcome["normal_latency_candidate"] == "true":
            candidate = timing_candidates[(outcome["config"], round_label)]
            for field in (
                "caliper_success", "caliper_fail", "tx_success_rate", "tx_error_rate"
            ):
                if candidate[field] != outcome[field]:
                    raise ValueError(
                        f"{outcome['config']} {round_label}: timing/outcome {field} mismatch"
                    )
            if int(candidate["endorse_n"]) != success or int(candidate["commit_n"]) != success:
                raise ValueError(
                    f"{outcome['config']} {round_label}: timing counts do not reconcile "
                    "with successful transactions"
                )
            row = {
                "config": outcome["implementation"],
                "offered_tps": outcome["send_rate_tps"],
                "total": str(total),
                "success": str(success),
                "fail": str(fail),
                "tx_success_rate": outcome["tx_success_rate"],
                "tx_error_rate": outcome["tx_error_rate"],
                "endorse_sample_count": candidate["endorse_n"],
                "endorse_median_ms": candidate["endorse_median_ms"],
                "endorse_p95_ms": candidate["endorse_p95_ms"],
                "endorse_p99_ms": candidate["endorse_p99_ms"],
                "commit_sample_count": candidate["commit_n"],
                "commit_median_ms": candidate["commit_median_ms"],
                "commit_p95_ms": candidate["commit_p95_ms"],
                "commit_p99_ms": candidate["commit_p99_ms"],
                "status": "valid_latency_candidate_historical_preflight_not_captured",
                "notes": (
                    "Zero failures and timing counts reconcile; retained historical log "
                    "predates full standardized CPU preflight capture in the run log; "
                    "these rows remain historical support after clean equivalent reruns."
                ),
                "endorse_source": candidate["endorse_source"],
                "endorse_sha256": candidate["endorse_sha256"],
                "commit_source": candidate["commit_source"],
                "commit_sha256": candidate["commit_sha256"],
                "log_source": candidate["log_source"],
                "log_sha256": candidate["log_sha256"],
            }
        else:
            endorse_path = one_matching_file(
                raw_dir, f"sphincs_endorse_{round_label}_worker0_*.csv"
            )
            commit_path = one_matching_file(
                raw_dir, f"sphincs_commit_{round_label}_worker0_*.csv"
            )
            endorse_samples = load_samples(endorse_path, minimum_samples=1)
            commit_samples = load_samples(commit_path, minimum_samples=1)
            if len(commit_samples) != success:
                raise ValueError(
                    f"SPHINCS+ {round_label}: commit samples do not reconcile with successes"
                )
            if not success <= len(endorse_samples) <= total:
                raise ValueError(
                    f"SPHINCS+ {round_label}: endorsement samples are outside the "
                    "successful-to-total request range"
                )
            if fail == 0:
                raise ValueError(f"SPHINCS+ {round_label}: expected retained saturation failures")
            row = {
                "config": outcome["implementation"],
                "offered_tps": outcome["send_rate_tps"],
                "total": str(total),
                "success": str(success),
                "fail": str(fail),
                "tx_success_rate": outcome["tx_success_rate"],
                "tx_error_rate": outcome["tx_error_rate"],
                "endorse_sample_count": str(len(endorse_samples)),
                "endorse_median_ms": "",
                "endorse_p95_ms": "",
                "endorse_p99_ms": "",
                "commit_sample_count": str(len(commit_samples)),
                "commit_median_ms": "",
                "commit_p95_ms": "",
                "commit_p99_ms": "",
                "status": "saturation_only_not_a_valid_latency_candidate",
                "notes": (
                    "Common-profile Gateway concurrency-limit saturation; success/error "
                    "outcome is reportable, but timing fields are intentionally blank. "
                    "SLH-DSA row maps to SPHINCS+-SHA2-128s-simple."
                ),
                "endorse_source": relative(endorse_path, project_root),
                "endorse_sha256": sha256_file(endorse_path),
                "commit_source": relative(commit_path, project_root),
                "commit_sha256": sha256_file(commit_path),
                "log_source": outcome["log_source"],
                "log_sha256": outcome["log_sha256"],
            }
        rows.append(row)

    if len(rows) != 8 or [row["offered_tps"] for row in rows].count("50.0") != 4 or [
        row["offered_tps"] for row in rows
    ].count("200.0") != 4:
        raise ValueError("professor-review table must contain four separate rows per rate")
    return rows


def build_clean_fixed_profile_rows(
    project_root: Path, run_namespace: str
) -> list[dict[str, str]]:
    """Validate one future namespaced common-profile rerun without merging rates."""
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", run_namespace) is None:
        raise ValueError("run namespace contains unsupported characters")
    config = config_from_run_namespace(run_namespace)
    raw_dir = project_root / "raw" / "e1"
    log_path, heights_path, benchmark_path = validate_namespaced_run_provenance(
        project_root,
        run_namespace,
        config,
        list(ROUNDS),
        CLEAN_FIXED_PROFILE_SPEC,
    )
    results = load_caliper_results(log_path, ROUNDS)
    rows = []

    for round_label in ROUNDS:
        result = results[round_label]
        success = int(result["caliper_success"])
        fail = int(result["caliper_fail"])
        total = success + fail
        tx_success_rate, tx_error_rate = transaction_rates(str(success), str(fail))
        endorse_path = one_matching_file(
            raw_dir, f"{run_namespace}_endorse_{round_label}_worker0_*.csv"
        )
        commit_path = one_matching_file(
            raw_dir, f"{run_namespace}_commit_{round_label}_worker0_*.csv"
        )
        e2e_path = one_matching_file(
            raw_dir, f"{run_namespace}_e2e_{round_label}_worker0_*.csv"
        )
        endorse = load_samples(endorse_path, minimum_samples=1)
        commit = load_samples(commit_path, minimum_samples=1)
        e2e = load_e2e_samples(e2e_path)
        if len(e2e) != total:
            raise ValueError(
                f"{e2e_path}: sample count does not reconcile with Caliper outcomes"
            )
        e2e_success = sum(sample["status"] == "success" for sample in e2e)
        if e2e_success != success:
            raise ValueError(
                f"{e2e_path}: success count does not reconcile with Caliper outcomes"
            )
        if not success <= len(endorse) <= total or not success <= len(commit) <= total:
            raise ValueError(
                f"{run_namespace} {round_label}: timing counts fall outside the "
                "successful-to-total request range"
            )

        valid_latency = (
            config != "sphincs"
            and fail == 0
            and success >= MINIMUM_SAMPLES
            and len(endorse) == success
            and len(commit) == success
        )
        if valid_latency:
            status = "clean_equivalent_latency_candidate"
            notes = (
                "Fresh height-7 network and full standardized provenance verified; "
                "50-TPS remains supporting and 200-TPS is professor-selected for final latency."
            )
        elif config == "sphincs" and fail > 0:
            status = "saturation_only_not_a_valid_latency_candidate"
            notes = (
                "Common-profile saturation is reportable; timing fields are intentionally "
                "blank and must not be used as a normal latency population."
            )
        else:
            status = "not_a_valid_normal_latency_candidate"
            notes = (
                "Failures or timing-count reconciliation prevent normal latency use; "
                "retain the outcome and raw evidence for review."
            )

        row = {
            "config": (
                "SPHINCS+-SHA2-128s-simple"
                if config == "sphincs"
                else FIXED_CONFIGS[config]
            ),
            "round_label": round_label,
            "offered_tps": result["send_rate_tps"],
            "total": str(total),
            "success": str(success),
            "fail": str(fail),
            "tx_success_rate": tx_success_rate,
            "tx_error_rate": tx_error_rate,
            "endorse_sample_count": str(len(endorse)),
            "endorse_median_ms": f"{percentile(endorse, 0.50):.6f}" if valid_latency else "",
            "endorse_p95_ms": f"{percentile(endorse, 0.95):.6f}" if valid_latency else "",
            "endorse_p99_ms": f"{percentile(endorse, 0.99):.6f}" if valid_latency else "",
            "commit_sample_count": str(len(commit)),
            "commit_median_ms": f"{percentile(commit, 0.50):.6f}" if valid_latency else "",
            "commit_p95_ms": f"{percentile(commit, 0.95):.6f}" if valid_latency else "",
            "commit_p99_ms": f"{percentile(commit, 0.99):.6f}" if valid_latency else "",
            "status": status,
            "notes": notes,
            "endorse_source": relative(endorse_path, project_root),
            "endorse_sha256": sha256_file(endorse_path),
            "commit_source": relative(commit_path, project_root),
            "commit_sha256": sha256_file(commit_path),
            "e2e_source": relative(e2e_path, project_root),
            "e2e_sha256": sha256_file(e2e_path),
            "heights_source": relative(heights_path, project_root),
            "heights_sha256": sha256_file(heights_path),
            "benchmark_source": relative(benchmark_path, project_root),
            "benchmark_sha256": sha256_file(benchmark_path),
            "log_source": relative(log_path, project_root),
            "log_sha256": sha256_file(log_path),
        }
        rows.append(row)

    return rows


def clean_round_block_count(heights_path: Path, round_label: str) -> int:
    with heights_path.open(newline="", encoding="utf-8") as source:
        rows = {row["marker"]: int(row["height"]) for row in csv.DictReader(source)}
    before = rows[f"before_{round_label}"]
    after = rows[f"after_{round_label}"]
    if after <= before:
        raise ValueError(f"{heights_path}: non-positive block-height delta")
    return after - before


def build_clean_latency_professor_review_rows(
    project_root: Path,
) -> list[dict[str, str]]:
    """Combine validated clean latency, cadence, and transaction evidence."""
    namespaces = (
        "latency-common-clean-v1_ecdsa",
        "latency-common-clean-v1_ml-dsa-44",
        "latency-common-clean-v1_ml-dsa-65",
        "latency-common-clean-v1_sphincs",
    )
    metadata = json.loads((project_root / "meta.json").read_text(encoding="utf-8"))
    block_meta = metadata["e1_benchmark"]["block_utilisation"]
    tx_summaries = {
        "ECDSA": block_meta["ecdsa_transaction_evidence"]["raw_summary"],
        "ML-DSA-44": block_meta["ml_dsa_44_transaction_evidence"]["raw_summary"],
        "ML-DSA-65": block_meta["ml_dsa_65_transaction_evidence"]["raw_summary"],
        "SPHINCS+-SHA2-128s-simple": metadata["e1_benchmark"]["caliper"]
        ["sphincs_low_rate_sweep"]["transaction_evidence_20_tps"]["raw_summary"],
    }
    tx_evidence = {
        config: validate_transaction_summary(project_root, summary)[0]
        for config, summary in tx_summaries.items()
    }
    historical = {
        (row["config"], row["offered_tps"]): row
        for row in build_latency_professor_review_rows(project_root)
    }
    rows_out = []
    for namespace in namespaces:
        for row in build_clean_fixed_profile_rows(project_root, namespace):
            config = row["config"]
            tx = tx_evidence[config]
            round_label = row["round_label"]
            block_count = clean_round_block_count(
                project_root / row["heights_source"], round_label
            )
            if config == "SPHINCS+-SHA2-128s-simple":
                role = "saturation_only"
            elif round_label == "200-tps":
                role = "professor_selected_final_latency_population"
            else:
                role = "supporting_50_tps_population"
            historical_commit = ""
            delta_ms = ""
            delta_percent = ""
            if role == "professor_selected_final_latency_population":
                historical_commit = historical[(config, row["offered_tps"])][
                    "commit_median_ms"
                ]
                delta = float(row["commit_median_ms"]) - float(historical_commit)
                delta_ms = f"{delta:.6f}"
                delta_percent = f"{delta / float(historical_commit) * 100:.6f}"
            rows_out.append({
                "config": config,
                "population_role": role,
                "offered_tps": row["offered_tps"],
                "duration_seconds": "120",
                "total": row["total"],
                "success": row["success"],
                "fail": row["fail"],
                "tx_success_rate": row["tx_success_rate"],
                "tx_error_rate": row["tx_error_rate"],
                "endorse_sample_count": row["endorse_sample_count"],
                "endorse_median_ms": row["endorse_median_ms"],
                "endorse_p95_ms": row["endorse_p95_ms"],
                "endorse_p99_ms": row["endorse_p99_ms"],
                "commit_sample_count": row["commit_sample_count"],
                "commit_median_ms": row["commit_median_ms"],
                "commit_p95_ms": row["commit_p95_ms"],
                "commit_p99_ms": row["commit_p99_ms"],
                "commit_timing_semantic": "post_endorsement_submit_to_commit_status_ms",
                "block_count": str(block_count),
                "mean_height_interval_seconds": f"{120 / block_count:.6f}",
                "tx_bytes_mean": tx["tx_bytes_mean"],
                "tx_bytes_sample_count": tx["ordinary_transaction_count"],
                "endorsements_per_tx": tx["endorsements_per_tx"],
                "historical_200_commit_median_ms": historical_commit,
                "clean_minus_historical_commit_median_ms": delta_ms,
                "clean_minus_historical_commit_median_percent": delta_percent,
                "status": row["status"],
                "endorse_source": row["endorse_source"],
                "endorse_sha256": row["endorse_sha256"],
                "commit_source": row["commit_source"],
                "commit_sha256": row["commit_sha256"],
                "e2e_source": row["e2e_source"],
                "e2e_sha256": row["e2e_sha256"],
                "heights_source": row["heights_source"],
                "heights_sha256": row["heights_sha256"],
                "transaction_source": tx["raw_transactions_file"],
                "transaction_sha256": tx["raw_transactions_sha256"],
                "benchmark_source": row["benchmark_source"],
                "benchmark_sha256": row["benchmark_sha256"],
                "log_source": row["log_source"],
                "log_sha256": row["log_sha256"],
            })
    if len(rows_out) != 8:
        raise ValueError("clean professor review must contain eight separate rows")
    return rows_out


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


def validate_namespaced_run_provenance(
    project_root: Path,
    run_namespace: str,
    config: str,
    round_labels: list[str],
    spec: dict[str, str],
) -> tuple[Path, Path, Path]:
    """Validate a retained log, height markers, and immutable benchmark profile."""
    raw_dir = project_root / "raw" / "e1"
    log_path = raw_dir / f"{run_namespace}_caliper_run.log"
    heights_path = raw_dir / f"{run_namespace}_block_heights.csv"
    benchmark_relative = spec["path"]
    benchmark_path = project_root / benchmark_relative
    metadata = json.loads((project_root / "meta.json").read_text(encoding="utf-8"))
    block = metadata["e1_benchmark"]["fabric_block_parameters"]

    log_text = log_path.read_text(encoding="utf-8", errors="strict")
    patch_match = re.search(
        r"^\[E1\] fabric_pq_patch_sha256=([0-9a-f]{64})$", log_text, re.MULTILINE
    )
    if patch_match is None:
        raise ValueError(f"{log_path}: missing Fabric PQ patch SHA-256")
    image_sets = [metadata["e1_benchmark"]["installed_images_at_metadata_update"]]
    image_sets.extend(metadata["e1_benchmark"].get("superseded_installed_images", []))
    matching_image_sets = [
        image_set
        for image_set in image_sets
        if image_set["fabric_pq_patch_sha256"] == patch_match.group(1)
    ]
    if len(matching_image_sets) != 1:
        raise ValueError(
            f"{log_path}: expected exactly one metadata image set for patch "
            f"{patch_match.group(1)}, found {len(matching_image_sets)}"
        )
    images = matching_image_sets[0]
    required_log_lines = [
        f"[E1] configuration={config}",
        f"[E1] run_type={spec['run_type']}",
        f"[E1] run_namespace={run_namespace}",
        f"[E1] benchmark_config={benchmark_relative}",
        f"[E1] benchmark_config_sha256={sha256_file(benchmark_path)}",
        "[E1] project_tracked_state_before_log_creation=clean",
        "[E1] fixed_cpu_set=2-15",
        "[E1] amd_pstate_mode=passive",
        "[E1] boost_state=0",
        "[E1] fabric_source_tag=v2.5.16",
        "[E1] fabric_source_commit=f871cf92a026aba7b12e6f06d71ded3e6e659d71",
        "[E1] fabric_source_state=clean",
        f"[E1] fabric_peer_image_id={images['peer']}",
        f"[E1] fabric_orderer_image_id={images['orderer']}",
        f"[E1] fabric_pq_patch_sha256={images['fabric_pq_patch_sha256']}",
        f"[E1] effective_BatchTimeout={block['BatchTimeout']}",
        f"[E1] effective_MaxMessageCount={block['MaxMessageCount']}",
        f"[E1] effective_PreferredMaxBytes={block['PreferredMaxBytes_effective_bytes']}",
        f"[E1] effective_AbsoluteMaxBytes={block['AbsoluteMaxBytes_effective_bytes']}",
        "[E1] Benchmark finished.",
    ]
    if spec.get("require_image_labels") == "true":
        required_log_lines.extend([
            f"[E1] fabric_image_label_fabric_commit={metadata['software_pins']['hyperledger_fabric']['commit']}",
            f"[E1] fabric_image_label_liboqs_commit={metadata['software_pins']['liboqs']['commit']}",
            f"[E1] fabric_image_label_patch_sha256={images['fabric_pq_patch_sha256']}",
            "[E1] pq_verify_trace=0",
            "[E1] Removing previous E1 containers and generated artifacts...",
            "[E1] Previous generated state removed.",
            f"[E1] Fabric network ready for configuration: {config}",
        ])
    for cpu in range(2, 16):
        required_log_lines.append(
            f"[E1] cpu{cpu}_state=governor:performance,min_khz:3201000,max_khz:3201000"
        )
    missing = [line for line in required_log_lines if line not in log_text]
    commit_match = re.search(
        r"^\[E1\] project_git_commit=([0-9a-f]{40})$", log_text, re.MULTILINE
    )
    if commit_match is None:
        raise ValueError(f"{log_path}: missing full project commit")
    recorded_commit = commit_match.group(1)
    scientific_sources = {
        "configtx_sha256": "env/fabric/e1/configtx.yaml",
        "fabric_setup_sha256": "env/fabric/e1/setup_fabric_e1.sh",
        "chaincode_sha256": "src/e1/chaincode/chaincode.go",
        "caliper_workload_sha256": "env/caliper/e1/workload/set.js",
        "caliper_timing_patch_sha256": (
            "env/caliper/e1/patches/peer-gateway-e1-timing.patch"
        ),
    }
    for log_key, source_relative in scientific_sources.items():
        if (project_root / ".git").is_dir():
            source_hash = sha256_git_file(
                project_root, recorded_commit, source_relative
            )
        else:
            source_hash = sha256_file(project_root / source_relative)
        required_source_line = f"[E1] {log_key}={source_hash}"
        if required_source_line not in log_text:
            missing.append(required_source_line)
    if spec["run_type"] == "sustainability":
        policy_relative = "env/caliper/e1/run_policy.sh"
        policy_sha256 = sha256_git_file(
            project_root, recorded_commit, policy_relative
        )
        required_policy_line = f"[E1] e1_run_policy_sha256={policy_sha256}"
        if required_policy_line not in log_text:
            missing.append(required_policy_line)
    if missing:
        raise ValueError(f"{log_path}: missing required provenance: {missing}")
    if re.search(r"^\[E1\] started_at=.+$", log_text, re.MULTILINE) is None or re.search(
        r"^\[E1\] finished_at=.+$", log_text, re.MULTILINE
    ) is None:
        raise ValueError(f"{log_path}: missing start/finish timestamps")

    with heights_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["marker", "height"]:
            raise ValueError(f"{heights_path}: unexpected height-marker schema")
        height_rows = list(reader)
    expected_labels = ["warmup", *round_labels]
    expected_markers = [
        marker
        for label in expected_labels
        for marker in (f"before_{label}", f"after_{label}")
    ]
    if [row["marker"] for row in height_rows] != expected_markers:
        raise ValueError(f"{heights_path}: unexpected height-marker sequence")
    heights = [parse_uint(heights_path, "height", row["height"]) for row in height_rows]
    if spec.get("require_image_labels") == "true" and heights[0] != 7:
        raise ValueError(
            f"{heights_path}: clean fixed-profile run must begin at ledger height 7"
        )
    if any(after < before for before, after in zip(heights[::2], heights[1::2])):
        raise ValueError(f"{heights_path}: a round's ending height precedes its start")
    if any(heights[index] != heights[index + 1] for index in range(1, len(heights) - 1, 2)):
        raise ValueError(f"{heights_path}: adjacent round boundaries do not reconcile")
    return log_path, heights_path, benchmark_path


def validate_sweep_provenance(
    project_root: Path,
    run_namespace: str,
    config: str,
    round_labels: list[str],
) -> tuple[Path, Path, Path]:
    spec = SUSTAINABILITY_PROFILE_SPECS.get((config, tuple(round_labels)))
    if spec is None:
        raise ValueError(
            f"unsupported sustainability config/round sequence: "
            f"config={config}, rounds={round_labels}"
        )
    return validate_namespaced_run_provenance(
        project_root, run_namespace, config, round_labels, spec
    )


def build_sustainability_rows(
    project_root: Path,
    run_namespace: str,
    validate_all_raw: bool = True,
) -> list[dict[str, str]]:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", run_namespace) is None:
        raise ValueError("run namespace contains unsupported characters")
    config = config_from_run_namespace(run_namespace)
    raw_dir = project_root / "raw" / "e1"
    log_path = raw_dir / f"{run_namespace}_caliper_run.log"
    e2e_paths = sorted(raw_dir.glob(f"{run_namespace}_e2e_*-tps_worker0_*.csv"))
    by_round: dict[str, Path] = {}
    for path in e2e_paths:
        match = re.fullmatch(
            rf"{re.escape(run_namespace)}_e2e_((?:sphincs|sustained)-(\d+)-tps)_worker0_\d+\.csv",
            path.name,
        )
        if match is None or match.group(1) in by_round:
            raise ValueError(f"{path}: ambiguous sweep timing filename")
        by_round[match.group(1)] = path
    if not by_round:
        raise ValueError(f"{raw_dir}: no end-to-end timing files for {run_namespace}")

    round_labels = sorted(by_round, key=rate_from_round_label)
    caliper_results = load_caliper_results(log_path, round_labels)
    heights_path: Path | None = None
    benchmark_path: Path | None = None
    if validate_all_raw:
        log_path, heights_path, benchmark_path = validate_sweep_provenance(
            project_root, run_namespace, config, round_labels
        )
        for sample_type in ("endorse", "commit"):
            if list(raw_dir.glob(f"{run_namespace}_{sample_type}_warmup_worker0_*.csv")):
                raise ValueError(f"{raw_dir}: warm-up {sample_type} samples must not be retained")
        if list(raw_dir.glob(f"{run_namespace}_e2e_warmup_worker0_*.csv")):
            raise ValueError(f"{raw_dir}: warm-up end-to-end samples must not be retained")
        configured_rates = {
            round_label: configured_sustainability_rate(
                benchmark_path, round_label
            )
            for round_label in round_labels
        }
    else:
        configured_rates = {
            round_label: rate_from_round_label(round_label)
            for round_label in round_labels
        }
    rows = []
    validated_paths = {log_path, heights_path, *by_round.values()}
    for round_label in round_labels:
        offered_rate = configured_rates[round_label]
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
        successful_latencies = sorted(float(sample["latency_ms"]) for sample in successful)
        if len(successful_latencies) != success:
            raise ValueError(
                f"{by_round[round_label]}: success status count does not reconcile with Caliper"
            )
        endorse_path: Path | None = None
        commit_path: Path | None = None
        endorse_samples: list[float] = []
        commit_samples: list[float] = []
        if validate_all_raw:
            endorse_path = one_matching_file(
                raw_dir, f"{run_namespace}_endorse_{round_label}_worker0_*.csv"
            )
            commit_path = one_matching_file(
                raw_dir, f"{run_namespace}_commit_{round_label}_worker0_*.csv"
            )
            endorse_samples = load_samples(endorse_path, minimum_samples=1)
            commit_samples = load_samples(commit_path, minimum_samples=1)
            validated_paths.update((endorse_path, commit_path))
            if len(commit_samples) != success:
                raise ValueError(
                    f"{round_label}: commit sample count does not reconcile with "
                    "successful transactions"
                )
            if not success <= len(endorse_samples) <= success + fail:
                raise ValueError(
                    f"{round_label}: endorsement sample count is outside the "
                    "successful-to-total request range"
                )
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
        achieved_offered_ratio = successful_throughput / offered_rate
        success_pass = float(tx_success_rate) >= SUCCESS_RATE_MINIMUM
        throughput_pass = successful_throughput >= THROUGHPUT_FRACTION_MINIMUM * offered_rate
        latency_pass = ratio <= LATENCY_P95_RATIO_MAXIMUM
        sustainable = success_pass and throughput_pass and latency_pass

        row = {
            "round_label": round_label,
            "offered_rate_tps": str(offered_rate),
            "duration_seconds": str(SWEEP_DURATION_SECONDS),
            "total": str(success + fail),
            "caliper_success": str(success),
            "caliper_fail": str(fail),
            "tx_success_rate": tx_success_rate,
            "tx_error_rate": tx_error_rate,
            "successful_throughput_tps": f"{successful_throughput:.6f}",
            "achieved_offered_ratio": f"{achieved_offered_ratio:.6f}",
            "caliper_reported_throughput_tps": result["throughput_tps"],
            **({
                "endorsement_timing_n": str(len(endorse_samples)),
                "commit_timing_n": str(len(commit_samples)),
            } if validate_all_raw else {}),
            "overall_successful_e2e_median_ms": f"{percentile(successful_latencies, 0.50):.6f}",
            "overall_successful_e2e_p95_ms": f"{percentile(successful_latencies, 0.95):.6f}",
            "overall_successful_e2e_p99_ms": f"{percentile(successful_latencies, 0.99):.6f}",
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
            **({
                "endorse_source": relative(endorse_path, project_root),
                "endorse_sha256": sha256_file(endorse_path),
                "commit_source": relative(commit_path, project_root),
                "commit_sha256": sha256_file(commit_path),
                "heights_source": relative(heights_path, project_root),
                "heights_sha256": sha256_file(heights_path),
                "benchmark_source": relative(benchmark_path, project_root),
                "benchmark_sha256": sha256_file(benchmark_path),
            } if validate_all_raw else {}),
            "log_source": relative(log_path, project_root),
            "log_sha256": sha256_file(log_path),
        }
        if config != "sphincs":
            row = {
                "config": FIXED_CONFIGS[config],
                "run_namespace": run_namespace,
                "run_type": "sustainability",
                "round_label": round_label,
                "offered_tps": str(offered_rate),
                "duration_seconds": str(SWEEP_DURATION_SECONDS),
                "total_count": str(success + fail),
                "success_count": str(success),
                "fail_count": str(fail),
                "tx_success_rate": tx_success_rate,
                "tx_error_rate": tx_error_rate,
                "successful_throughput_tps": f"{successful_throughput:.6f}",
                "successful_throughput_ratio": f"{achieved_offered_ratio:.6f}",
                "caliper_reported_throughput_tps": result["throughput_tps"],
                **({
                    "endorsement_timing_count": str(len(endorse_samples)),
                    "commit_timing_count": str(len(commit_samples)),
                } if validate_all_raw else {}),
                "e2e_median_ms": f"{percentile(successful_latencies, 0.50):.6f}",
                "e2e_p95_ms": f"{percentile(successful_latencies, 0.95):.6f}",
                "e2e_p99_ms": f"{percentile(successful_latencies, 0.99):.6f}",
                "begin_window_ms": "[0,12000)",
                "end_window_ms": "[48000,60000)",
                "begin_window_success_count": str(len(beginning)),
                "end_window_success_count": str(len(end)),
                "begin_window_p95_ms": f"{beginning_p95:.6f}",
                "end_window_p95_ms": f"{end_p95:.6f}",
                "latency_stability_ratio": f"{ratio:.6f}",
                "success_rate_gate_pass": str(success_pass).lower(),
                "throughput_gate_pass": str(throughput_pass).lower(),
                "latency_stability_gate_pass": str(latency_pass).lower(),
                "sustainable": str(sustainable).lower(),
                "e2e_source": relative(by_round[round_label], project_root),
                "e2e_sha256": sha256_file(by_round[round_label]),
                **({
                    "endorse_source": relative(endorse_path, project_root),
                    "endorse_sha256": sha256_file(endorse_path),
                    "commit_source": relative(commit_path, project_root),
                    "commit_sha256": sha256_file(commit_path),
                    "heights_source": relative(heights_path, project_root),
                    "heights_sha256": sha256_file(heights_path),
                    "benchmark_source": relative(benchmark_path, project_root),
                    "benchmark_sha256": sha256_file(benchmark_path),
                } if validate_all_raw else {}),
                "log_source": relative(log_path, project_root),
                "log_sha256": sha256_file(log_path),
            }
        rows.append(row)

    if validate_all_raw:
        namespace_paths = set(raw_dir.glob(f"{run_namespace}_*"))
        supplemental_block_evidence = set(
            raw_dir.glob(f"{run_namespace}_blocks_*.csv")
        ) | set(raw_dir.glob(f"{run_namespace}_transactions_*.csv"))
        namespace_paths -= supplemental_block_evidence
        if namespace_paths != validated_paths:
            unexpected = sorted(str(path.name) for path in namespace_paths - validated_paths)
            missing = sorted(str(path.name) for path in validated_paths - namespace_paths)
            raise ValueError(
                f"{raw_dir}: namespace file set does not reconcile; "
                f"unexpected={unexpected}, missing={missing}"
            )

    rate_field = "offered_rate_tps" if config == "sphincs" else "offered_tps"
    passing_rates = [
        int(row[rate_field]) for row in rows if row["sustainable"] == "true"
    ]
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
        "start_block", "end_block", "ordinary_block_count", "excluded_block_count",
        "block_bytes_mean", "transactions_per_block_mean",
        "effective_preferred_max_bytes", "block_utilisation", "raw_blocks_file",
        "raw_blocks_sha256", "ordinary_transaction_count", "valid_transaction_count",
        "invalid_transaction_count", "tx_bytes_mean", "endorsements_per_tx",
        "endorsements_min", "endorsements_max", "raw_transactions_file",
        "raw_transactions_sha256",
    }
    if not required_summary_fields.issubset(summary):
        raise ValueError(f"{summary_path}: summary predates serialized transaction evidence")

    raw_blocks_path = (project_root / summary["raw_blocks_file"]).resolve()
    if not raw_blocks_path.is_relative_to(project_root) or not raw_blocks_path.is_file():
        raise ValueError(f"{summary_path}: invalid block evidence path")
    if sha256_file(raw_blocks_path) != summary["raw_blocks_sha256"]:
        raise ValueError(f"{raw_blocks_path}: SHA-256 does not match summary")
    with raw_blocks_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        required_block_fields = {
            "config", "run_label", "benchmark_label", "block_number", "block_bytes",
            "transaction_count", "header_types", "classification", "accepted_for_mean",
        }
        if reader.fieldnames is None or set(reader.fieldnames) != required_block_fields:
            raise ValueError(f"{raw_blocks_path}: unexpected per-block schema")
        block_rows = list(reader)
    if not block_rows:
        raise ValueError(f"{raw_blocks_path}: no block evidence")

    accepted_block_counts: dict[int, int] = {}
    accepted_block_bytes: list[int] = []
    excluded_blocks = 0
    block_numbers = []
    for row in block_rows:
        if (
            row["config"] != summary["config"]
            or row["run_label"] != summary["run_label"]
            or row["benchmark_label"] != summary["benchmark_label"]
        ):
            raise ValueError(f"{raw_blocks_path}: mixed block provenance")
        number = parse_uint(raw_blocks_path, "block_number", row["block_number"])
        size = parse_uint(raw_blocks_path, "block_bytes", row["block_bytes"])
        tx_count = parse_uint(
            raw_blocks_path, "transaction_count", row["transaction_count"]
        )
        header_types = row["header_types"].split(";") if row["header_types"] else []
        if len(header_types) != tx_count:
            raise ValueError(f"{raw_blocks_path}: block {number} header count mismatch")
        accepted = row["accepted_for_mean"] == "true"
        if row["accepted_for_mean"] not in {"true", "false"}:
            raise ValueError(f"{raw_blocks_path}: block {number} invalid acceptance flag")
        if accepted:
            if (
                number == 0
                or row["classification"] != "ordinary_transaction"
                or any(header_type != "3" for header_type in header_types)
            ):
                raise ValueError(f"{raw_blocks_path}: block {number} is not ordinary")
            accepted_block_counts[number] = tx_count
            accepted_block_bytes.append(size)
        else:
            if row["classification"] == "ordinary_transaction":
                raise ValueError(f"{raw_blocks_path}: ordinary block {number} was excluded")
            excluded_blocks += 1
        block_numbers.append(number)
    if len(set(block_numbers)) != len(block_numbers) or sorted(block_numbers) != list(
        range(min(block_numbers), max(block_numbers) + 1)
    ):
        raise ValueError(f"{raw_blocks_path}: block interval is not unique and contiguous")
    preferred = parse_uint(
        summary_path, "effective_preferred_max_bytes",
        summary["effective_preferred_max_bytes"],
    )
    block_mean = sum(accepted_block_bytes) / len(accepted_block_bytes)
    block_regenerated = {
        "start_block": str(min(block_numbers)),
        "end_block": str(max(block_numbers)),
        "ordinary_block_count": str(len(accepted_block_counts)),
        "excluded_block_count": str(excluded_blocks),
        "block_bytes_mean": f"{block_mean:.6f}",
        "transactions_per_block_mean": (
            f"{sum(accepted_block_counts.values()) / len(accepted_block_counts):.6f}"
        ),
        "block_utilisation": f"{block_mean / preferred:.9f}",
    }
    for field, value in block_regenerated.items():
        if summary[field] != value:
            raise ValueError(f"{summary_path}: {field} does not regenerate from blocks")

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
    unavailable_count = 0
    accepted_transactions_by_block: dict[int, int] = {}
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
        block_number = parse_uint(transaction_path, "block_number", row["block_number"])
        accepted_transactions_by_block[block_number] = (
            accepted_transactions_by_block.get(block_number, 0) + 1
        )
        if row["validation_code"] == "0":
            valid_count += 1
        elif row["validation_code"] == "-1" and row["validation_name"] == "unavailable":
            unavailable_count += 1
        else:
            invalid_count += 1

    if not accepted_bytes:
        raise ValueError(f"{transaction_path}: no accepted ordinary transactions")
    if accepted_transactions_by_block != accepted_block_counts:
        raise ValueError(
            f"{transaction_path}: per-block transaction counts do not match block evidence"
        )
    regenerated = {
        "ordinary_transaction_count": str(len(accepted_bytes)),
        "valid_transaction_count": str(valid_count),
        "invalid_transaction_count": str(invalid_count),
        "validation_unavailable_count": str(unavailable_count),
        "tx_bytes_mean": f"{sum(accepted_bytes) / len(accepted_bytes):.6f}",
        "endorsements_per_tx": f"{sum(endorsements) / len(endorsements):.6f}",
        "endorsements_min": str(min(endorsements)),
        "endorsements_max": str(max(endorsements)),
    }
    for field in (
        "ordinary_transaction_count", "tx_bytes_mean", "endorsements_per_tx",
        "endorsements_min", "endorsements_max",
    ):
        value = regenerated[field]
        if summary[field] != value:
            raise ValueError(f"{summary_path}: {field} does not regenerate from transactions")
    if "validation_unavailable_count" in summary:
        for field in (
            "valid_transaction_count", "invalid_transaction_count",
            "validation_unavailable_count",
        ):
            if summary[field] != regenerated[field]:
                raise ValueError(
                    f"{summary_path}: {field} does not regenerate from transactions"
                )
        validation_semantics = "validation_codes_reported_separately"
    else:
        legacy_invalid = invalid_count + unavailable_count
        if (
            summary["valid_transaction_count"] != str(valid_count)
            or summary["invalid_transaction_count"] != str(legacy_invalid)
        ):
            raise ValueError(
                f"{summary_path}: legacy validation counts do not regenerate"
            )
        validation_semantics = (
            "legacy_summary_grouped_validation_unavailable_with_invalid; "
            "derived invalid/unavailable counts above supersede those two raw-summary fields"
        )
    return [{
        "config": summary["config"],
        "run_label": summary["run_label"],
        "benchmark_label": summary["benchmark_label"],
        **block_regenerated,
        **regenerated,
        "validation_evidence_status": validation_semantics,
        "raw_blocks_file": summary["raw_blocks_file"],
        "raw_blocks_sha256": summary["raw_blocks_sha256"],
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
        "--latency-professor-review",
        action="store_true",
        help=(
            "report separate 50-TPS and 200-TPS latency populations, retaining "
            "SPHINCS+ as saturation-only evidence"
        ),
    )
    mode.add_argument(
        "--clean-fixed-profile",
        metavar="RUN_NAMESPACE",
        help=(
            "validate one namespaced clean common-profile rerun, preserving "
            "separate 50-TPS and 200-TPS rows"
        ),
    )
    mode.add_argument(
        "--clean-latency-professor-review",
        action="store_true",
        help=(
            "validate and combine all four clean common-profile reruns with "
            "block cadence and exact transaction evidence"
        ),
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
        help="evaluate an allowlisted 60-second rate probe from namespaced raw evidence",
    )
    mode.add_argument(
        "--block-transactions",
        metavar="SUMMARY_CSV",
        help="validate exact serialized transaction sizes and endorsement counts",
    )
    mode.add_argument(
        "--pq-verification-audits",
        action="store_true",
        help="validate retained functional liboqs PQ verification traces",
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
        elif args.clean_latency_professor_review:
            rows = build_clean_latency_professor_review_rows(project_root)
        elif args.clean_fixed_profile:
            rows = build_clean_fixed_profile_rows(
                project_root, args.clean_fixed_profile
            )
        elif args.latency_professor_review:
            rows = build_latency_professor_review_rows(project_root)
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
        elif args.pq_verification_audits:
            rows = validate_pq_verification_audits(project_root)
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
