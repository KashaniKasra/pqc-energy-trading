#!/usr/bin/env python3
"""Run one disposable, non-scientific Android E0 integration feasibility test.

This script never invokes run_e0_android.py and never writes repository data or
raw-evidence paths. Its output is diagnostic only and must not be used in the
official E0 CSV.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from android_e0 import (
    BASELINE_FRACTION,
    DROP_THRESHOLD_PERCENT,
    MAX_SAMPLE_GAP_MULTIPLIER,
    MIN_FREQUENCY_SAMPLES,
    ROLLING_FRACTION,
    SCHEME_OPERATIONS,
    percentile,
    parse_phase_events,
    read_frequency_samples,
    read_timing_samples,
)

LABEL = "NON-SCIENTIFIC FEASIBILITY TEST — NEVER INCLUDE IN OFFICIAL E0 CSV"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEME = "ECDSA-P-256"
DEFAULT_OPERATION = "verify"
ADB_CPU_MASK = "1"
SAMPLED_CPU = 7
BENCHMARK_CPU = 7
LEGACY_FEASIBILITY_FREQUENCY_INTERVAL_MS = 1


class FeasibilityError(RuntimeError):
    pass


class Commands:
    def run(
        self, command: list[str], *, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            command,
            check=check,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def start(self, command: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@dataclass(frozen=True)
class CoverageSummary:
    measured_phase_ms: float
    timing_sample_count: int
    frequency_sample_count: int
    first_distance_ms: float
    last_distance_ms: float
    gap_median_ms: float
    gap_p95_ms: float
    gap_p99_ms: float
    gap_max_ms: float
    gaps_above_3ms: int
    frequency_mhz_median: float
    frequency_mhz_minimum: float
    frequency_mhz_maximum: float
    frequency_baseline_samples: int
    frequency_rolling_window_samples: int
    frequency_reference_mhz: float
    frequency_sustained_minimum_mhz: float
    frequency_drop_percent: float
    frequency_drop_gate_pass: bool
    coverage_gates_pass: bool


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def create_output_directory(output_parent: Path, output_dir: Path | None, token: str) -> Path:
    if output_dir is None:
        destination = output_parent.expanduser().resolve() / f"e0-android-nonscientific-feasibility-{token}"
    else:
        destination = output_dir.expanduser().resolve()
    if destination == PROJECT_ROOT or PROJECT_ROOT in destination.parents:
        raise FeasibilityError("feasibility output must be outside the repository")
    destination.mkdir(parents=True, exist_ok=False)
    _write_exclusive(destination / "NON_SCIENTIFIC.txt", (LABEL + "\n").encode())
    return destination


def resolve_home_path(value: str, home: str) -> str:
    if value == "~":
        value = home
    elif value.startswith("~/"):
        value = home.rstrip("/") + value[1:]
    if not value.startswith("/") or value.startswith("~"):
        raise FeasibilityError(f"device path must be absolute or use ~/: {value}")
    return value.rstrip("/") or "/"


def ssh_command(args: argparse.Namespace, argv: list[str]) -> list[str]:
    return [args.ssh, "-p", str(args.ssh_port), args.ssh_target, shlex.join(argv)]


def scp_from(args: argparse.Namespace, remote: str, local: Path) -> list[str]:
    return [args.scp, "-P", str(args.ssh_port), f"{args.ssh_target}:{remote}", str(local)]


def termux_environment(args: argparse.Namespace) -> list[str]:
    library = f"{args.liboqs_prefix}/lib"
    return [
        "env",
        f"PATH={args.termux_prefix}/bin",
        f"LD_LIBRARY_PATH={library}",
        f"PKG_CONFIG_PATH={library}/pkgconfig",
    ]


def sampler_argv(args: argparse.Namespace, frequency: str, ready: str, stop: str) -> list[str]:
    return [
        "taskset",
        ADB_CPU_MASK,
        args.sampler,
        "--cpu",
        str(SAMPLED_CPU),
        "--interval-ms",
        str(LEGACY_FEASIBILITY_FREQUENCY_INTERVAL_MS),
        "--output",
        frequency,
        "--ready-file",
        ready,
        "--stop-file",
        stop,
    ]


def benchmark_argv(args: argparse.Namespace, timing: str, phases: str) -> list[str]:
    return termux_environment(args) + [
        "env",
        "GOMAXPROCS=1",
        "taskset",
        "-c",
        str(BENCHMARK_CPU),
        args.benchmark,
        "android-condition",
        "--scheme",
        args.scheme,
        "--operation",
        args.operation,
        "--raw-output",
        timing,
        "--phase-output",
        phases,
        "--legacy-no-stabilization",
    ]


def adb_command(args: argparse.Namespace, *argv: str) -> list[str]:
    return [args.adb, "-s", args.adb_serial, *argv]


def wait_for_ready(
    args: argparse.Namespace,
    commands: Commands,
    sampler: subprocess.Popen[bytes],
    ready: str,
    *,
    timeout_seconds: float = 10.0,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if sampler.poll() is not None:
            raise FeasibilityError("frequency sampler exited before readiness")
        result = commands.run(adb_command(args, "shell", "test", "-f", ready), check=False)
        if result.returncode == 0:
            return
        sleep(0.05)
    raise FeasibilityError("frequency sampler readiness timeout")


def stop_and_wait_sampler(
    args: argparse.Namespace,
    commands: Commands,
    sampler: subprocess.Popen[bytes],
    stop: str,
) -> tuple[bytes, bytes]:
    commands.run(adb_command(args, "shell", "touch", stop), check=False)
    try:
        return sampler.communicate(timeout=10.0)
    except subprocess.TimeoutExpired as exc:
        sampler.terminate()
        try:
            sampler.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            sampler.kill()
            sampler.communicate()
        raise FeasibilityError("frequency sampler did not stop after stop marker") from exc


def analyze(timing_path: Path, phase_path: Path, frequency_path: Path) -> CoverageSummary:
    timings = read_timing_samples(timing_path)
    measured_start, measured_end = parse_phase_events(phase_path)
    all_frequency = read_frequency_samples(frequency_path)
    selected = [
        (timestamp, frequency)
        for timestamp, frequency in all_frequency
        if measured_start <= timestamp <= measured_end
    ]
    if not selected:
        raise FeasibilityError("no frequency samples fall inside the measured phase")
    gaps_ns = [right[0] - left[0] for left, right in zip(selected, selected[1:])]
    gap_ms = [gap / 1_000_000 for gap in gaps_ns]
    maximum_allowed_ns = LEGACY_FEASIBILITY_FREQUENCY_INTERVAL_MS * MAX_SAMPLE_GAP_MULTIPLIER * 1_000_000
    first_distance_ns = selected[0][0] - measured_start
    last_distance_ns = measured_end - selected[-1][0]
    coverage_pass = (
        len(selected) >= MIN_FREQUENCY_SAMPLES
        and first_distance_ns <= maximum_allowed_ns
        and last_distance_ns <= maximum_allowed_ns
        and all(gap <= maximum_allowed_ns for gap in gaps_ns)
    )
    frequencies_mhz = [frequency / 1000 for _, frequency in selected]
    baseline_count = max(1, math.ceil(BASELINE_FRACTION * len(frequencies_mhz)))
    rolling_count = max(3, math.ceil(ROLLING_FRACTION * len(frequencies_mhz)))
    later = frequencies_mhz[baseline_count:]
    if len(later) < rolling_count:
        raise FeasibilityError("insufficient frequency samples for sustained-drop calculation")
    reference = statistics.median(frequencies_mhz[:baseline_count])
    rolling = [
        statistics.median(later[index : index + rolling_count])
        for index in range(len(later) - rolling_count + 1)
    ]
    sustained_minimum = min(rolling)
    drop_percent = 100.0 * max(0.0, reference - sustained_minimum) / reference
    return CoverageSummary(
        measured_phase_ms=(measured_end - measured_start) / 1_000_000,
        timing_sample_count=len(timings),
        frequency_sample_count=len(selected),
        first_distance_ms=first_distance_ns / 1_000_000,
        last_distance_ms=last_distance_ns / 1_000_000,
        gap_median_ms=statistics.median(gap_ms) if gap_ms else 0.0,
        gap_p95_ms=percentile(gap_ms, 0.95) if gap_ms else 0.0,
        gap_p99_ms=percentile(gap_ms, 0.99) if gap_ms else 0.0,
        gap_max_ms=max(gap_ms, default=0.0),
        gaps_above_3ms=sum(gap > 3.0 for gap in gap_ms),
        frequency_mhz_median=statistics.median(frequencies_mhz),
        frequency_mhz_minimum=min(frequencies_mhz),
        frequency_mhz_maximum=max(frequencies_mhz),
        frequency_baseline_samples=baseline_count,
        frequency_rolling_window_samples=rolling_count,
        frequency_reference_mhz=reference,
        frequency_sustained_minimum_mhz=sustained_minimum,
        frequency_drop_percent=drop_percent,
        frequency_drop_gate_pass=drop_percent <= DROP_THRESHOLD_PERCENT,
        coverage_gates_pass=coverage_pass,
    )


def print_summary(summary: CoverageSummary, output: Path, scheme: str, operation: str) -> None:
    print(LABEL)
    print(f"output_directory: {output}")
    print(f"scheme: {scheme}")
    print(f"operation: {operation}")
    print(f"measured_phase_duration_ms: {summary.measured_phase_ms:.6f}")
    print(f"measured_timing_samples: {summary.timing_sample_count}")
    print(f"in_phase_frequency_samples: {summary.frequency_sample_count}")
    print(f"first_sample_distance_from_start_ms: {summary.first_distance_ms:.6f}")
    print(f"last_sample_distance_from_end_ms: {summary.last_distance_ms:.6f}")
    print(f"sampling_gap_median_ms: {summary.gap_median_ms:.6f}")
    print(f"sampling_gap_p95_ms: {summary.gap_p95_ms:.6f}")
    print(f"sampling_gap_p99_ms: {summary.gap_p99_ms:.6f}")
    print(f"sampling_gap_max_ms: {summary.gap_max_ms:.6f}")
    print(f"sampling_gaps_above_3ms: {summary.gaps_above_3ms}")
    print(f"loaded_frequency_mhz_median: {summary.frequency_mhz_median:.6f}")
    print(f"loaded_frequency_mhz_minimum: {summary.frequency_mhz_minimum:.6f}")
    print(f"loaded_frequency_mhz_maximum: {summary.frequency_mhz_maximum:.6f}")
    print(f"frequency_reference_mhz: {summary.frequency_reference_mhz:.6f}")
    print(f"frequency_sustained_minimum_mhz: {summary.frequency_sustained_minimum_mhz:.6f}")
    print(f"frequency_drop_percent: {summary.frequency_drop_percent:.6f}")
    print(f"telemetry_coverage_gates_pass: {str(summary.coverage_gates_pass).lower()}")
    print(f"frequency_drop_gate_pass: {str(summary.frequency_drop_gate_pass).lower()}")
    print("These diagnostics are not scientific results and must not enter the official E0 CSV.")


def _retrieve_available(
    args: argparse.Namespace,
    commands: Commands,
    remote_frequency: str,
    remote_timing: str,
    remote_phases: str,
    output: Path,
) -> None:
    frequency_result = commands.run(
        adb_command(args, "exec-out", "cat", remote_frequency), check=False
    )
    if frequency_result.returncode == 0 and not (output / "frequency_samples.csv").exists():
        _write_exclusive(output / "frequency_samples.csv", frequency_result.stdout)
    for remote, name in ((remote_timing, "timing_samples.csv"), (remote_phases, "phase_events.jsonl")):
        exists = commands.run(
            ssh_command(args, termux_environment(args) + ["test", "-f", remote]), check=False
        )
        if exists.returncode == 0 and not (output / name).exists():
            commands.run(scp_from(args, remote, output / name))


def run_feasibility(args: argparse.Namespace, commands: Commands) -> Path:
    token = uuid.uuid4().hex
    output = create_output_directory(args.output_parent, args.output_dir, token)
    adb_prefix = f"/data/local/tmp/e0-feasibility-{token}"
    remote_frequency = f"{adb_prefix}-frequency.csv"
    remote_ready = f"{adb_prefix}-ready"
    remote_stop = f"{adb_prefix}-stop"
    sampler: subprocess.Popen[bytes] | None = None
    sampler_stdout = b""
    sampler_stderr = b""
    benchmark_stdout = b""
    benchmark_stderr = b""
    status = "failed"
    failure = ""
    remote_termux_dir = ""
    termux_directory_owned = False
    adb_paths_owned = False
    try:
        commands.run(
            adb_command(args, "forward", f"tcp:{args.ssh_port}", f"tcp:{args.device_ssh_port}")
        )
        home_result = commands.run(
            ssh_command(args, ["sh", "-c", 'printf "%s\\n" "$HOME"'])
        )
        home = home_result.stdout.decode("utf-8", errors="strict").strip()
        if not home.startswith("/"):
            raise FeasibilityError("Termux HOME is not absolute")
        args.benchmark = resolve_home_path(args.benchmark, home)
        args.liboqs_prefix = resolve_home_path(args.liboqs_prefix, home)
        args.termux_prefix = resolve_home_path(args.termux_prefix, home)
        remote_termux_dir = f"{home}/.e0-nonscientific-feasibility-{token}"
        remote_timing = f"{remote_termux_dir}/timing_samples.csv"
        remote_phases = f"{remote_termux_dir}/phase_events.jsonl"

        environment = termux_environment(args)
        for check in (
            ["test", "-x", args.benchmark],
            ["test", "-r", f"{args.liboqs_prefix}/lib/pkgconfig/liboqs.pc"],
            ["pkg-config", "--modversion", "liboqs"],
        ):
            result = commands.run(ssh_command(args, environment + check))
            if check[0] == "pkg-config" and result.stdout.decode().strip() != "0.15.0":
                raise FeasibilityError("noninteractive pkg-config did not report liboqs 0.15.0")
        commands.run(ssh_command(args, environment + ["mkdir", remote_termux_dir]))
        termux_directory_owned = True
        for remote in (remote_frequency, remote_ready, remote_stop):
            result = commands.run(adb_command(args, "shell", "test", "!", "-e", remote), check=False)
            if result.returncode != 0:
                raise FeasibilityError(f"refusing existing remote path {remote}")
        adb_paths_owned = True

        sampler = commands.start(
            adb_command(args, "shell", shlex.join(sampler_argv(args, remote_frequency, remote_ready, remote_stop)))
        )
        wait_for_ready(args, commands, sampler, remote_ready)
        try:
            benchmark = commands.run(
                ssh_command(args, benchmark_argv(args, remote_timing, remote_phases)),
                check=False,
                timeout=args.benchmark_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            benchmark_stdout = exc.stdout or b""
            benchmark_stderr = exc.stderr or b""
            raise FeasibilityError("benchmark timed out") from exc
        benchmark_stdout, benchmark_stderr = benchmark.stdout, benchmark.stderr
        if benchmark.returncode != 0:
            raise FeasibilityError(f"benchmark exited with status {benchmark.returncode}")
        sampler_stdout, sampler_stderr = stop_and_wait_sampler(
            args, commands, sampler, remote_stop
        )
        if sampler.returncode != 0:
            raise FeasibilityError(f"sampler exited with status {sampler.returncode}")
        sampler = None
        _retrieve_available(
            args, commands, remote_frequency, remote_timing, remote_phases, output
        )
        summary = analyze(
            output / "timing_samples.csv",
            output / "phase_events.jsonl",
            output / "frequency_samples.csv",
        )
        summary_document = {
            "label": LABEL,
            "scientific": False,
            "official_csv_eligible": False,
            "scheme": args.scheme,
            "operation": args.operation,
            "diagnostics": asdict(summary),
        }
        _write_exclusive(
            output / "coverage_summary.json",
            (json.dumps(summary_document, indent=2, sort_keys=True) + "\n").encode(),
        )
        print_summary(summary, output, args.scheme, args.operation)
        status = "completed"
        return output
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
        print(f"{LABEL}\nFAILED: {failure}\nDiagnostics retained at: {output}", file=sys.stderr)
        raise
    finally:
        if sampler is not None and adb_paths_owned:
            try:
                sampler_stdout, sampler_stderr = stop_and_wait_sampler(
                    args, commands, sampler, remote_stop
                )
            except Exception as exc:
                sampler_stderr += (f"\ncleanup error: {exc}\n").encode()
        if remote_termux_dir and termux_directory_owned:
            remote_timing = f"{remote_termux_dir}/timing_samples.csv"
            remote_phases = f"{remote_termux_dir}/phase_events.jsonl"
            try:
                _retrieve_available(
                    args, commands, remote_frequency, remote_timing, remote_phases, output
                )
            except Exception:
                pass
            try:
                commands.run(
                    ssh_command(args, termux_environment(args) + ["rm", "-rf", remote_termux_dir]),
                    check=False,
                )
            except Exception as exc:
                sampler_stderr += (f"\nTermux cleanup error: {exc}\n").encode()
        if adb_paths_owned:
            try:
                commands.run(
                    adb_command(
                        args,
                        "shell",
                        "rm",
                        "-f",
                        remote_frequency,
                        remote_ready,
                        remote_stop,
                    ),
                    check=False,
                )
            except Exception as exc:
                sampler_stderr += (f"\nADB cleanup error: {exc}\n").encode()
        for name, content in (
            ("benchmark_stdout.log", benchmark_stdout),
            ("benchmark_stderr.log", benchmark_stderr),
            ("sampler_stdout.log", sampler_stdout),
            ("sampler_stderr.log", sampler_stderr),
        ):
            path = output / name
            if not path.exists():
                _write_exclusive(path, content)
        manifest = {
            "label": LABEL,
            "scientific": False,
            "official_csv_eligible": False,
            "status": status,
            "failure": failure,
            "scheme": args.scheme,
            "operation": args.operation,
            "warmup_iterations": 100,
            "measured_iterations": 1000,
        }
        _write_exclusive(
            output / "feasibility_manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", default=DEFAULT_SCHEME)
    parser.add_argument("--operation", default=DEFAULT_OPERATION)
    parser.add_argument("--output-parent", type=Path, default=Path("/tmp"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-serial", default="05a96d6d0405")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-target", default="u0_a21@127.0.0.1")
    parser.add_argument("--ssh-port", type=int, default=8022)
    parser.add_argument("--device-ssh-port", type=int, default=8022)
    parser.add_argument(
        "--benchmark", default="~/pqc-e0-android/android-runner/e0bench-android-v2"
    )
    parser.add_argument("--liboqs-prefix", default="~/pqc-e0-android/ops-install")
    parser.add_argument("--termux-prefix", default="/data/data/com.termux/files/usr")
    parser.add_argument("--sampler", default="/data/local/tmp/e0freq-optimized")
    parser.add_argument("--benchmark-timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.benchmark_timeout_seconds <= 0 or args.ssh_port <= 0 or args.device_ssh_port <= 0:
        parser.error("ports and benchmark timeout must be positive")
    if (args.scheme, args.operation) not in SCHEME_OPERATIONS:
        parser.error(
            f"unregistered E0 condition {args.scheme}/{args.operation}; "
            "select one of the exact 21 registered scheme/operation pairs"
        )
    return args


def main() -> None:
    run_feasibility(parse_args(), Commands())


if __name__ == "__main__":
    main()
