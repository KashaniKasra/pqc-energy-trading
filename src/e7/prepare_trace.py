#!/usr/bin/env python3
"""Prepare a compact, deterministic 15-minute E7 trace from OPSD household data."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile


DATASET_NAME = "Open Power System Data — Household Data"
DATASET_VERSION = "2020-04-15"
DATASET_PRIMARY_SOURCE = "CoSSMic"
DATASET_RESOLUTION = "15 minutes"
DATASET_LICENCE = "Creative Commons Attribution-International"
UPSTREAM_FILENAME = "household_data_15min_singleindex.csv"
INTERPOLATION_COLUMN = "interpolated"
TRACE_FIELDS = ("timestamp", "pv_kwh", "demand_kwh", "net_kwh")
PROVENANCE_SCHEMA = "pqc-energy-trading.e7-trace-provenance.v1"
INTERVAL = timedelta(minutes=15)
WARMUP_SAMPLES = 100
MEASURED_SAMPLES = 1000
MINIMUM_TRACE_ROWS = WARMUP_SAMPLES + MEASURED_SAMPLES
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse_timestamp(value: str | None) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid OPSD timestamp {value!r}")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"invalid OPSD timestamp {value!r}") from error
    return timestamp


def parse_energy(value: str | None, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label} value {value!r}") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


def _format_number(value: float) -> str:
    return format(value, ".12g")


def interpolated_source_columns(value: str | None) -> set[str]:
    """Parse and case-fold OPSD's semicolon-delimited interpolation marker."""
    if not isinstance(value, str):
        raise ValueError("OPSD interpolation marker is missing")
    return {
        column.strip().casefold() for column in value.split(";") if column.strip()
    }


def select_window(
    upstream_content: bytes,
    *,
    timestamp_column: str,
    pv_column: str,
    grid_import_column: str,
    grid_export_column: str,
    row_count: int = MINIMUM_TRACE_ROWS,
) -> list[tuple[datetime, float, float, float]]:
    if row_count < MINIMUM_TRACE_ROWS:
        raise ValueError(f"E7 trace requires at least {MINIMUM_TRACE_ROWS} rows")
    try:
        reader = csv.DictReader(io.StringIO(upstream_content.decode("utf-8"), newline=""))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("invalid OPSD CSV encoding or syntax") from error
    required = (
        timestamp_column,
        pv_column,
        grid_import_column,
        grid_export_column,
        INTERPOLATION_COLUMN,
    )
    if len(set(required)) != len(required):
        raise ValueError(
            "timestamp, PV, grid-import, grid-export, and interpolation columns "
            "must be distinct"
        )
    fields = tuple(reader.fieldnames or ())
    if len(fields) != len(set(fields)):
        raise ValueError("OPSD source has duplicate CSV header names")
    missing = [column for column in required if column not in fields]
    if missing:
        raise ValueError(f"OPSD source columns are missing: {missing}")

    segment: list[tuple[datetime, float, float, float]] = []
    selected_feeds = {
        pv_column.casefold(),
        grid_import_column.casefold(),
        grid_export_column.casefold(),
    }
    previous_timestamp: datetime | None = None
    for source_index, row in enumerate(reader):
        if None in row:
            raise ValueError(f"malformed OPSD row {source_index}")
        timestamp_value = row.get(timestamp_column)
        interpolation_value = row.get(INTERPOLATION_COLUMN)
        if timestamp_value is None or interpolation_value is None:
            raise ValueError(f"malformed OPSD row {source_index}: missing required value")
        timestamp = parse_timestamp(timestamp_value)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("OPSD timestamps must be strictly chronological")
        previous_timestamp = timestamp
        interpolated = interpolated_source_columns(interpolation_value)
        if interpolated & selected_feeds:
            segment = []
            continue
        try:
            pv = parse_energy(row.get(pv_column), "PV")
            grid_import = parse_energy(row.get(grid_import_column), "grid import")
            grid_export = parse_energy(row.get(grid_export_column), "grid export")
        except ValueError:
            segment = []
            continue
        if segment and timestamp - segment[-1][0] != INTERVAL:
            segment = []
        demand = pv + grid_import - grid_export
        net = grid_export - grid_import
        if not math.isfinite(demand) or demand < 0 or not math.isfinite(net):
            segment = []
            continue
        segment.append((timestamp, pv, demand, net))
        if len(segment) == row_count:
            return segment
    raise ValueError(
        f"OPSD source has no contiguous valid 15-minute window of {row_count} rows"
    )


