#!/usr/bin/env python3
"""Validate future E7 per-sample evidence and emit the professor CSV."""

from __future__ import annotations

import argparse
import csv
from datetime import timedelta
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

try:
    from .config import CONFIG_NAMES, get_configuration
    from .prepare_trace import (
        DATASET_LICENCE,
        DATASET_NAME,
        DATASET_PRIMARY_SOURCE,
        DATASET_RESOLUTION,
        DATASET_VERSION,
        COUNTER_POLICY,
        INTERVAL,
        INTERVAL_TIMESTAMP_CONVENTION,
        INTERPOLATION_COLUMN,
        MINIMUM_TRACE_ROWS,
        PROVENANCE_SCHEMA,
        SELECTION_POLICY,
        TRACE_FIELDS,
        TRANSFORMATION,
        UPSTREAM_REPRESENTATION,
        UPSTREAM_FILENAME,
        WARMUP_SAMPLES,
        parse_energy,
        parse_timestamp,
        sha256,
    )
except ImportError:  # Direct execution: python3 src/e7/finalize_e7.py
    from config import CONFIG_NAMES, get_configuration
    from prepare_trace import (
        DATASET_LICENCE,
        DATASET_NAME,
        DATASET_PRIMARY_SOURCE,
        DATASET_RESOLUTION,
        DATASET_VERSION,
        COUNTER_POLICY,
        INTERVAL,
        INTERVAL_TIMESTAMP_CONVENTION,
        INTERPOLATION_COLUMN,
        MINIMUM_TRACE_ROWS,
        PROVENANCE_SCHEMA,
        SELECTION_POLICY,
        TRACE_FIELDS,
        TRANSFORMATION,
        UPSTREAM_REPRESENTATION,
        UPSTREAM_FILENAME,
        WARMUP_SAMPLES,
        parse_energy,
        parse_timestamp,
        sha256,
    )


