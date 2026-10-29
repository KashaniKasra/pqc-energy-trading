#!/usr/bin/env python3
"""Run a two-block, non-scientific Android E0 sampler-overhead pilot.

This operational pilot is not statistically sufficient to establish negligible
overhead. It never writes canonical repository results or scientific evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from android_e0 import parse_current_hal_cpu_temperatures, parse_phase_events, read_timing_samples
from feasibility_e0_android import (
    LABEL,
    PROJECT_ROOT,
    Commands,
    adb_command,
    analyze,
    benchmark_argv,
    resolve_home_path,
    sampler_argv,
    scp_from,
    ssh_command,
    stop_and_wait_sampler,
    termux_environment,
    wait_for_ready,
)

PILOT_SCHEMA = "pqc-energy-trading.e0-android-overhead-pilot.v1"
ATTEMPT_SCHEMA = "pqc-energy-trading.e0-android-overhead-pilot-attempt.v1"
PILOT_ORDER = (("on", "off"), ("off", "on"))
DEFAULT_RECOVERY_SECONDS = 180


class PilotError(RuntimeError):
    pass


@dataclass(frozen=True)
class AttemptResult:
    block: int
    order_in_block: int
    sampler: str
    median_latency_ms: float
    measured_phase_ms: float
    attempt_directory: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def create_pilot_directory(parent: Path, explicit: Path | None, token: str) -> Path:
    destination = (
        explicit.expanduser().resolve()
        if explicit is not None
        else parent.expanduser().resolve() / f"e0-android-nonscientific-overhead-pilot-{token}"
    )
    if destination == PROJECT_ROOT or PROJECT_ROOT in destination.parents:
        raise PilotError("pilot output must be outside the repository")
    destination.mkdir(parents=True, exist_ok=False)
    write_exclusive(destination / "NON_SCIENTIFIC.txt", (LABEL + "\n").encode())
    return destination


def parse_sha256_output(output: bytes, role: str) -> str:
    fields = output.decode("utf-8", errors="strict").split()
    if not fields or len(fields[0]) != 64 or any(character not in "0123456789abcdef" for character in fields[0]):
        raise PilotError(f"invalid {role} SHA-256 output")
    return fields[0]


def relative_difference_percent(enabled: float, disabled: float) -> float:
    if enabled <= 0 or disabled <= 0:
        raise PilotError("paired effects require positive measurements")
    return 100.0 * (enabled - disabled) / disabled


def paired_effects(results: list[AttemptResult]) -> list[dict[str, object]]:
    effects: list[dict[str, object]] = []
    for block in range(1, len(PILOT_ORDER) + 1):
        members = [result for result in results if result.block == block]
        if len(members) != 2 or {member.sampler for member in members} != {"on", "off"}:
            raise PilotError(f"block {block} does not contain exactly one sampler-on and sampler-off attempt")
        enabled = next(member for member in members if member.sampler == "on")
        disabled = next(member for member in members if member.sampler == "off")
        effects.append(
            {
                "block": block,
                "execution_order": [member.sampler for member in sorted(members, key=lambda item: item.order_in_block)],
                "sampler_on_attempt": enabled.attempt_directory,
                "sampler_off_attempt": disabled.attempt_directory,
                "median_latency_on_ms": format(enabled.median_latency_ms, ".9f"),
                "median_latency_off_ms": format(disabled.median_latency_ms, ".9f"),
                "median_latency_relative_difference_percent": format(
                    relative_difference_percent(enabled.median_latency_ms, disabled.median_latency_ms), ".9f"
                ),
                "measured_phase_on_ms": format(enabled.measured_phase_ms, ".9f"),
                "measured_phase_off_ms": format(disabled.measured_phase_ms, ".9f"),
                "measured_phase_relative_difference_percent": format(
                    relative_difference_percent(enabled.measured_phase_ms, disabled.measured_phase_ms), ".9f"
                ),
            }
        )
    return effects


def collect_environment(args: argparse.Namespace, commands: Commands) -> dict[str, object]:
    commands.run(adb_command(args, "forward", f"tcp:{args.ssh_port}", f"tcp:{args.device_ssh_port}"))
    home_result = commands.run(ssh_command(args, ["sh", "-c", 'printf "%s\\n" "$HOME"']))
    home = home_result.stdout.decode("utf-8", errors="strict").strip()
    if not home.startswith("/"):
        raise PilotError("Termux HOME is not absolute")
    args.benchmark = resolve_home_path(args.benchmark, home)
    args.liboqs_prefix = resolve_home_path(args.liboqs_prefix, home)
    args.termux_prefix = resolve_home_path(args.termux_prefix, home)
    args.termux_home = home
    environment = termux_environment(args)

    def adb_text(*command: str) -> str:
        return commands.run(adb_command(args, *command)).stdout.decode("utf-8", errors="strict").strip()

    def ssh_result(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        return commands.run(ssh_command(args, environment + command))

    for command in (
        ["test", "-x", args.benchmark],
        ["test", "-r", f"{args.liboqs_prefix}/lib/pkgconfig/liboqs.pc"],
    ):
        ssh_result(command)
    liboqs = ssh_result(["pkg-config", "--modversion", "liboqs"]).stdout.decode().strip()
    if liboqs != "0.15.0":
        raise PilotError(f"unexpected liboqs version {liboqs!r}")
    return {
        "label": LABEL,
        "scientific": False,
        "adb_serial": args.adb_serial,
        "ssh_target": args.ssh_target,
        "model": adb_text("shell", "getprop", "ro.product.model"),
        "android": adb_text("shell", "getprop", "ro.build.version.release"),
        "architecture": adb_text("shell", "uname", "-m"),
        "cpu7_governor": adb_text(
            "shell", "cat", "/sys/devices/system/cpu/cpu7/cpufreq/scaling_governor"
        ),
        "cpu7_max_khz": int(
            adb_text("shell", "cat", "/sys/devices/system/cpu/cpu7/cpufreq/cpuinfo_max_freq")
        ),
        "go_version": ssh_result(["go", "version"]).stdout.decode().strip(),
        "liboqs_version": liboqs,
        "benchmark_path": args.benchmark,
        "benchmark_sha256": parse_sha256_output(
            ssh_result(["sha256sum", args.benchmark]).stdout, "benchmark"
        ),
        "sampler_path": args.sampler,
        "sampler_sha256": parse_sha256_output(
            commands.run(adb_command(args, "shell", "sha256sum", args.sampler)).stdout, "sampler"
        ),
        "benchmark_cpu": 7,
        "sampler_cpu": 0,
        "gomaxprocs": 1,
    }


def retrieve_termux_file(
    args: argparse.Namespace, commands: Commands, remote: str, local: Path
) -> None:
    exists = commands.run(
        ssh_command(args, termux_environment(args) + ["test", "-f", remote]), check=False
    )
    if exists.returncode == 0 and not local.exists():
        commands.run(scp_from(args, remote, local))


def unavailable_telemetry() -> dict[str, str]:
    return {
        "availability": "unavailable",
        "reason": "sampler intentionally disabled for paired overhead pilot",
    }


def cleanup_remote(
    args: argparse.Namespace,
    commands: Commands,
    *,
    remote_dir: str,
    remote_dir_owned: bool,
    frequency: str,
    ready: str,
    stop: str,
    adb_paths_owned: bool,
) -> None:
    if remote_dir_owned:
        commands.run(
            ssh_command(args, termux_environment(args) + ["rm", "-rf", remote_dir]), check=False
        )
    if adb_paths_owned:
        commands.run(
            adb_command(args, "shell", "rm", "-f", frequency, ready, stop), check=False
        )


def collect_attempt(
    args: argparse.Namespace,
    commands: Commands,
    pilot: Path,
    block: int,
    order_in_block: int,
    sampler_mode: str,
) -> AttemptResult:
    attempt = pilot / f"block_{block:02d}" / f"order_{order_in_block:02d}_sampler_{sampler_mode}"
    attempt.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    adb_prefix = f"/data/local/tmp/e0-overhead-pilot-{token}"
    remote_frequency = f"{adb_prefix}-frequency.csv"
    remote_ready = f"{adb_prefix}-ready"
    remote_stop = f"{adb_prefix}-stop"
    home = args.termux_home
    remote_dir = f"{home}/.e0-overhead-pilot-{token}"
    remote_timing = f"{remote_dir}/timing_samples.csv"
    remote_phases = f"{remote_dir}/phase_events.jsonl"
    sampler: subprocess.Popen[bytes] | None = None
    sampler_stdout = b""
    sampler_stderr = b""
    benchmark_stdout = b""
    benchmark_stderr = b""
    remote_dir_owned = False
    adb_paths_owned = False
    status = "failed"
    failure = ""
    started_at = ""
    finished_at = ""
    try:
        before = commands.run(adb_command(args, "shell", "dumpsys", "thermalservice")).stdout
        write_exclusive(attempt / "temperature_before.txt", before)
        before_cpu = parse_current_hal_cpu_temperatures(before.decode("utf-8", errors="strict"))
        commands.run(ssh_command(args, termux_environment(args) + ["mkdir", remote_dir]))
        remote_dir_owned = True
        if sampler_mode == "on":
            for remote in (remote_frequency, remote_ready, remote_stop):
                result = commands.run(adb_command(args, "shell", "test", "!", "-e", remote), check=False)
                if result.returncode != 0:
                    raise PilotError(f"refusing existing remote path {remote}")
            adb_paths_owned = True
            sampler = commands.start(
                adb_command(
                    args,
                    "shell",
                    shlex.join(sampler_argv(args, remote_frequency, remote_ready, remote_stop)),
                )
            )
            wait_for_ready(args, commands, sampler, remote_ready)

        started_at = utc_now()
        try:
            benchmark = commands.run(
                ssh_command(args, benchmark_argv(args, remote_timing, remote_phases)),
                check=False,
                timeout=args.benchmark_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            benchmark_stdout = exc.stdout or b""
            benchmark_stderr = exc.stderr or b""
            finished_at = utc_now()
            raise PilotError("benchmark timed out") from exc
        finished_at = utc_now()
        benchmark_stdout, benchmark_stderr = benchmark.stdout, benchmark.stderr
        if benchmark.returncode != 0:
            raise PilotError(f"benchmark exited with status {benchmark.returncode}")
        if sampler is not None:
            sampler_stdout, sampler_stderr = stop_and_wait_sampler(args, commands, sampler, remote_stop)
            if sampler.returncode != 0:
                raise PilotError(f"sampler exited with status {sampler.returncode}")
            sampler = None

        after = commands.run(adb_command(args, "shell", "dumpsys", "thermalservice")).stdout
        write_exclusive(attempt / "temperature_after.txt", after)
        after_cpu = parse_current_hal_cpu_temperatures(after.decode("utf-8", errors="strict"))
        retrieve_termux_file(args, commands, remote_timing, attempt / "timing_samples.csv")
        retrieve_termux_file(args, commands, remote_phases, attempt / "phase_events.jsonl")
        if sampler_mode == "on":
            result = commands.run(adb_command(args, "exec-out", "cat", remote_frequency))
            write_exclusive(attempt / "frequency_samples.csv", result.stdout)
        timings = read_timing_samples(attempt / "timing_samples.csv")
        phase_start, phase_end = parse_phase_events(attempt / "phase_events.jsonl")
        phase_ms = (phase_end - phase_start) / 1_000_000
        telemetry: dict[str, object]
        if sampler_mode == "on":
            telemetry_summary = analyze(
                attempt / "timing_samples.csv",
                attempt / "phase_events.jsonl",
                attempt / "frequency_samples.csv",
            )
            telemetry = {"availability": "available", **asdict(telemetry_summary)}
        else:
            telemetry = unavailable_telemetry()
        status = "completed"
        manifest = {
            "schema": ATTEMPT_SCHEMA,
            "label": LABEL,
            "scientific": False,
            "official_csv_eligible": False,
            "status": status,
            "block": block,
            "order_in_block": order_in_block,
            "sampler": sampler_mode,
            "scheme": args.scheme,
            "operation": args.operation,
            "warmup_iterations": 100,
            "measured_iterations": 1000,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "median_latency_ms": format(statistics.median(timings), ".9f"),
            "measured_phase_ms": format(phase_ms, ".9f"),
            "temperature_before_cpu_c": [format(value, ".9f") for value in before_cpu],
            "temperature_after_cpu_c": [format(value, ".9f") for value in after_cpu],
            "telemetry": telemetry,
        }
        write_exclusive(
            attempt / "attempt_manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
        return AttemptResult(
            block=block,
            order_in_block=order_in_block,
            sampler=sampler_mode,
            median_latency_ms=statistics.median(timings),
            measured_phase_ms=phase_ms,
            attempt_directory=attempt.relative_to(pilot).as_posix(),
        )
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
        raise
    finally:
        if sampler is not None and adb_paths_owned:
            try:
                sampler_stdout, sampler_stderr = stop_and_wait_sampler(args, commands, sampler, remote_stop)
            except Exception as exc:
                sampler_stderr += f"\ncleanup error: {exc}\n".encode()
        for remote, name in (
            (remote_timing, "timing_samples.csv"),
            (remote_phases, "phase_events.jsonl"),
        ):
            try:
                retrieve_termux_file(args, commands, remote, attempt / name)
            except Exception:
                pass
        if sampler_mode == "on" and adb_paths_owned and not (attempt / "frequency_samples.csv").exists():
            try:
                result = commands.run(adb_command(args, "exec-out", "cat", remote_frequency), check=False)
                if result.returncode == 0:
                    write_exclusive(attempt / "frequency_samples.csv", result.stdout)
            except Exception:
                pass
        if not (attempt / "temperature_after.txt").exists():
            try:
                after = commands.run(adb_command(args, "shell", "dumpsys", "thermalservice")).stdout
                write_exclusive(attempt / "temperature_after.txt", after)
            except Exception:
                pass
        try:
            cleanup_remote(
                args,
                commands,
                remote_dir=remote_dir,
                remote_dir_owned=remote_dir_owned,
                frequency=remote_frequency,
                ready=remote_ready,
                stop=remote_stop,
                adb_paths_owned=adb_paths_owned,
            )
        except Exception as exc:
            sampler_stderr += f"\ncleanup error: {exc}\n".encode()
        for name, content in (
            ("benchmark_stdout.log", benchmark_stdout),
            ("benchmark_stderr.log", benchmark_stderr),
            ("sampler_stdout.log", sampler_stdout),
            ("sampler_stderr.log", sampler_stderr),
        ):
            if not (attempt / name).exists():
                write_exclusive(attempt / name, content)
        if status != "completed" and not (attempt / "attempt_manifest.json").exists():
            write_exclusive(
                attempt / "attempt_manifest.json",
                (
                    json.dumps(
                        {
                            "schema": ATTEMPT_SCHEMA,
                            "label": LABEL,
                            "scientific": False,
                            "official_csv_eligible": False,
                            "status": "failed",
                            "failure": failure,
                            "block": block,
                            "order_in_block": order_in_block,
                            "sampler": sampler_mode,
                            "scheme": args.scheme,
                            "operation": args.operation,
                            "started_at_utc": started_at,
                            "finished_at_utc": finished_at,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )


def run_pilot(
    args: argparse.Namespace,
    commands: Commands,
    *,
    attempt_runner: Callable[..., AttemptResult] = collect_attempt,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    token = uuid.uuid4().hex
    pilot = create_pilot_directory(args.output_parent, args.output_dir, token)
    environment = collect_environment(args, commands)
    environment["recovery_interval_seconds"] = args.recovery_seconds
    environment["counterbalanced_order"] = [list(block) for block in PILOT_ORDER]
    write_exclusive(
        pilot / "environment.json",
        (json.dumps(environment, indent=2, sort_keys=True) + "\n").encode(),
    )
    results: list[AttemptResult] = []
    sequence = [(block, order, mode) for block, modes in enumerate(PILOT_ORDER, 1) for order, mode in enumerate(modes, 1)]
    for sequence_index, (block, order, mode) in enumerate(sequence):
        results.append(attempt_runner(args, commands, pilot, block, order, mode))
        if sequence_index != len(sequence) - 1:
            sleep(args.recovery_seconds)
    summary = {
        "schema": PILOT_SCHEMA,
        "label": LABEL,
        "scientific": False,
        "official_csv_eligible": False,
        "interpretation": "two-block operational pilot; not statistically sufficient to establish negligible overhead",
        "scheme": args.scheme,
        "operation": args.operation,
        "paired_blocks": len(PILOT_ORDER),
        "recovery_interval_seconds": args.recovery_seconds,
        "attempts": [asdict(result) for result in results],
        "paired_effects": paired_effects(results),
        "overhead_subtracted_from_official_results": False,
    }
    write_exclusive(
        pilot / "pilot_summary.json",
        (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode(),
    )
    print(LABEL)
    print(f"output_directory: {pilot}")
    for effect in summary["paired_effects"]:
        print(
            f"block={effect['block']} order={','.join(effect['execution_order'])} "
            f"latency_difference_percent={effect['median_latency_relative_difference_percent']} "
            f"phase_difference_percent={effect['measured_phase_relative_difference_percent']}"
        )
    print("Two paired blocks are operational diagnostics, not a definitive overhead estimate.")
    return pilot


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-parent", type=Path, default=Path("/tmp"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--recovery-seconds", type=int, default=DEFAULT_RECOVERY_SECONDS)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-serial", default="05a96d6d0405")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-target", default="u0_a21@127.0.0.1")
    parser.add_argument("--ssh-port", type=int, default=8022)
    parser.add_argument("--device-ssh-port", type=int, default=8022)
    parser.add_argument("--benchmark", default="~/pqc-e0-android/android-runner/e0bench-android-v2")
    parser.add_argument("--liboqs-prefix", default="~/pqc-e0-android/ops-install")
    parser.add_argument("--termux-prefix", default="/data/data/com.termux/files/usr")
    parser.add_argument("--sampler", default="/data/local/tmp/e0freq-optimized")
    parser.add_argument("--benchmark-timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    args.scheme = "ECDSA-P-256"
    args.operation = "keygen"
    if args.recovery_seconds <= 0 or args.benchmark_timeout_seconds <= 0:
        parser.error("recovery interval and timeout must be positive")
    return args


def main() -> None:
    run_pilot(parse_args(), Commands())


if __name__ == "__main__":
    main()