def render_trace(rows: list[tuple[datetime, float, float, float]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(TRACE_FIELDS)
    for timestamp, pv, demand, net in rows:
        writer.writerow(
            (
                timestamp.isoformat(),
                _format_number(pv),
                _format_number(demand),
                _format_number(net),
            )
        )
    return output.getvalue().encode("utf-8")


def build_provenance(
    *,
    input_path: Path,
    upstream_content: bytes,
    trace_content: bytes,
    rows: list[tuple[datetime, float, float, float]],
    timestamp_column: str,
    pv_column: str,
    grid_import_column: str,
    grid_export_column: str,
) -> dict[str, object]:
    return {
        "schema": PROVENANCE_SCHEMA,
        "dataset": {
            "name": DATASET_NAME,
            "version": DATASET_VERSION,
            "primary_source": DATASET_PRIMARY_SOURCE,
            "resolution": DATASET_RESOLUTION,
            "licence": DATASET_LICENCE,
            "source_filename": input_path.name,
        },
        "selected_source_columns": {
            "timestamp": timestamp_column,
            "photovoltaic_generation": pv_column,
            "grid_import": grid_import_column,
            "grid_export": grid_export_column,
        },
        "interpolation": {
            "column": INTERPOLATION_COLUMN,
            "policy": "selected E7 source feeds must be non-interpolated",
            "selected_feed_interpolated_row_count": 0,
        },
        "selection": {
            "policy": (
                "earliest contiguous valid non-interpolated-selected-feed "
                "15-minute window in source order"
            ),
            "timestamp_start": rows[0][0].isoformat(),
            "timestamp_end": rows[-1][0].isoformat(),
            "row_count": len(rows),
            "warmup_rows": WARMUP_SAMPLES,
            "measured_rows": len(rows) - WARMUP_SAMPLES,
        },
        "upstream_sha256": sha256(upstream_content),
        "prepared_trace_sha256": sha256(trace_content),
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_trace(
    input_path: Path,
    output_trace: Path,
    provenance_path: Path,
    *,
    timestamp_column: str,
    pv_column: str,
    grid_import_column: str,
    grid_export_column: str,
    row_count: int = MINIMUM_TRACE_ROWS,
) -> None:
    if input_path.name != UPSTREAM_FILENAME:
        raise ValueError(f"expected authoritative OPSD source filename {UPSTREAM_FILENAME}")
    try:
        upstream = input_path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read OPSD input {input_path}") from error
    rows = select_window(
        upstream,
        timestamp_column=timestamp_column,
        pv_column=pv_column,
        grid_import_column=grid_import_column,
        grid_export_column=grid_export_column,
        row_count=row_count,
    )
    trace = render_trace(rows)
    provenance = build_provenance(
        input_path=input_path,
        upstream_content=upstream,
        trace_content=trace,
        rows=rows,
        timestamp_column=timestamp_column,
        pv_column=pv_column,
        grid_import_column=grid_import_column,
        grid_export_column=grid_export_column,
    )
    provenance_content = (
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(output_trace, trace)
    _atomic_write(provenance_path, provenance_content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--timestamp-column", required=True)
    parser.add_argument("--pv-column", required=True)
    parser.add_argument("--grid-import-column", required=True)
    parser.add_argument("--grid-export-column", required=True)
    parser.add_argument("--output-trace", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    arguments = parser.parse_args()
    prepare_trace(
        arguments.input,
        arguments.output_trace,
        arguments.provenance,
        timestamp_column=arguments.timestamp_column,
        pv_column=arguments.pv_column,
        grid_import_column=arguments.grid_import_column,
        grid_export_column=arguments.grid_export_column,
    )


if __name__ == "__main__":
    main()
