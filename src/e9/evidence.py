"""Fail-closed raw-evidence persistence for one E9 network condition."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from .network import (
    MESSAGE_BYTES_BY_CONFIG,
    PingVerificationRecord,
    RawCompletionRecord,
    RunOptions,
    expected_end_to_end_rtt_ms,
    validate_config,
    validate_ping_gate,
    write_raw_completion_csv,
)


MANIFEST_SCHEMA = "pqc-energy-trading.e9-condition-evidence.v1"


def condition_stem(config: str, rtt_ms: int, hops: int) -> str:
    validate_config(config, scientific=True)
    return f"{config}_rtt{rtt_ms}_hops{hops}"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _exclusive_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def persist_condition_evidence(
    output_root: Path,
    records: Sequence[RawCompletionRecord],
    verification: PingVerificationRecord,
    options: RunOptions,
    message_bytes: int,
    environment: Mapping[str, str],
) -> Path:
    """Persist one scientific condition without overwriting prior evidence."""
    if not options.scientific:
        raise ValueError("persistent E9 evidence requires scientific mode")
    condition = verification.condition
    validate_ping_gate(verification.config, condition, verification)
    if options.measured_iterations < 1000:
        raise ValueError("persistent scientific evidence requires >=1000 measurements")
    if not records or any(not record.scientific for record in records):
        raise ValueError("persistent E9 evidence requires scientific raw records")
    expected_key = (verification.config, condition.configured_rtt_ms, condition.hops)
    if message_bytes != MESSAGE_BYTES_BY_CONFIG[verification.config]:
        raise ValueError("prepared message size does not match authoritative E2-derived policy")
    if any(
        (record.config, record.configured_rtt_ms, record.hops) != expected_key
        for record in records
    ):
        raise ValueError("raw completion records do not match ping condition")

    stem = condition_stem(*expected_key)
    ping_name = f"ping_{stem}.txt"
    completion_name = f"completion_{stem}.csv"
    manifest_name = f"manifest_{stem}.json"
    ping_path = output_root / ping_name
    completion_path = output_root / completion_name
    manifest_path = output_root / manifest_name
    targets = (ping_path, completion_path, manifest_path)
    if any(path.exists() for path in targets):
        raise FileExistsError(f"E9 evidence collision for {stem}")

    ping_content = verification.raw_output.encode("utf-8")
    completion_text = io.StringIO()
    write_raw_completion_csv(completion_text, records)
    completion_content = completion_text.getvalue().encode("utf-8")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "scientific": True,
        "condition": {
            "config": verification.config,
            "configured_per_channel_rtt_ms": condition.configured_rtt_ms,
            "hops": condition.hops,
            "expected_end_to_end_rtt_ms": expected_end_to_end_rtt_ms(condition),
            "prepared_message_bytes": message_bytes,
            "warmup_iterations": options.warmup_iterations,
            "measured_iterations": options.measured_iterations,
        },
        "ping_verification": {
            "filename": ping_name,
            "sha256": sha256_bytes(ping_content),
            "bytes": len(ping_content),
            "sample_count": len(verification.samples_ms),
            "samples_ms": list(verification.samples_ms),
            "measured_end_to_end_rtt_ms": verification.measured_end_to_end_rtt_ms,
            "deviation_pct": verification.deviation_pct,
            "pass": verification.passed,
        },
        "completion_samples": {
            "filename": completion_name,
            "sha256": sha256_bytes(completion_content),
            "bytes": len(completion_content),
            "row_count": len(records),
        },
        "environment": dict(sorted(environment.items())),
    }
    manifest_content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    written: list[Path] = []
    try:
        for path, content in (
            (ping_path, ping_content),
            (completion_path, completion_content),
            (manifest_path, manifest_content),
        ):
            _exclusive_write(path, content)
            written.append(path)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return manifest_path


def validate_condition_manifest(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("scientific") is not True:
        raise ValueError("invalid E9 evidence manifest identity")
    for section_name in ("ping_verification", "completion_samples"):
        section = manifest[section_name]
        path = manifest_path.parent / section["filename"]
        content = path.read_bytes()
        if len(content) != section["bytes"]:
            raise ValueError(f"{section_name} byte-length mismatch")
        if sha256_bytes(content) != section["sha256"]:
            raise ValueError(f"{section_name} SHA-256 mismatch")
