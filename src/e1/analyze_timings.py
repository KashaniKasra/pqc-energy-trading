#!/usr/bin/env python3
"""Validate and summarize retained candidate E1 timing samples.

This script intentionally excludes SPHINCS+ because its fixed-profile run had
substantial failures and is not a valid normal-latency data point.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
    try:
        rows = build_rows(project_root)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    fieldnames = list(rows[0])
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
