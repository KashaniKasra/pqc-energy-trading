#!/usr/bin/env python3
"""Run one E2 config/transition RTT condition; never emits the final CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from env.mininet.e2.topology import E2RTTTopology, TARGET_RTT_MS  # noqa: E402
from src.e9.environment import (  # noqa: E402
    collect_timing_environment,
    validate_scientific_environment,
)


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
MINIMUM_SCIENTIFIC_ITERATIONS = 1000
DEFAULT_WARMUP_ITERATIONS = 100
PING_RE = re.compile(r"\btime=([0-9]+(?:\.[0-9]+)?)\s*ms\b")
MININET_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-+~.A-Za-z0-9]*)?\b")
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


def percentile(values: list[float], probability: float) -> float:
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("finite ping samples are required")
    ordered = sorted(values)
    rank = probability * (len(ordered) - 1)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


def verify_ping(raw_output: str, expected_samples: int) -> dict[str, object]:
    samples = [float(value) for value in PING_RE.findall(raw_output)]
    if len(samples) != expected_samples:
        raise ValueError(f"official ping samples = {len(samples)}, want {expected_samples}")
    measured = percentile(samples, 0.5)
    deviation = abs(measured - TARGET_RTT_MS) / TARGET_RTT_MS * 100.0
    raw_bytes = raw_output.encode("utf-8")
    return {
        "target_rtt_ms": TARGET_RTT_MS,
        "sample_count": len(samples),
        "samples_ms": samples,
        "median_rtt_ms": measured,
        "deviation_pct": deviation,
        "pass": deviation < 10.0,
        "raw_output_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_output_bytes": len(raw_bytes),
    }


def validate_args(args: argparse.Namespace) -> None:
    if args.config not in CONFIGS or args.transition not in TRANSITIONS:
        raise ValueError("unsupported E2 config or transition")
    if args.warmup < 0 or args.iterations <= 0:
        raise ValueError("invalid E2 iteration counts")
    if args.ping_samples <= 0:
        raise ValueError("official ping sample count must be positive")
    if args.scientific and args.iterations < MINIMUM_SCIENTIFIC_ITERATIONS:
        raise ValueError("scientific E2 requires at least 1000 measured iterations")
    if args.preflight and args.iterations > 10:
        raise ValueError("E2 preflight is limited to at most 10 measured iterations")
    if args.preflight == args.scientific:
        raise ValueError("select exactly one of --preflight or --scientific")
    if Path(args.output_root).name == "e2_statemachine.csv":
        raise ValueError("condition runner never writes data/e2_statemachine.csv")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mininet_version(run_command=subprocess.run) -> str:
    """Return mn's reported version, including installations that use stderr."""
    completed = run_command(
        ["mn", "--version"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot determine Mininet version: mn --version exited {completed.returncode}"
        )
    combined = "\n".join((completed.stdout or "", completed.stderr or ""))
    matches = MININET_VERSION_RE.findall(combined)
    if not matches:
        raise RuntimeError("cannot determine Mininet version: mn --version was empty")
    return matches[-1]


def validate_scientific_software_provenance(environment: dict[str, object]) -> None:
    software = environment.get("software")
    if not isinstance(software, dict):
        raise RuntimeError("scientific E2 software provenance is missing")
    mininet_version = software.get("mininet")
    if not isinstance(mininet_version, str) or not mininet_version.strip():
        raise RuntimeError("scientific E2 requires a detected Mininet version")


def _environment() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True, text=True,
        capture_output=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, check=True, text=True,
        capture_output=True,
    ).stdout)
    return {
        "software": {
            "git_commit": commit,
            "git_dirty": dirty,
            "go": subprocess.run(["go", "version"], check=True, text=True, capture_output=True).stdout.strip(),
            "kernel": platform.release(),
            "mininet": detect_mininet_version(),
            "python": platform.python_version(),
            "tc": subprocess.run(["tc", "-V"], check=True, text=True, capture_output=True).stdout.strip(),
        },
        "timing": collect_timing_environment(),
    }


