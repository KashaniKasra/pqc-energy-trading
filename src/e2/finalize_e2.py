#!/usr/bin/env python3
"""Validate frozen E2 evidence and regenerate the professor CSV."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
from pathlib import Path


CONFIGS = ("classical", "uniform_mldsa", "layer_aware")
TRANSITIONS = (
    "channel_setup",
    "commitment_update",
    "htlc_add",
    "htlc_settle",
    "funding",
    "coop_close",
    "force_close",
    "penalty",
)
MEASUREMENT_COMMIT = "43b863e88b7bf0ba3da2a4dfa654d9f4e2b4ed37"
EXPECTED_OUTPUT_SHA256 = "0b6c9139ff5973f08373060e68c309d4d3d57d19b3ecfcc0f917eb94e2b882dd"
FINAL_FIELDS = (
    "config",
    "transition",
    "message_bytes",
    "rtt_median_ms",
    "rtt_p95_ms",
)
RAW_FIELDS = (
    "config",
    "transition",
    "iteration",
    "phase",
    "scientific",
    "message_bytes",
    "rtt_ms",
    "success",
    "error",
)
EVIDENCE_NAME = re.compile(
    r"^(manifest|samples|ping)_"
    r"(classical|uniform_mldsa|layer_aware)_"
    r"(channel_setup|commitment_update|htlc_add|htlc_settle|funding|"
    r"coop_close|force_close|penalty)_rtt20\.(json|csv|txt)$"
)
PING_TIME = re.compile(r"\btime=([0-9]+(?:\.[0-9]+)?)\s*ms\b")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def percentile(values: list[float], probability: float) -> float:
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("percentiles require finite samples")
    ordered = sorted(values)
    rank = probability * (len(ordered) - 1)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


class FrozenEvidence:
    def __init__(self, root: Path):
        self.root = root.resolve()
        manifest_path = self.root / "evidence_manifest.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.entries: dict[str, dict[str, object]] = {}
        self._validate_manifest()

    def _validate_manifest(self) -> None:
        manifest = self.manifest
        expected = {
            "schema": "pqc-energy-trading.raw-e2-evidence-manifest.v1",
            "measurement_commit": MEASUREMENT_COMMIT,
            "condition_count": 24,
            "source_file_count": 72,
            "raw_sample_rows_total": 26400,
            "warmup_rows_total": 2400,
            "measured_rows_total": 24000,
            "failure_count": 0,
            "configs": list(CONFIGS),
            "transitions": list(TRANSITIONS),
            "target_rtt_ms": 20,
            "warmup_iterations_per_condition": 100,
            "measured_iterations_per_condition": 1000,
            "ping_samples_per_condition": 20,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"evidence manifest {key} mismatch")
        compression = manifest.get("compression")
        if not isinstance(compression, dict) or (
            compression.get("format") != "gzip"
            or compression.get("level") != 9
            or compression.get("deterministic_header") is not True
            or compression.get("command") != "LC_ALL=C gzip -9 -n"
        ):
            raise ValueError("evidence manifest compression policy mismatch")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or len(entries) != 72:
            raise ValueError("evidence manifest must contain exactly 72 entries")
        if [entry.get("original_filename") for entry in entries] != sorted(
            entry.get("original_filename") for entry in entries
        ):
            raise ValueError("evidence manifest entries are not sorted")
        tracked_names: set[str] = set()
        for entry in entries:
            logical = entry.get("original_filename")
            tracked_name = entry.get("tracked_filename")
            if not isinstance(logical, str) or not EVIDENCE_NAME.fullmatch(logical):
                raise ValueError(f"invalid logical evidence filename {logical!r}")
            if logical in self.entries:
                raise ValueError(f"duplicate logical evidence filename {logical}")
            expected_tracked = f"raw/e2/{logical}.gz"
            if tracked_name != expected_tracked or tracked_name in tracked_names:
                raise ValueError(f"invalid or duplicate tracked path for {logical}")
            if entry.get("compression") != "gzip":
                raise ValueError(f"unsupported compression for {logical}")
            match = EVIDENCE_NAME.fullmatch(logical)
            assert match is not None
            role, config, transition, _ = match.groups()
            if (
                entry.get("evidence_role") != role
                or entry.get("config") != config
                or entry.get("transition") != transition
            ):
                raise ValueError(f"evidence classification mismatch for {logical}")
            tracked = self.root / Path(tracked_name).name
            compressed = tracked.read_bytes()
            if len(compressed) != entry.get("tracked_size") or sha256(compressed) != entry.get("tracked_sha256"):
                raise ValueError(f"tracked gzip identity mismatch for {logical}")
            try:
                original = gzip.decompress(compressed)
            except OSError as error:
                raise ValueError(f"invalid gzip evidence {logical}") from error
            if len(original) != entry.get("original_size") or sha256(original) != entry.get("original_sha256"):
                raise ValueError(f"original evidence identity mismatch for {logical}")
            self.entries[logical] = {**entry, "content": original}
            tracked_names.add(tracked_name)
        actual_files = {path.name for path in self.root.iterdir() if path.is_file()}
        expected_files = {Path(name).name for name in tracked_names} | {
            "evidence_manifest.json"
        }
        if actual_files != expected_files:
            raise ValueError("raw/e2 contains missing or unmanifested evidence")

    def read(self, logical: str) -> bytes:
        try:
            return self.entries[logical]["content"]  # type: ignore[return-value]
        except KeyError as error:
            raise ValueError(f"missing logical evidence {logical}") from error


def validate_ping(evidence: FrozenEvidence, condition: dict[str, object]) -> None:
    ping = condition["ping_verification"]
    if not isinstance(ping, dict):
        raise ValueError("condition ping provenance is missing")
    filename = ping.get("filename")
    if not isinstance(filename, str):
        raise ValueError("condition ping filename is missing")
    raw = evidence.read(filename)
    if sha256(raw) != ping.get("raw_output_sha256") or len(raw) != ping.get("raw_output_bytes"):
        raise ValueError(f"condition ping identity mismatch for {filename}")
    samples = [float(value) for value in PING_TIME.findall(raw.decode("utf-8"))]
    if len(samples) != 20 or samples != ping.get("samples_ms") or ping.get("sample_count") != 20:
        raise ValueError(f"condition ping samples mismatch for {filename}")
    median = percentile(samples, 0.5)
    deviation = abs(median - 20.0) / 20.0 * 100.0
    if (
        ping.get("target_rtt_ms") != 20
        or ping.get("pass") is not True
        or deviation >= 10.0
        or not math.isclose(median, float(ping.get("median_rtt_ms")), rel_tol=0, abs_tol=1e-12)
        or not math.isclose(deviation, float(ping.get("deviation_pct")), rel_tol=0, abs_tol=1e-12)
    ):
        raise ValueError(f"condition ping gate failed for {filename}")


def summarize_condition(
    evidence: FrozenEvidence,
    config: str,
    transition: str,
) -> tuple[str, str, int, float, float]:
    stem = f"{config}_{transition}_rtt20"
    condition = json.loads(evidence.read(f"manifest_{stem}.json"))
    if condition.get("schema") != "pqc-energy-trading.e2-condition-evidence.v1" or condition.get("scientific") is not True:
        raise ValueError(f"invalid scientific condition manifest for {stem}")
    details = condition.get("condition")
    software = condition.get("environment", {}).get("software", {})
    if details != {
        "config": config,
        "measured_iterations": 1000,
        "message_bytes": details.get("message_bytes") if isinstance(details, dict) else None,
        "target_rtt_ms": 20,
        "transition": transition,
        "warmup_iterations": 100,
    }:
        raise ValueError(f"condition metadata mismatch for {stem}")
    if (
        software.get("git_commit") != MEASUREMENT_COMMIT
        or software.get("git_dirty") is not False
        or software.get("mininet") != "2.3.0"
    ):
        raise ValueError(f"scientific software provenance mismatch for {stem}")
    if condition.get("ping_verification", {}).get("filename") != f"ping_{stem}.txt":
        raise ValueError(f"condition ping filename mismatch for {stem}")
    validate_ping(evidence, condition)

    samples_meta = condition.get("samples")
    if not isinstance(samples_meta, dict):
        raise ValueError(f"sample provenance missing for {stem}")
    sample_name = f"samples_{stem}.csv"
    if samples_meta.get("filename") != sample_name:
        raise ValueError(f"sample filename mismatch for {stem}")
    raw = evidence.read(sample_name)
    if sha256(raw) != samples_meta.get("sha256") or len(raw) != samples_meta.get("bytes"):
        raise ValueError(f"sample identity mismatch for {stem}")
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    if not rows or tuple(rows[0]) != RAW_FIELDS or len(rows) != 1100:
        raise ValueError(f"sample schema/count mismatch for {stem}")
    warmup = [row for row in rows if row["phase"] == "warmup"]
    measured = [row for row in rows if row["phase"] == "measured"]
    if len(warmup) != 100 or len(measured) != 1000:
        raise ValueError(f"sample phase counts mismatch for {stem}")
    if sorted(int(row["iteration"]) for row in warmup) != list(range(100)):
        raise ValueError(f"warm-up iterations incomplete for {stem}")
    if sorted(int(row["iteration"]) for row in measured) != list(range(1000)):
        raise ValueError(f"measured iterations incomplete for {stem}")
    if any(
        row["config"] != config
        or row["transition"] != transition
        or row["scientific"] != "true"
        or row["success"] != "true"
        or row["error"] != ""
        for row in rows
    ):
        raise ValueError(f"failed or inconsistent scientific sample for {stem}")
    message_sizes = {int(row["message_bytes"]) for row in rows}
    if len(message_sizes) != 1 or message_sizes != {details["message_bytes"]}:
        raise ValueError(f"message_bytes mismatch for {stem}")
    if (
        samples_meta.get("row_count") != 1100
        or samples_meta.get("warmup_count") != 100
        or samples_meta.get("measured_count") != 1000
        or samples_meta.get("failure_count") != 0
        or samples_meta.get("message_bytes_values") != [details["message_bytes"]]
    ):
        raise ValueError(f"sample manifest summary mismatch for {stem}")
    timings = [float(row["rtt_ms"]) for row in measured]
    if any(not math.isfinite(value) or value < 0 for value in timings):
        raise ValueError(f"invalid RTT sample for {stem}")
    return config, transition, message_sizes.pop(), percentile(timings, 0.5), percentile(timings, 0.95)


def finalize(evidence_root: Path, output: Path) -> None:
    evidence = FrozenEvidence(evidence_root)
    expected = {(config, transition) for config in CONFIGS for transition in TRANSITIONS}
    actual = {
        (entry["config"], entry["transition"])
        for entry in evidence.entries.values()
        if entry["evidence_role"] == "manifest"
    }
    if actual != expected:
        raise ValueError("E2 condition set is missing or contains duplicates")
    rows = [
        summarize_condition(evidence, config, transition)
        for config in CONFIGS
        for transition in TRANSITIONS
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination)
        writer.writerow(FINAL_FIELDS)
        for config, transition, message_bytes, median, p95 in rows:
            writer.writerow((config, transition, message_bytes, f"{median:.6f}", f"{p95:.6f}"))
    actual_hash = sha256(output.read_bytes())
    if actual_hash != EXPECTED_OUTPUT_SHA256:
        output.unlink(missing_ok=True)
        raise ValueError(f"regenerated E2 CSV SHA-256 mismatch: {actual_hash}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    finalize(arguments.evidence_root, arguments.output)


if __name__ == "__main__":
    main()