FINAL_FIELDS = ("config", "run_id", "sample_idx", "latency_ms")
MINIMUM_MEASURED_SAMPLES = 1000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_trace(trace_path: Path, provenance_path: Path) -> int:
    try:
        trace_content = trace_path.read_bytes()
        provenance = json.loads(provenance_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("E7 trace provenance is missing or malformed") from error
    if not isinstance(provenance, dict) or provenance.get("schema") != PROVENANCE_SCHEMA:
        raise ValueError("E7 trace provenance schema mismatch")
    dataset = provenance.get("dataset")
    if dataset != {
        "name": DATASET_NAME,
        "version": DATASET_VERSION,
        "primary_source": DATASET_PRIMARY_SOURCE,
        "resolution": DATASET_RESOLUTION,
        "licence": DATASET_LICENCE,
        "source_filename": UPSTREAM_FILENAME,
    }:
        raise ValueError("E7 dataset provenance mismatch")
    selected = provenance.get("selected_source_columns")
    if (
        not isinstance(selected, dict)
        or set(selected)
        != {"timestamp", "photovoltaic_generation", "grid_import", "grid_export"}
        or any(not isinstance(value, str) or not value for value in selected.values())
    ):
        raise ValueError("E7 selected source-column provenance is missing")
    if provenance.get("interpolation") != {
        "column": INTERPOLATION_COLUMN,
        "policy": "selected E7 source feeds must be non-interpolated",
        "selected_feed_interpolated_row_count": 0,
    }:
        raise ValueError("E7 interpolation provenance mismatch")
    if provenance.get("transformation") != {
        "upstream_data_representation": UPSTREAM_REPRESENTATION,
        "method": TRANSFORMATION,
        "interval_timestamp_convention": INTERVAL_TIMESTAMP_CONVENTION,
        "monotonic_counter_reset_policy": COUNTER_POLICY,
    }:
        raise ValueError("E7 cumulative-to-interval transformation mismatch")
    upstream_sha = provenance.get("upstream_sha256")
    if not isinstance(upstream_sha, str) or not SHA256_PATTERN.fullmatch(upstream_sha):
        raise ValueError("E7 upstream SHA-256 provenance is invalid")
    if provenance.get("prepared_trace_sha256") != sha256(trace_content):
        raise ValueError("E7 prepared trace SHA-256 mismatch")

    try:
        reader = csv.DictReader(io.StringIO(trace_content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 prepared trace has duplicate CSV header names")
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 prepared trace is malformed") from error
    if fields != TRACE_FIELDS:
        raise ValueError("E7 prepared trace schema mismatch")
    if len(rows) < MINIMUM_TRACE_ROWS:
        raise ValueError("E7 prepared trace is too short")
    timestamps = []
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in TRACE_FIELDS):
            raise ValueError("malformed E7 prepared trace row")
        timestamp = parse_timestamp(row["timestamp"])
        pv = parse_energy(row["pv_kwh"], "PV")
        demand = parse_energy(row["demand_kwh"], "demand")
        try:
            net = float(row["net_kwh"])
        except (TypeError, ValueError) as error:
            raise ValueError("malformed E7 prepared trace net_kwh") from error
        if not math.isfinite(net) or not math.isclose(
            net, pv - demand, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("E7 prepared trace net_kwh mismatch")
        timestamps.append(timestamp)
    if any(right - left != INTERVAL for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("E7 prepared trace timestamps are not 15 minutes apart")
    selection = provenance.get("selection")
    expected_selection = {
        "policy": SELECTION_POLICY,
        "source_rows_required": len(rows) + 1,
        "prepared_interval_rows": len(rows),
        "warmup_rows": WARMUP_SAMPLES,
        "measured_rows": len(rows) - WARMUP_SAMPLES,
        "source_timestamp_start": (timestamps[0] - INTERVAL).isoformat(),
        "source_timestamp_end": timestamps[-1].isoformat(),
        "prepared_interval_timestamp_start": timestamps[0].isoformat(),
        "prepared_interval_timestamp_end": timestamps[-1].isoformat(),
    }
    if selection != expected_selection:
        raise ValueError("E7 trace selection provenance mismatch")
    return len(rows)


def load_evidence(evidence_path: Path) -> list[dict[str, str]]:
    try:
        content = evidence_path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 evidence has duplicate CSV header names")
        rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 evidence is missing or malformed") from error
    if fields != FINAL_FIELDS:
        raise ValueError("E7 evidence schema mismatch")
    return rows


def validate_rows(rows: list[dict[str, str]], trace_row_count: int) -> list[dict[str, object]]:
    if not rows:
        raise ValueError("E7 evidence is empty")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    populations: dict[str, set[tuple[str, int]]] = {
        name: set() for name in CONFIG_NAMES
    }
    counts: dict[str, int] = {name: 0 for name in CONFIG_NAMES}
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in FINAL_FIELDS):
            raise ValueError("malformed E7 evidence row")
        config = row["config"]
        get_configuration(config)
        run_id = row["run_id"].strip()
        if not run_id:
            raise ValueError("E7 run_id must be non-empty")
        try:
            sample_idx = int(row["sample_idx"])
            latency_ms = float(row["latency_ms"])
        except (TypeError, ValueError) as error:
            raise ValueError("E7 sample_idx/latency_ms is malformed") from error
        measured_trace_rows = trace_row_count - WARMUP_SAMPLES
        if sample_idx < 0 or sample_idx >= measured_trace_rows:
            raise ValueError("E7 sample_idx is outside the measured trace population")
        if not math.isfinite(latency_ms) or latency_ms <= 0:
            raise ValueError("E7 latency_ms must be finite and positive")
        key = (config, run_id, sample_idx)
        if key in seen:
            raise ValueError("duplicate E7 (config, run_id, sample_idx)")
        seen.add(key)
        populations[config].add((run_id, sample_idx))
        counts[config] += 1
        normalized.append(
            {
                "config": config,
                "run_id": run_id,
                "sample_idx": sample_idx,
                "latency_ms": latency_ms,
            }
        )
    if any(count < MINIMUM_MEASURED_SAMPLES for count in counts.values()):
        raise ValueError("E7 requires at least 1000 measured samples per configuration")
    first_population = populations[CONFIG_NAMES[0]]
    if any(populations[name] != first_population for name in CONFIG_NAMES[1:]):
        raise ValueError(
            "E7 measured (run_id, sample_idx) populations differ across configurations"
        )
    return normalized


def _run_sort_key(run_id: str) -> tuple[int, object]:
    return (0, int(run_id)) if run_id.isdigit() else (1, run_id)


def render_final(rows: list[dict[str, object]]) -> bytes:
    config_order = {name: index for index, name in enumerate(CONFIG_NAMES)}
    ordered = sorted(
        rows,
        key=lambda row: (
            config_order[str(row["config"])],
            _run_sort_key(str(row["run_id"])),
            int(row["sample_idx"]),
        ),
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FINAL_FIELDS)
    for row in ordered:
        writer.writerow(
            (
                row["config"],
                row["run_id"],
                row["sample_idx"],
                format(float(row["latency_ms"]), ".9f"),
            )
        )
    return output.getvalue().encode("utf-8")


def finalize(
    evidence_path: Path,
    trace_path: Path,
    provenance_path: Path,
    output_path: Path,
) -> None:
    trace_rows = validate_trace(trace_path, provenance_path)
    content = render_final(validate_rows(load_evidence(evidence_path), trace_rows))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--trace-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    finalize(
        arguments.evidence,
        arguments.trace,
        arguments.trace_provenance,
        arguments.output,
    )


if __name__ == "__main__":
    main()
