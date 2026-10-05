#!/usr/bin/env python3
"""Derive the deterministic E6 key-aggregation size ablation from final E2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
from pathlib import Path
import tempfile


E2_SHA256 = "0b6c9139ff5973f08373060e68c309d4d3d57d19b3ecfcc0f917eb94e2b882dd"
E2_FIELDS = (
    "config",
    "transition",
    "message_bytes",
    "rtt_median_ms",
    "rtt_p95_ms",
)
E6_FIELDS = ("scheme", "aggregated", "k_sig", "k_pk", "tx_bytes")
EXPECTED_FUNDING_BYTES = {
    "classical": 222,
    "uniform_mldsa": 10648,
}
E6_ROWS = (
    ("classical", "true", 1, 1, 222),
    ("ML-DSA-65", "false", 2, 2, 10648),
)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _extract_funding_sizes(
    e2_csv: Path,
    *,
    expected_sha256: str,
) -> dict[str, int]:
    """Validate final E2 and return its two authoritative funding sizes."""
    try:
        content = e2_csv.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read final E2 CSV: {e2_csv}") from error
    actual_sha256 = sha256(content)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"final E2 CSV SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("invalid final E2 CSV encoding or syntax") from error
    if tuple(reader.fieldnames or ()) != E2_FIELDS:
        raise ValueError("final E2 CSV schema mismatch")

    funding_sizes: dict[str, int] = {}
    for config, expected_bytes in EXPECTED_FUNDING_BYTES.items():
        matches = [
            row
            for row in rows
            if row.get("config") == config and row.get("transition") == "funding"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"final E2 CSV requires exactly one {config} funding row; found {len(matches)}"
            )
        raw_bytes = matches[0].get("message_bytes", "")
        try:
            message_bytes = int(raw_bytes)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{config} funding message_bytes must be an integer") from error
        if message_bytes <= 0:
            raise ValueError(f"{config} funding message_bytes must be positive")
        if message_bytes != expected_bytes:
            raise ValueError(
                f"{config} funding message_bytes mismatch: "
                f"expected {expected_bytes}, got {message_bytes}"
            )
        funding_sizes[config] = message_bytes
    return funding_sizes


def extract_funding_sizes(e2_csv: Path) -> dict[str, int]:
    """Extract funding sizes only from the immutable authoritative E2 CSV."""
    return _extract_funding_sizes(e2_csv, expected_sha256=E2_SHA256)


def build_e6_rows(funding_sizes: dict[str, int]) -> tuple[tuple[object, ...], ...]:
    rows = (
        ("classical", "true", 1, 1, funding_sizes["classical"]),
        ("ML-DSA-65", "false", 2, 2, funding_sizes["uniform_mldsa"]),
    )
    if rows != E6_ROWS:
        raise ValueError("derived E6 row values or order do not match the registered ablation")
    return rows


def render_e6(rows: tuple[tuple[object, ...], ...]) -> bytes:
    if rows != E6_ROWS:
        raise ValueError("E6 output must contain exactly the two authoritative ordered rows")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(E6_FIELDS)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def finalize(
    e2_csv: Path,
    output: Path,
) -> None:
    funding_sizes = extract_funding_sizes(e2_csv)
    content = render_e6(build_e6_rows(funding_sizes))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e2-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    finalize(arguments.e2_csv, arguments.output)


if __name__ == "__main__":
    main()
