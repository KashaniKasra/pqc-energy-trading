#!/usr/bin/env python3
"""Validate and deterministically freeze complete E7 scientific evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

try:
    from .config import CONFIG_NAMES
    from .finalize_e7 import (
        FINAL_CSV_SHA256,
        FINAL_FIELDS,
        FROZEN_EVIDENCE_SCHEMA,
        FROZEN_SOURCE_FILE_COUNT,
        FUNDING_ARCHIVE_FILES,
        FUNDING_DURATIONS_NS,
        FUNDING_MEASUREMENT_COMMIT,
        FUNDING_TRANSACTION_IDS,
        SETTLEMENT_ARCHIVE_FILES,
        SETTLEMENT_MEASUREMENT_COMMIT,
        EXPECTED_TRACE_PROVENANCE_SHA256,
        EXPECTED_TRACE_SHA256,
        finalize,
        finalize_frozen,
        sha256,
    )
except ImportError:
    from config import CONFIG_NAMES
    from finalize_e7 import (
        FINAL_CSV_SHA256,
        FINAL_FIELDS,
        FROZEN_EVIDENCE_SCHEMA,
        FROZEN_SOURCE_FILE_COUNT,
        FUNDING_ARCHIVE_FILES,
        FUNDING_DURATIONS_NS,
        FUNDING_MEASUREMENT_COMMIT,
        FUNDING_TRANSACTION_IDS,
        SETTLEMENT_ARCHIVE_FILES,
        SETTLEMENT_MEASUREMENT_COMMIT,
        EXPECTED_TRACE_PROVENANCE_SHA256,
        EXPECTED_TRACE_SHA256,
        finalize,
        finalize_frozen,
        sha256,
    )


def deterministic_gzip(content: bytes) -> bytes:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ("gzip", "-9", "-n", "-c"), input=content,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "deterministic gzip failed: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )
    return result.stdout


def _require_plain_inventory(root: Path, expected: dict[str, set[str]], label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{label} evidence root is missing or unsafe")
    if {path.name for path in root.iterdir()} != set(expected):
        raise ValueError(f"{label} evidence configurations are incomplete")
    for config, filenames in expected.items():
        parent = root / config
        run = parent / "run_1"
        if (parent.is_symlink() or run.is_symlink()
                or {path.name for path in parent.iterdir()} != {"run_1"}
                or not run.is_dir()):
            raise ValueError(f"{label} evidence run identity is invalid")
        actual = set()
        for path in run.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{label} evidence contains an unsafe entry")
            actual.add(path.name)
        if actual != filenames:
            raise ValueError(f"{label} evidence file inventory mismatch for {config}")


def build_manifest(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": FROZEN_EVIDENCE_SCHEMA,
        "experiment": "E7",
        "settlement_measurement_commit": SETTLEMENT_MEASUREMENT_COMMIT,
        "funding_measurement_commit": FUNDING_MEASUREMENT_COMMIT,
        "settlement_condition_count": 4,
        "funding_condition_count": 2,
        "source_file_count": FROZEN_SOURCE_FILE_COUNT,
        "configs": list(CONFIG_NAMES),
        "funding_configs": ["classical_l2", "layer_aware_l2"],
        "run_id": "1",
        "warmup_rows_per_settlement_config": 100,
        "measured_rows_per_settlement_config": 1000,
        "warmup_rows_total": 400,
        "measured_rows_total": 4000,
        "final_csv": {
            "path": "data/e7_endtoend.csv",
            "sha256": FINAL_CSV_SHA256,
            "data_rows": 4000,
            "schema": list(FINAL_FIELDS),
        },
        "trace": {
            "path": "env/e7/trace/opsd_household_15min_e7.csv",
            "sha256": EXPECTED_TRACE_SHA256,
            "provenance_path": "env/e7/trace/provenance.json",
            "provenance_sha256": EXPECTED_TRACE_PROVENANCE_SHA256,
        },
        "funding": {
            config: {
                "duration_ns": FUNDING_DURATIONS_NS[config],
                "transaction_id": FUNDING_TRANSACTION_IDS[config],
            }
            for config in ("classical_l2", "layer_aware_l2")
        },
        "compression": {
            "format": "gzip",
            "level": 9,
            "deterministic_header": True,
            "command": "LC_ALL=C gzip -9 -n",
            "original_sha256_semantics": "SHA-256 of decompressed/original scientific evidence bytes",
            "tracked_sha256_semantics": "SHA-256 of tracked gzip container bytes",
        },
        "entries": sorted(
            entries,
            key=lambda entry: (
                str(entry["evidence_group"]), str(entry["config"]),
                str(entry["run_id"]), str(entry["original_filename"]),
            ),
        ),
    }


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())


def freeze(
    settlement_root: Path,
    funding_root: Path,
    output_root: Path,
    trace_path: Path,
    provenance_path: Path,
    final_csv_path: Path,
    project_root: Path,
) -> None:
    if settlement_root.is_symlink() or funding_root.is_symlink():
        raise ValueError("E7 source evidence roots must not be symlinks")
    settlement_root = settlement_root.absolute()
    funding_root = funding_root.absolute()
    _require_plain_inventory(settlement_root, SETTLEMENT_ARCHIVE_FILES, "settlement")
    _require_plain_inventory(funding_root, FUNDING_ARCHIVE_FILES, "funding")
    if sha256(final_csv_path.read_bytes()) != FINAL_CSV_SHA256:
        raise ValueError("existing E7 final CSV SHA-256 mismatch")
    with tempfile.TemporaryDirectory(prefix="e7-freeze-audit-") as audit_name:
        candidate = Path(audit_name) / "e7_endtoend.csv"
        finalize(
            settlement_root, funding_root, trace_path, provenance_path,
            candidate, project_root,
        )
        if candidate.read_bytes() != final_csv_path.read_bytes():
            raise ValueError("plain E7 evidence does not reproduce the final CSV")
    if output_root.exists():
        if not output_root.is_dir() or any(output_root.iterdir()):
            raise FileExistsError(f"refusing non-empty frozen E7 root: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{output_root.name}-freeze-", dir=output_root.parent
    ))
    try:
        entries: list[dict[str, object]] = []
        for group, root, inventory in (
            ("settlement", settlement_root, SETTLEMENT_ARCHIVE_FILES),
            ("funding", funding_root, FUNDING_ARCHIVE_FILES),
        ):
            for config in sorted(inventory):
                for filename in sorted(inventory[config]):
                    original = (root / config / "run_1" / filename).read_bytes()
                    compressed = deterministic_gzip(original)
                    relative = f"{group}/{config}/run_1/{filename}.gz"
                    _write_exclusive(temporary / relative, compressed)
                    entries.append({
                        "evidence_group": group,
                        "config": config,
                        "run_id": "1",
                        "original_filename": filename,
                        "original_bytes": len(original),
                        "original_sha256": hashlib.sha256(original).hexdigest(),
                        "tracked_filename": relative,
                        "tracked_bytes": len(compressed),
                        "tracked_sha256": hashlib.sha256(compressed).hexdigest(),
                        "compression": "gzip",
                    })
        manifest = (json.dumps(
            build_manifest(entries), indent=2, sort_keys=True,
        ) + "\n").encode("utf-8")
        _write_exclusive(temporary / "evidence_manifest.json", manifest)
        with tempfile.TemporaryDirectory(prefix="e7-freeze-verify-") as verify_name:
            candidate = Path(verify_name) / "e7_endtoend.csv"
            finalize_frozen(
                temporary, trace_path, provenance_path, candidate, project_root,
            )
            if candidate.read_bytes() != final_csv_path.read_bytes():
                raise ValueError("frozen E7 evidence does not reproduce the final CSV")
        if output_root.exists():
            output_root.rmdir()
        temporary.replace(output_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settlement-evidence-root", type=Path, required=True)
    parser.add_argument("--funding-evidence-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--trace-provenance", type=Path, required=True)
    parser.add_argument("--final-csv", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    arguments = parser.parse_args()
    freeze(
        arguments.settlement_evidence_root,
        arguments.funding_evidence_root,
        arguments.output_root,
        arguments.trace,
        arguments.trace_provenance,
        arguments.final_csv,
        arguments.project_root,
    )


if __name__ == "__main__":
    main()
