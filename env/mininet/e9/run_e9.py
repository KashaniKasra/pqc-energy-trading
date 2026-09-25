#!/usr/bin/env python3
"""Run one E9 Mininet condition; never runs the full sweep implicitly."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from env.mininet.e9.topology import E9LinearTopology  # noqa: E402
from src.e9.evidence import persist_condition_evidence  # noqa: E402
from src.e9.environment import (  # noqa: E402
    collect_timing_environment,
    validate_scientific_environment,
)
from src.e9.network import (  # noqa: E402
    DEFAULT_SCIENTIFIC_WARMUP_ITERATIONS,
    MESSAGE_BYTES_BY_CONFIG,
    MINIMUM_SCIENTIFIC_ITERATIONS,
    NetworkCondition,
    RawCompletionRecord,
    RunOptions,
    expected_end_to_end_rtt_ms,
    payment_channel_node_names,
    prepared_htlc_messages,
    summarize_condition,
    validate_config,
    verify_ping,
)


def validate_cli_mode(args: argparse.Namespace) -> None:
    validate_config(args.config, scientific=True)
    NetworkCondition(args.rtt_ms, args.hops).validate()
    if args.warmup < 0 or args.iterations <= 0:
        raise ValueError("warm-up must be non-negative and iterations positive")
    if args.scientific and args.iterations < MINIMUM_SCIENTIFIC_ITERATIONS:
        raise ValueError("scientific E9 runs require at least 1000 measured iterations")
    if args.preflight and args.scientific:
        raise ValueError("preflight is non-scientific and cannot use --scientific")
    if args.preflight and args.iterations > 10:
        raise ValueError("preflight is limited to at most 10 non-scientific samples")
    if not args.preflight and not args.scientific:
        raise ValueError("select exactly one of --preflight or --scientific")
    if Path(args.output_root).name == "e9_network.csv":
        raise ValueError("condition runner never writes data/e9_network.csv")


def _command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[0] if text else f"exit {result.returncode}"


def collect_environment() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {
        "software": {
            "git_commit": commit,
            "git_dirty": dirty,
            "kernel": platform.release(),
            "mininet": _command_version(["mn", "--version"]),
            "python": platform.python_version(),
            "tc": _command_version(["tc", "-V"]),
        },
        "timing": collect_timing_environment(),
    }


def _wait_ready(control_dir: Path, processes, node_count: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failed = [process.returncode for process in processes if process.poll() is not None]
        if failed:
            raise RuntimeError(f"E9 relay exited before readiness: {failed}")
        if all((control_dir / f"ready-{index}").exists() for index in range(node_count)):
            return
        time.sleep(0.05)
    raise TimeoutError("E9 relays did not become ready")


def _read_sender_records(path: Path) -> list[RawCompletionRecord]:
    records: list[RawCompletionRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        records.append(RawCompletionRecord(**json.loads(line)))
    return records


def _terminate_processes(processes) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 3.0
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
    for process in processes:
        if process.poll() is None:
            process.wait()


def run_one_condition(args: argparse.Namespace) -> None:
    validate_cli_mode(args)
    if os.geteuid() != 0:
        raise PermissionError("Mininet E9 runner requires root; invoke it with sudo env PYTHONPATH=...")
    if shutil.which("mn") is None or shutil.which("ping") is None or shutil.which("tc") is None:
        raise RuntimeError("E9 requires installed mn, ping, and tc commands")
    environment = collect_environment()
    if args.scientific:
        if environment["software"]["git_dirty"] is not False:
            raise RuntimeError("scientific E9 runs require a clean Git worktree")
        validate_scientific_environment(environment["timing"])

    # Imported lazily so unit tests never require privileged Mininet execution.
    from mininet.link import TCLink
    from mininet.net import Mininet

    condition = NetworkCondition(args.rtt_ms, args.hops)
    names = payment_channel_node_names(args.hops)
    topology = E9LinearTopology(hops=args.hops, target_rtt_ms=args.rtt_ms)
    network = Mininet(topo=topology, controller=None, link=TCLink, autoSetMacs=True)
    processes = []
    log_handles = []
    try:
        network.start()
        sender = network.get("a")
        receiver_ip = f"10.0.{args.hops}.2"
        # Connectivity warm-up is intentionally outside retained official ping evidence.
        warmup_ping = sender.cmd(f"ping -n -c 1 -W 2 {receiver_ip}")
        if " 0% packet loss" not in warmup_ping:
            raise RuntimeError(f"E9 connectivity warm-up failed:\n{warmup_ping}")
        official_ping = sender.cmd(f"ping -n -c {args.ping_samples} -W 2 {receiver_ip}")
        verification = verify_ping(args.config, condition, official_ping)
        if len(verification.samples_ms) != args.ping_samples:
            raise RuntimeError(
                "official ping did not retain every requested sample: "
                f"got {len(verification.samples_ms)}, expected {args.ping_samples}"
            )
        if not verification.passed:
            raise RuntimeError(
                "official ping failed strict <10% gate: "
                f"expected={verification.expected_end_to_end_rtt_ms:.6f} ms, "
                f"measured={verification.measured_end_to_end_rtt_ms:.6f} ms, "
                f"deviation={verification.deviation_pct:.6f}%"
            )

        with tempfile.TemporaryDirectory(prefix="e9-condition-") as temporary:
            control_dir = Path(temporary)
            ping_file = control_dir / "official-ping.txt"
            ping_file.write_text(official_ping, encoding="utf-8", newline="")
            node_program = REPO_ROOT / "env/mininet/e9/htlc_node.py"
            for index, name in enumerate(names):
                log_path = control_dir / f"node-{index}.log"
                log_handle = log_path.open("wb")
                log_handles.append(log_handle)
                command = [
                    sys.executable,
                    str(node_program),
                    "--index", str(index),
                    "--hops", str(args.hops),
                    "--config", args.config,
                    "--rtt-ms", str(args.rtt_ms),
                    "--warmup", str(args.warmup),
                    "--iterations", str(args.iterations),
                    "--control-dir", str(control_dir),
                    "--ping-file", str(ping_file),
                ]
                if args.scientific:
                    command.append("--scientific")
                processes.append(network.get(name).popen(command, stdout=log_handle, stderr=subprocess.STDOUT))
            _wait_ready(control_dir, processes, len(names))
            (control_dir / "start").touch(exist_ok=False)
            expected_seconds = (
                (args.warmup + args.iterations)
                * expected_end_to_end_rtt_ms(condition)
                / 1000.0
            )
            timeout = max(60.0, expected_seconds * 3.0 + 30.0)
            deadline = time.monotonic() + timeout
            for process in processes:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            failed = [process.returncode for process in processes if process.returncode != 0]
            records_path = control_dir / "sender-records.jsonl"
            records = _read_sender_records(records_path) if records_path.exists() else []
            failure_details = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in sorted(control_dir.glob("node-*.log"))
            )

        if args.scientific:
            manifest = None
            if records:
                manifest = persist_condition_evidence(
                    Path(args.output_root),
                    records,
                    verification,
                    RunOptions(args.warmup, args.iterations, True),
                    MESSAGE_BYTES_BY_CONFIG[args.config],
                    environment,
                )
            if failed:
                raise RuntimeError(
                    f"E9 relay process failure {failed}; "
                    f"sender evidence retained={manifest is not None}\n{failure_details}"
                )
            if not records:
                raise RuntimeError("E9 sender produced no raw completion records")
            row = summarize_condition(records, args.iterations, verification)
            print(f"manifest={manifest}")
            print(
                f"summary={row.config},rtt={row.rtt_ms},hops={row.hops},"
                f"median={row.completion_median_ms:.6f},p95={row.completion_p95_ms:.6f},"
                f"p99={row.completion_p99_ms:.6f},n={row.n_iter}"
            )
        else:
            if failed:
                raise RuntimeError(f"E9 preflight relay failure {failed}:\n{failure_details}")
            if not records:
                raise RuntimeError("E9 preflight sender produced no completion records")
            print(
                f"preflight config={args.config} rtt_ms={args.rtt_ms} hops={args.hops} "
                f"message_bytes={MESSAGE_BYTES_BY_CONFIG[args.config]}"
            )
            for record in records:
                print(
                    f"{record.phase}[{record.iteration}] completion_ms="
                    f"{record.completion_ms:.6f} success={str(record.success).lower()}"
                )
    finally:
        _terminate_processes(processes)
        for handle in log_handles:
            handle.close()
        network.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--rtt-ms", required=True, type=int)
    parser.add_argument("--hops", required=True, type=int)
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--output-root", default=str(REPO_ROOT / "raw/e9"))
    parser.add_argument("--ping-samples", type=int, default=5)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scientific", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.warmup is None:
        args.warmup = 1 if args.preflight else DEFAULT_SCIENTIFIC_WARMUP_ITERATIONS
    if args.iterations is None:
        args.iterations = 3 if args.preflight else MINIMUM_SCIENTIFIC_ITERATIONS
    return args


if __name__ == "__main__":
    run_one_condition(parse_args())
