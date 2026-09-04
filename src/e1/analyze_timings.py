#!/usr/bin/env python3
"""Validate and summarize retained E1 evidence.

The default output contains per-round candidate timing statistics. SPHINCS+ is
intentionally excluded because its fixed-profile run had substantial failures
and is not a valid normal-latency data point.

``--working-e1`` emits the exact final E1 schema to stdout, but leaves every
scientifically unresolved field empty. It currently populates only the accepted
boundary-inclusive ECDSA block-utilisation working result after validating its
retained summary and per-block source. This mode cannot write an output file, so
it cannot be mistaken for the completed ``data/e1_fabric.csv`` deliverable.
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
    "block_bytes_mean",
    "block_utilisation",
)


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


def load_caliper_results(log_path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, set[tuple[str, str, str, str]]] = {
        round_label: set() for round_label in ROUNDS
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


def build_working_e1_rows(project_root: Path) -> list[dict[str, str]]:
    """Build an explicitly incomplete E1-schema view from supported evidence."""
    block_mean, block_utilisation = validated_ecdsa_block_result(project_root)
    rows = [{field: "" for field in FINAL_FIELDS} for _ in FINAL_CONFIGS]
    for row, config in zip(rows, FINAL_CONFIGS):
        row["config"] = config
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

            rows.append(
                {
                    "config": display_label,
                    "round_label": round_label,
                    **caliper_results[round_label],
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--working-e1",
        action="store_true",
        help=(
            "print the exact final E1 schema with supported working values and "
            "unresolved fields left empty; cannot be written with --output"
        ),
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
        rows = (
            build_working_e1_rows(project_root)
            if args.working_e1
            else build_rows(project_root)
        )
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
