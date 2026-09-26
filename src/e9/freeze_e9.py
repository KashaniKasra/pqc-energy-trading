#!/usr/bin/env python3
"""Validate and deterministically freeze original E9 scientific evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.e9.finalize_e9 import (
    CONDITION_COUNT,
    CONFIG_VALUES,
    EVIDENCE_NAME,
    FROZEN_SCHEMA,
    HOP_VALUES,
    MEASURED_ITERATIONS,
    MEASUREMENT_COMMIT,
    PING_SAMPLES,
    RTT_VALUES_MS,
    SOURCE_FILE_COUNT,
    WARMUP_ITERATIONS,
    EvidenceRoot,
    audit,
    sha256,
)


def deterministic_gzip(content: bytes) -> bytes:
    """Return gzip -9 -n-equivalent bytes without filename or timestamp."""
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ("gzip", "-9", "-n", "-c"),
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deterministic gzip failed: {result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def _entry(logical: str, original: bytes, compressed: bytes) -> dict[str, object]:
    match = EVIDENCE_NAME.fullmatch(logical)
    if match is None:
        raise ValueError(f"invalid E9 evidence filename {logical}")
    role, config, rtt_text, hops_text, _ = match.groups()
    evidence_role = "completion_samples" if role == "completion" else role
    return {
        "original_filename": logical,
        "evidence_role": evidence_role,
        "config": config,
        "rtt_ms": int(rtt_text),
        "hops": int(hops_text),
        "compression": "gzip",
        "original_size": len(original),
        "original_sha256": sha256(original),
        "tracked_filename": f"raw/e9/{logical}.gz",
        "tracked_size": len(compressed),
        "tracked_sha256": sha256(compressed),
    }


def build_frozen_manifest(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": FROZEN_SCHEMA,
        "experiment": "E9",
        "measurement_commit": MEASUREMENT_COMMIT,
        "condition_count": CONDITION_COUNT,
        "source_file_count": SOURCE_FILE_COUNT,
        "configs": list(CONFIG_VALUES),
        "rtt_ms_per_payment_channel": list(RTT_VALUES_MS),
        "hops_payment_channel_count": list(HOP_VALUES),
        "expected_conditions": [
            {"config": config, "rtt_ms": rtt_ms, "hops": hops}
            for config in CONFIG_VALUES
            for rtt_ms in RTT_VALUES_MS
            for hops in HOP_VALUES
        ],
        "warmup_iterations_per_condition": WARMUP_ITERATIONS,
        "measured_iterations_per_condition": MEASURED_ITERATIONS,
        "ping_samples_per_condition": PING_SAMPLES,
        "raw_sample_rows_total": CONDITION_COUNT
        * (WARMUP_ITERATIONS + MEASURED_ITERATIONS),
        "warmup_rows_total": CONDITION_COUNT * WARMUP_ITERATIONS,
        "measured_rows_total": CONDITION_COUNT * MEASURED_ITERATIONS,
        "failure_count": 0,
        "compression": {
            "format": "gzip",
            "level": 9,
            "deterministic_header": True,
            "command": "LC_ALL=C gzip -9 -n",
            "original_sha256_semantics": (
                "SHA-256 of decompressed/original scientific evidence bytes"
            ),
            "tracked_sha256_semantics": "SHA-256 of tracked gzip container bytes",
        },
        "entries": sorted(entries, key=lambda entry: str(entry["original_filename"])),
    }


def freeze(source_root: Path, output_root: Path) -> None:
    source = EvidenceRoot(source_root)
    audit(source_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"refusing non-empty frozen evidence root: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}-freeze-", dir=output_root.parent)
    )
    try:
        entries: list[dict[str, object]] = []
        for logical in sorted(source.contents):
            original = source.read(logical)
            compressed = deterministic_gzip(original)
            tracked = temporary / f"{logical}.gz"
            descriptor = os.open(tracked, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(compressed)
                destination.flush()
                os.fsync(destination.fileno())
            entries.append(_entry(logical, original, compressed))
        manifest_content = (
            json.dumps(build_frozen_manifest(entries), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        manifest_path = temporary / "evidence_manifest.json"
        manifest_path.write_bytes(manifest_content)
        EvidenceRoot(temporary)
        audit(temporary)
        if output_root.exists():
            output_root.rmdir()
        temporary.replace(output_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    freeze(arguments.source_root, arguments.output_root)


if __name__ == "__main__":
    main()
