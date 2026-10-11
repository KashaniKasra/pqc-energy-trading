#!/usr/bin/env python3
"""Validate future E7 per-sample evidence and emit the professor CSV."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
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
RAW_FIELDS = (
    "config", "run_id", "phase", "phase_idx", "sample_idx", "trace_row_idx",
    "trace_timestamp", "trace_row_sha256", "logical_trade_id", "direction",
    "amount_kwh", "payer", "payee", "start_monotonic_ns", "end_monotonic_ns",
    "latency_ms", "scientific", "success", "error", "request_id",
    "transaction_id", "completion_status", "channel_id", "htlc_id",
    "add_algorithm", "settle_algorithm", "add_serialized_bytes",
    "settle_serialized_bytes", "add_serialized_sha256",
    "settle_serialized_sha256", "authorization_verified", "receiver_verified",
    "committed", "verified", "validation_status", "validation_code",
    "payload_sha256",
    "sets_ecdsa_algorithm", "sets_signed_preimage_sha256",
    "sets_public_key_sha256", "sets_signature_sha256",
    "sets_signature_verified", "sets_controller_eligible",
    "sets_controller_balance_sufficient",
)
MINIMUM_MEASURED_SAMPLES = 1000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SETS_ECDSA_ALGORITHM = "ECDSA-P256-SHA256"


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


def load_raw_evidence(evidence_path: Path) -> list[dict[str, str]]:
    try:
        content = evidence_path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 raw evidence has duplicate CSV header names")
        rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 raw evidence is missing or malformed") from error
    if fields != RAW_FIELDS:
        raise ValueError("E7 raw evidence schema mismatch")
    return rows


def evidence_fields(evidence_path: Path) -> tuple[str, ...]:
    try:
        with evidence_path.open(newline="", encoding="utf-8") as source:
            fields = tuple(csv.reader(source).__next__())
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration) as error:
        raise ValueError("E7 evidence is missing or malformed") from error
    if len(fields) != len(set(fields)):
        raise ValueError("E7 evidence has duplicate CSV header names")
    return fields


def _identity_field(name: str, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return f"{name}:{len(encoded)}:".encode("ascii") + encoded + b"\n"


def _expected_trade(trace_sha: str, row: dict[str, str], trace_row_idx: int):
    try:
        net = Decimal(row["net_kwh"])
    except (InvalidOperation, TypeError, KeyError) as error:
        raise ValueError("malformed E7 trace net_kwh") from error
    if not net.is_finite():
        raise ValueError("non-finite E7 trace net_kwh")
    if net > 0:
        direction, amount, payer, payee = (
            "prosumer_sale", row["net_kwh"],
            "external-market-grid-counterparty", "residential3",
        )
    elif net < 0:
        direction, amount, payer, payee = (
            "prosumer_purchase", row["net_kwh"].removeprefix("-"),
            "residential3", "external-market-grid-counterparty",
        )
    else:
        direction, amount, payer, payee = "balanced_no_transfer", "0", "none", "none"
    values = (
        ("schema_version", "e7-v1"), ("trace_sha256", trace_sha),
        ("run_id", "1"), ("trace_row_idx", str(trace_row_idx)),
        ("timestamp", row["timestamp"]), ("direction", direction),
        ("amount_kwh", amount), ("payer", payer), ("payee", payee),
    )
    preimage = b"".join(_identity_field(name, value) for name, value in values)
    return hashlib.sha256(preimage).hexdigest(), direction, amount, payer, payee


def _expected_logical_settlement(
    trace_sha: str,
    row: dict[str, str],
    trace_row_idx: int,
    phase: str,
    sample_idx: int,
    trade_id: str,
    direction: str,
    amount: str,
    payer: str,
    payee: str,
) -> bytes:
    values = (
        ("schema_version", "e7-v1"), ("trace_sha256", trace_sha),
        ("run_id", "1"), ("phase", phase),
        ("trace_row_idx", str(trace_row_idx)), ("sample_idx", str(sample_idx)),
        ("timestamp", row["timestamp"]), ("pv_kwh", row["pv_kwh"]),
        ("demand_kwh", row["demand_kwh"]), ("net_kwh", row["net_kwh"]),
        ("direction", direction), ("amount_kwh", amount), ("payer", payer),
        ("payee", payee), ("logical_trade_id", trade_id),
    )
    return b"".join(_identity_field(name, value) for name, value in values)


def _trace_identity_rows(trace_path: Path):
    content = trace_path.read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
    fields = tuple(reader.fieldnames or ())
    if fields != TRACE_FIELDS or len(fields) != len(set(fields)):
        raise ValueError("E7 prepared trace schema mismatch")
    rows = list(reader)
    return sha256(content), rows


def validate_raw_rows(rows: list[dict[str, str]], trace_path: Path) -> list[dict[str, object]]:
    trace_sha, trace_rows = _trace_identity_rows(trace_path)
    if len(trace_rows) != MINIMUM_TRACE_ROWS:
        raise ValueError("E7 raw finalization requires exactly 1100 trace rows")
    normalized = []
    seen = set()
    populations = {name: set() for name in CONFIG_NAMES}
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in RAW_FIELDS):
            raise ValueError("malformed E7 raw evidence row")
        config = row["config"]
        get_configuration(config)
        try:
            trace_idx = int(row["trace_row_idx"])
            phase_idx = int(row["phase_idx"])
            sample_idx = int(row["sample_idx"])
            started = int(row["start_monotonic_ns"])
            ended = int(row["end_monotonic_ns"])
            latency = float(row["latency_ms"])
        except (TypeError, ValueError) as error:
            raise ValueError("malformed E7 raw evidence numeric field") from error
        if not 0 <= trace_idx < len(trace_rows):
            raise ValueError("E7 raw trace_row_idx is out of range")
        expected_phase = "warmup" if trace_idx < WARMUP_SAMPLES else "measured"
        expected_idx = trace_idx if expected_phase == "warmup" else trace_idx - WARMUP_SAMPLES
        expected_sample = -1 if expected_phase == "warmup" else expected_idx
        if row["run_id"] != "1" or row["phase"] != expected_phase or phase_idx != expected_idx or sample_idx != expected_sample:
            raise ValueError("E7 raw phase/run/sample mapping mismatch")
        trace_row = trace_rows[trace_idx]
        row_bytes = (
            ",".join(trace_row[field] for field in TRACE_FIELDS) + "\n"
        ).encode("utf-8")
        trade_id, direction, amount, payer, payee = _expected_trade(trace_sha, trace_row, trace_idx)
        if (
            row["trace_timestamp"] != trace_row["timestamp"]
            or row["trace_row_sha256"] != sha256(row_bytes)
            or row["logical_trade_id"] != trade_id
            or row["direction"] != direction
            or row["amount_kwh"] != amount
            or row["payer"] != payer
            or row["payee"] != payee
        ):
            raise ValueError("E7 raw logical trade identity mismatch")
        if row["scientific"] != "true" or row["success"] != "true" or row["error"]:
            raise ValueError("E7 raw evidence is not a successful scientific sample")
        if ended <= started or not math.isfinite(latency) or latency <= 0:
            raise ValueError("E7 raw timing is invalid")
        if not math.isclose(latency, (ended - started) / 1_000_000, rel_tol=0, abs_tol=5e-10):
            raise ValueError("E7 raw latency disagrees with monotonic markers")
        key = (config, row["run_id"], trace_idx)
        if key in seen:
            raise ValueError("duplicate E7 raw sample")
        seen.add(key)
        populations[config].add(trace_idx)
        if config.endswith("_l2"):
            if row["htlc_id"] != "e7-v1:" + trade_id or row["authorization_verified"] != "true" or row["receiver_verified"] != "true":
                raise ValueError("E7 L2 verification evidence mismatch")
        else:
            if row["committed"] != "true" or row["verified"] != "true" or not row["transaction_id"]:
                raise ValueError("E7 Fabric commit evidence mismatch")
        if config == "sets_reimpl":
            common = _expected_logical_settlement(
                trace_sha, trace_row, trace_idx, expected_phase, expected_sample,
                trade_id, direction, amount, payer, payee,
            )
            if (
                row["sets_ecdsa_algorithm"] != SETS_ECDSA_ALGORITHM
                or row["sets_signed_preimage_sha256"] != sha256(common)
                or not SHA256_PATTERN.fullmatch(row["sets_public_key_sha256"])
                or not SHA256_PATTERN.fullmatch(row["sets_signature_sha256"])
                or row["sets_signature_verified"] != "true"
                or row["sets_controller_eligible"] != "true"
                or row["sets_controller_balance_sufficient"] != "true"
            ):
                raise ValueError("E7 SETS ECDSA/controller evidence mismatch")
        if expected_phase == "measured":
            normalized.append({
                "config": config, "run_id": row["run_id"],
                "sample_idx": sample_idx, "latency_ms": latency,
            })
    expected = set(range(len(trace_rows)))
    if any(populations[name] != expected for name in CONFIG_NAMES):
        raise ValueError("E7 raw trace populations differ or are incomplete")
    return validate_rows(normalized, len(trace_rows))


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
    validate_trace(trace_path, provenance_path)
    fields = evidence_fields(evidence_path)
    if fields != RAW_FIELDS:
        raise ValueError("production E7 finalization requires rich raw evidence schema")
    normalized = validate_raw_rows(load_raw_evidence(evidence_path), trace_path)
    content = render_final(normalized)
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