def _wait_ready(path: Path, process, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError(f"E2 verification server exited early with {process.returncode}")
        time.sleep(0.05)
    raise TimeoutError("E2 verification server did not become ready")


def _audit_raw(
    path: Path,
    args: argparse.Namespace,
    *,
    enforce_scientific_completeness: bool = True,
) -> dict[str, object]:
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    if not rows or tuple(rows[0]) != RAW_FIELDS:
        raise ValueError("invalid E2 raw CSV schema")
    expected = args.warmup + args.iterations
    if len(rows) != expected:
        raise ValueError(f"E2 raw row count = {len(rows)}, want {expected}")
    for row in rows:
        if row["config"] != args.config or row["transition"] != args.transition:
            raise ValueError("E2 raw condition mismatch")
    measured = [row for row in rows if row["phase"] == "measured"]
    warmup = [row for row in rows if row["phase"] == "warmup"]
    failures = [row for row in rows if row["success"] != "true"]
    message_sizes = sorted({int(row["message_bytes"]) for row in measured if row["success"] == "true"})
    result = {
        "row_count": len(rows),
        "warmup_count": len(warmup),
        "measured_count": len(measured),
        "failure_count": len(failures),
        "message_bytes_values": message_sizes,
    }
    if args.scientific and enforce_scientific_completeness:
        if len(measured) != args.iterations or len(warmup) != args.warmup or failures:
            raise ValueError("scientific E2 raw evidence is incomplete or contains failures")
        if any(row["scientific"] != "true" for row in rows):
            raise ValueError("scientific E2 raw evidence has mixed provenance")
        if len(message_sizes) != 1:
            raise ValueError("scientific E2 message_bytes are inconsistent")
        if sorted(int(row["iteration"]) for row in measured) != list(range(args.iterations)):
            raise ValueError("scientific E2 measured iterations are missing or duplicated")
    return result


def _persist(
    output_root: Path,
    args: argparse.Namespace,
    ping_output: str,
    ping: dict[str, object],
    raw_temp: Path,
    audit: dict[str, object],
    environment: dict[str, object],
    binary: Path,
) -> Path:
    stem = f"{args.config}_{args.transition}_rtt{TARGET_RTT_MS}"
    ping_path = output_root / f"ping_{stem}.txt"
    raw_path = output_root / f"samples_{stem}.csv"
    manifest_path = output_root / f"manifest_{stem}.json"
    if any(path.exists() for path in (ping_path, raw_path, manifest_path)):
        raise FileExistsError(f"E2 evidence collision for {stem}")
    output_root.mkdir(parents=True, exist_ok=True)
    ping_bytes = ping_output.encode("utf-8")
    if (
        hashlib.sha256(ping_bytes).hexdigest() != ping["raw_output_sha256"]
        or len(ping_bytes) != ping["raw_output_bytes"]
    ):
        raise ValueError("official E2 ping output does not match its provenance")
    raw_bytes = raw_temp.read_bytes()
    manifest = {
        "schema": "pqc-energy-trading.e2-condition-evidence.v1",
        "scientific": True,
        "condition": {
            "config": args.config,
            "transition": args.transition,
            "target_rtt_ms": TARGET_RTT_MS,
            "warmup_iterations": args.warmup,
            "measured_iterations": args.iterations,
            "message_bytes": (
                audit["message_bytes_values"][0]
                if len(audit["message_bytes_values"]) == 1 else None
            ),
        },
        "ping_verification": {
            **ping,
            "filename": ping_path.name,
        },
        "samples": {
            **audit,
            "filename": raw_path.name,
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        "runner": {"binary_sha256": _sha256(binary)},
        "environment": environment,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    written: list[Path] = []
    try:
        for path, content in (
            (ping_path, ping_bytes),
            (raw_path, raw_bytes),
            (manifest_path, manifest_bytes),
        ):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
            written.append(path)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return manifest_path


def run(args: argparse.Namespace) -> None:
    validate_args(args)
    if os.geteuid() != 0:
        raise PermissionError("E2 Mininet runner requires root")
    for command in ("mn", "tc", "ping", "go"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command not found: {command}")
    environment = _environment()
    if args.scientific:
        if environment["software"]["git_dirty"] is not False:
            raise RuntimeError("scientific E2 requires a clean Git worktree")
        validate_scientific_software_provenance(environment)
        validate_scientific_environment(environment["timing"])

    from mininet.link import TCLink
    from mininet.net import Mininet

    network = Mininet(topo=E2RTTTopology(), controller=None, link=TCLink, autoSetMacs=True)
    processes = []
    handles = []
    try:
        with tempfile.TemporaryDirectory(prefix="e2-rtt-") as temporary:
            temporary_root = Path(temporary)
            binary = temporary_root / "e2rtt"
            subprocess.run(
                ["go", "build", "-o", str(binary), "./cmd/e2rtt"],
                cwd=REPO_ROOT / "src/e2", check=True,
            )
            network.start()
            node_a, node_b = network.get("a", "b")
            warmup_ping = node_a.cmd("ping -n -c 1 -W 2 10.20.0.2")
            if " 0% packet loss" not in warmup_ping:
                raise RuntimeError(f"E2 connectivity warm-up failed:\n{warmup_ping}")
            official_ping = node_a.cmd(f"ping -n -c {args.ping_samples} -W 2 10.20.0.2")
            ping = verify_ping(official_ping, args.ping_samples)
            if not ping["pass"]:
                raise RuntimeError(f"E2 ping gate failed: {ping['deviation_pct']:.6f}%")

            ready = temporary_root / "server.ready"
            raw_temp = temporary_root / "samples.csv"
            server_log = (temporary_root / "server.log").open("wb")
            client_log = (temporary_root / "client.log").open("wb")
            handles.extend((server_log, client_log))
            common = [
                "--config", args.config,
                "--transition", args.transition,
                "--warmup", str(args.warmup),
                "--iterations", str(args.iterations),
            ]
            server = node_b.popen(
                [str(binary), "server", *common, "--listen", "10.20.0.2:19220", "--ready-file", str(ready)],
                stdout=server_log, stderr=subprocess.STDOUT,
            )
            processes.append(server)
            _wait_ready(ready, server)
            client_command = [
                str(binary), "client", *common,
                "--connect", "10.20.0.2:19220",
                "--source", "10.20.0.1",
                "--output", str(raw_temp),
            ]
            if args.scientific:
                client_command.append("--scientific")
            client = node_a.popen(client_command, stdout=client_log, stderr=subprocess.STDOUT)
            processes.append(client)
            timeout = max(60.0, (args.warmup + args.iterations) * 0.25 + 30.0)
            client.wait(timeout=timeout)
            server.wait(timeout=30)
            if client.returncode != 0 or server.returncode != 0:
                server_log.flush()
                client_log.flush()
                raise RuntimeError(
                    "E2 RTT process failure:\n"
                    + Path(server_log.name).read_text(errors="replace")
                    + "\n"
                    + Path(client_log.name).read_text(errors="replace")
                )
            audit = _audit_raw(
                raw_temp,
                args,
                enforce_scientific_completeness=False,
            )
            if args.scientific:
                manifest = _persist(
                    Path(args.output_root), args, official_ping, ping, raw_temp,
                    audit, environment, binary,
                )
                print(f"manifest={manifest}")
                # Freeze every structurally valid scientific attempt before
                # deciding whether it is eligible for final summarization.
                # Failed samples therefore remain auditable, but fail closed.
                _audit_raw(raw_temp, args, enforce_scientific_completeness=True)
            else:
                print(json.dumps({"ping": ping, "samples": audit}, sort_keys=True))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in handles:
            handle.close()
        network.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, choices=CONFIGS)
    parser.add_argument("--transition", required=True, choices=TRANSITIONS)
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--ping-samples", type=int, default=20)
    parser.add_argument("--output-root", default=str(REPO_ROOT / "raw/e2"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--scientific", action="store_true")
    args = parser.parse_args()
    if args.warmup is None:
        args.warmup = 1 if args.preflight else DEFAULT_WARMUP_ITERATIONS
    if args.iterations is None:
        args.iterations = 3 if args.preflight else MINIMUM_SCIENTIFIC_ITERATIONS
    return args


if __name__ == "__main__":
    run(parse_args())
