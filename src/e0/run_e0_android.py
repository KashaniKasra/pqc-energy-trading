#!/usr/bin/env python3
"""Collect isolated Android E0 attempts through Termux SSH and ADB.

This program never writes the repository's canonical E0 CSV or raw/e0 tree.
Run it only with a new external evidence root. It deliberately has no mode that
can reuse or overwrite an earlier root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from android_e0 import (
    COOLDOWN_SECONDS,
    EXPECTED_DEVICE,
    FREQUENCY_INTERVAL_MS,
    SCIENTIFIC_STABILIZATION_FREQUENCY_INTERVAL_MS,
    SCHEME_OPERATIONS,
    SCIENTIFIC_LIMITATIONS,
    file_receipt,
    parse_current_hal_cpu_temperatures,
    sha256_file,
)
from android_e0_blocks import (
    BLOCK_ATTEMPT_SCHEMA,
    COOLDOWN_ESCALATION_SECONDS,
    ConditionProgress,
    block_methodology_document,
    complete_block_telemetry,
    parse_block_phases,
    read_block_frequency,
    read_block_timings,
    assess_block_frequency,
    validate_accepted_block,
    validate_rejected_block,
)


class CollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlockAttemptOutcome:
    accepted: bool
    completed_timings: int
    reason: str
    attempt_dir: Path


ADB_SAMPLER_CPU_MASK = "1"
ANDROID_SOURCE_PATHS = (
    "src/e0/android_condition.go",
    "src/e0/android_e0.py",
    "src/e0/android_e0_blocks.py",
    "src/e0/android_e0_reporting.py",
    "src/e0/android_stabilization.go",
    "src/e0/cmd/e0freq/main.go",
    "src/e0/finalize_e0_android.py",
    "src/e0/main.go",
    "src/e0/monotonic.go",
    "src/e0/run_e0_android.py",
    "src/e0/timing.go",
)


class Commands:
    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(command, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def start(self, command: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def _atomic_exclusive_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def create_new_evidence_root(path: Path, project_root: Path) -> Path:
    resolved = path.expanduser().resolve()
    project = project_root.resolve()
    if resolved == project or project in resolved.parents:
        raise CollectionError("Android scientific evidence root must be outside the repository")
    try:
        resolved.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise CollectionError(f"refusing existing evidence root {resolved}") from exc
    return resolved


def create_attempt_directory(root: Path, scheme: str, operation: str, attempt: int) -> Path:
    safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", scheme)
    path = root / safe_scheme / operation / f"attempt_{attempt:03d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def create_block_attempt_directory(
    root: Path, scheme: str, operation: str, block_index: int, attempt_index: int
) -> Path:
    if block_index <= 0 or attempt_index <= 0:
        raise CollectionError("block and attempt indices must be positive")
    safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", scheme)
    path = root / safe_scheme / operation / f"block_{block_index:03d}" / f"attempt_{attempt_index:03d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _adb(
    commands: Commands, executable: str, serial: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return commands.run([executable, "-s", serial, *args], check=check)


def _ssh_command(executable: str, port: int, target: str, argv: list[str]) -> list[str]:
    return [executable, "-p", str(port), target, shlex.join(argv)]


def _scp_from(executable: str, port: int, target: str, remote: str, local: Path) -> list[str]:
    return [executable, "-P", str(port), f"{target}:{remote}", str(local)]


def _resolve_termux_path(value: str, home: str) -> str:
    if value == "~":
        value = home
    elif value.startswith("~/"):
        value = home.rstrip("/") + value[1:]
    elif value.startswith("~"):
        raise CollectionError("~user device paths are not supported")
    if not value.startswith("/"):
        raise CollectionError(f"device path must be absolute or start with ~/: {value}")
    return value.rstrip("/") or "/"


def _termux_environment(args: argparse.Namespace) -> list[str]:
    lib = f"{args.device_liboqs_prefix}/lib"
    command = [
        "env",
        f"PATH={args.termux_prefix}/bin",
        f"LD_LIBRARY_PATH={lib}",
        f"PKG_CONFIG_PATH={lib}/pkgconfig",
    ]
    return command


def _termux_argv(args: argparse.Namespace, argv: list[str]) -> list[str]:
    return _termux_environment(args) + argv


def _sampler_argv(
    args: argparse.Namespace,
    frequency_remote: str,
    ready_remote: str,
    stop_remote: str,
    stabilization_token: str | None = None,
) -> list[str]:
    # Android's Toybox taskset accepts a hexadecimal mask, not GNU -c syntax.
    # Mask 1 pins the sampler process to CPU 0; --cpu 7 selects the separate
    # big-core scaling_cur_freq attribute that the sampler observes.
    command = [
        "taskset",
        ADB_SAMPLER_CPU_MASK,
        args.device_sampler,
        "--cpu",
        "7",
        "--interval-ms",
        str(
            SCIENTIFIC_STABILIZATION_FREQUENCY_INTERVAL_MS
            if stabilization_token is not None
            else FREQUENCY_INTERVAL_MS
        ),
        "--output",
        frequency_remote,
        "--ready-file",
        ready_remote,
        "--stop-file",
        stop_remote,
    ]
    if stabilization_token is not None:
        command.extend(
            [
                "--stabilization-listen",
                f"127.0.0.1:{args.stabilization_port}",
                "--stabilization-token",
                stabilization_token,
                "--stabilization-timeout",
                f"{args.stabilization_timeout_seconds}s",
            ]
        )
    return command


def _benchmark_argv(
    args: argparse.Namespace,
    scheme: str,
    operation: str,
    timing_remote: str,
    phase_remote: str,
    schedule_remote: str,
    token: str,
    requested_iterations: int,
) -> list[str]:
    return [
        "env",
        "GOMAXPROCS=1",
        "taskset",
        "-c",
        "7",
        args.device_benchmark,
        "android-condition",
        "--scheme",
        scheme,
        "--operation",
        operation,
        "--raw-output",
        timing_remote,
        "--phase-output",
        phase_remote,
        "--schedule-output",
        schedule_remote,
        "--stabilization-address",
        f"127.0.0.1:{args.stabilization_port}",
        "--stabilization-token",
        token,
        "--stabilization-timeout",
        f"{args.stabilization_timeout_seconds}s",
        "--requested-iterations",
        str(requested_iterations),
        "--block-start-budget",
        "59s",
    ]


def _validate_deployed_binary_hash(label: str, expected: str, actual: str) -> None:
    for name, value in (("expected", expected), ("actual", actual)):
        if re.fullmatch(r"[0-9a-f]{64}", value or "") is None:
            raise CollectionError(f"invalid {name} {label} SHA-256")
    if actual != expected:
        raise CollectionError(f"deployed {label} SHA-256 does not match the approved fresh build")


def _validate_android_go_version(value: str) -> None:
    if value != "go version go1.27.3 android/arm64":
        raise CollectionError(f"unexpected Go version: {value}")


def _validate_git_provenance(commands: Commands, project_root: Path) -> str:
    if commands.run(["git", "-C", str(project_root), "diff", "--quiet"], check=False).returncode != 0 or commands.run(
        ["git", "-C", str(project_root), "diff", "--cached", "--quiet"], check=False
    ).returncode != 0:
        raise CollectionError("tracked Git tree must be clean")
    tracked = commands.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", "--", *ANDROID_SOURCE_PATHS],
        check=False,
    )
    if tracked.returncode != 0:
        raise CollectionError("Android E0 scientific source files must be committed before collection")
    untracked_result = commands.run(
        ["git", "-C", str(project_root), "ls-files", "--others", "--exclude-standard", "--", "src/e0"],
        check=False,
    )
    if untracked_result.returncode != 0:
        raise CollectionError("could not inspect untracked E0 source files")
    untracked_e0 = _text(untracked_result)
    if untracked_e0:
        raise CollectionError(f"untracked E0 source files must be committed or removed: {untracked_e0}")
    commit = _text(commands.run(["git", "-C", str(project_root), "rev-parse", "HEAD"]))
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise CollectionError("invalid Git commit")
    return commit


def _wait_for_sampler(process: subprocess.Popen[bytes], timeout: float = 10.0) -> tuple[bytes, bytes]:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise CollectionError("frequency sampler did not stop after stop marker") from exc


def collect_static_provenance(args: argparse.Namespace, commands: Commands, project_root: Path) -> dict[str, object]:
    commit = _validate_git_provenance(commands, project_root)
    _adb(
        commands,
        args.adb,
        args.adb_serial,
        "forward",
        f"tcp:{args.ssh_port}",
        f"tcp:{args.device_ssh_port}",
    )

    properties = {
        "model": _text(_adb(commands, args.adb, args.adb_serial, "shell", "getprop", "ro.product.model")),
        "android": _text(_adb(commands, args.adb, args.adb_serial, "shell", "getprop", "ro.build.version.release")),
        "architecture": _text(_adb(commands, args.adb, args.adb_serial, "shell", "uname", "-m")),
        "governor": _text(
            _adb(commands, args.adb, args.adb_serial, "shell", "cat", "/sys/devices/system/cpu/cpu7/cpufreq/scaling_governor")
        ),
        "cpu_max_khz": int(
            _text(_adb(commands, args.adb, args.adb_serial, "shell", "cat", "/sys/devices/system/cpu/cpu7/cpufreq/cpuinfo_max_freq"))
        ),
    }
    expected = {
        "model": EXPECTED_DEVICE["model"],
        "android": EXPECTED_DEVICE["android"],
        "architecture": EXPECTED_DEVICE["architecture"],
        "governor": EXPECTED_DEVICE["governor"],
        "cpu_max_khz": EXPECTED_DEVICE["selected_cpu_max_mhz"] * 1000,
    }
    if properties != expected:
        raise CollectionError(f"Android device preflight mismatch: {properties!r}")

    raw_ssh = lambda argv: commands.run(_ssh_command(args.ssh, args.ssh_port, args.ssh_target, argv))
    termux_home = _text(raw_ssh(["sh", "-c", 'printf "%s\\n" "$HOME"']))
    if not termux_home.startswith("/"):
        raise CollectionError("noninteractive Termux SSH did not provide an absolute HOME")
    for name in ("device_benchmark", "device_e0_module_dir", "device_liboqs_prefix", "device_temp_root", "termux_prefix"):
        setattr(args, name, _resolve_termux_path(getattr(args, name), termux_home))
    ssh = lambda argv: commands.run(
        _ssh_command(args.ssh, args.ssh_port, args.ssh_target, _termux_argv(args, argv))
    )
    for path in (args.device_benchmark, args.device_e0_module_dir, args.device_liboqs_prefix, args.device_temp_root):
        ssh(["test", "-e", path])
    ssh(["test", "-x", args.device_benchmark])
    ssh(["test", "-r", f"{args.device_liboqs_prefix}/lib/pkgconfig/liboqs.pc"])
    go_version = _text(ssh(["go", "version"]))
    _validate_android_go_version(go_version)
    if _text(ssh(["pkg-config", "--modversion", "liboqs"])) != "0.15.0":
        raise CollectionError("unexpected liboqs version")
    liboqs_go = _text(
        ssh(
            [
                "sh",
                "-c",
                f"cd {shlex.quote(args.device_e0_module_dir)} && go list -m -f '{{{{.Version}}}}' github.com/open-quantum-safe/liboqs-go",
            ]
        )
    )
    if liboqs_go != "v0.15.0":
        raise CollectionError(f"unexpected liboqs-go version: {liboqs_go}")
    if int(_text(ssh(["id", "-u"]))) == 0:
        raise CollectionError("Termux benchmark user must be non-root")

    benchmark_hash = _text(ssh(["sha256sum", args.device_benchmark])).split()[0]
    sampler_hash = _text(_adb(commands, args.adb, args.adb_serial, "shell", "sha256sum", args.device_sampler)).split()[0]
    _validate_deployed_binary_hash("benchmark", args.expected_benchmark_sha256, benchmark_hash)
    _validate_deployed_binary_hash("frequency sampler", args.expected_sampler_sha256, sampler_hash)
    return {
        "git_commit": commit,
        "git_tracked_clean": True,
        "benchmark_sha256": benchmark_hash,
        "frequency_sampler_sha256": sampler_hash,
        "go_version_output": go_version,
        "governor": properties["governor"],
        "cpu_max_khz": properties["cpu_max_khz"],
        "benchmark_transport": "Termux SSH",
        "telemetry_transport": "ADB; frequency sampled on-device and retrieved after the measured run",
        "adb_serial": args.adb_serial,
        "scientific_limitations": SCIENTIFIC_LIMITATIONS,
    }


def _write_bytes(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def collect_attempt(
    args: argparse.Namespace,
    commands: Commands,
    root: Path,
    provenance: dict[str, object],
    scheme: str,
    operation: str,
    block_index: int,
    attempt_index: int,
    requested_iterations: int,
    accepted_sample_start: int,
    cooldown_preceding_seconds: int,
) -> BlockAttemptOutcome:
    attempt_dir = create_block_attempt_directory(root, scheme, operation, block_index, attempt_index)
    token = uuid.uuid4().hex
    adb_prefix = f"{args.adb_temp_dir.rstrip('/')}/e0-{token}"
    frequency_remote = f"{adb_prefix}-frequency.csv"
    ready_remote = f"{adb_prefix}-ready"
    stop_remote = f"{adb_prefix}-stop"
    termux_dir = f"{args.device_temp_root.rstrip('/')}/e0-{token}"
    timing_remote = f"{termux_dir}/timing_samples.csv"
    phase_remote = f"{termux_dir}/phase_events.jsonl"
    schedule_remote = f"{termux_dir}/timed_schedule.csv"
    local_paths = {
        "timing_samples": attempt_dir / "timing_samples.csv",
        "phase_events": attempt_dir / "phase_events.jsonl",
        "timed_schedule": attempt_dir / "timed_schedule.csv",
        "frequency_samples": attempt_dir / "frequency_samples.csv",
        "temperature_before": attempt_dir / "temperature_before.txt",
        "temperature_after": attempt_dir / "temperature_after.txt",
        "stdout": attempt_dir / "stdout.log",
        "stderr": attempt_dir / "stderr.log",
        "sampler_stdout": attempt_dir / "sampler_stdout.log",
        "sampler_stderr": attempt_dir / "sampler_stderr.log",
    }
    status = "rejected"
    reason = "collection did not complete"
    sampler_process: subprocess.Popen[bytes] | None = None
    sampler_stdout = b""
    sampler_stderr = b""
    telemetry: dict[str, object] = {}
    completed_timings = 0
    fatal: BaseException | None = None
    try:
        before = _adb(commands, args.adb, args.adb_serial, "shell", "dumpsys", "thermalservice").stdout
        _write_bytes(local_paths["temperature_before"], before)
        parse_current_hal_cpu_temperatures(before.decode("utf-8", errors="strict"))

        _adb(commands, args.adb, args.adb_serial, "shell", "test", "!", "-e", frequency_remote)
        _adb(commands, args.adb, args.adb_serial, "shell", "test", "!", "-e", ready_remote)
        _adb(commands, args.adb, args.adb_serial, "shell", "test", "!", "-e", stop_remote)
        sampler_argv = _sampler_argv(args, frequency_remote, ready_remote, stop_remote, token)
        sampler_process = commands.start([args.adb, "-s", args.adb_serial, "shell", shlex.join(sampler_argv)])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _adb(commands, args.adb, args.adb_serial, "shell", "test", "-f", ready_remote, check=False).returncode == 0:
                break
            time.sleep(0.05)
        else:
            raise CollectionError("frequency sampler readiness timeout")

        ssh = lambda argv, check=True: commands.run(
            _ssh_command(args.ssh, args.ssh_port, args.ssh_target, _termux_argv(args, argv)), check=check
        )
        ssh(["mkdir", termux_dir])
        benchmark = ssh(
            _benchmark_argv(
                args, scheme, operation, timing_remote, phase_remote, schedule_remote, token, requested_iterations
            ),
            check=False,
        )
        _write_bytes(local_paths["stdout"], benchmark.stdout)
        _write_bytes(local_paths["stderr"], benchmark.stderr)
        if benchmark.returncode != 0:
            raise CollectionError(f"benchmark exited {benchmark.returncode}")
        _adb(commands, args.adb, args.adb_serial, "shell", "touch", stop_remote)
        after = _adb(commands, args.adb, args.adb_serial, "shell", "dumpsys", "thermalservice").stdout
        _write_bytes(local_paths["temperature_after"], after)
        commands.run(_scp_from(args.scp, args.ssh_port, args.ssh_target, timing_remote, local_paths["timing_samples"]))
        commands.run(_scp_from(args.scp, args.ssh_port, args.ssh_target, phase_remote, local_paths["phase_events"]))
        commands.run(_scp_from(args.scp, args.ssh_port, args.ssh_target, schedule_remote, local_paths["timed_schedule"]))
        sampler_stdout, sampler_stderr = _wait_for_sampler(sampler_process)
        if sampler_process.returncode != 0:
            raise CollectionError(
                f"frequency sampler failed: {(sampler_stdout + sampler_stderr).decode('utf-8', errors='replace')}"
            )
        sampler_process = None
        frequency = _adb(commands, args.adb, args.adb_serial, "exec-out", "cat", frequency_remote).stdout
        _write_bytes(local_paths["frequency_samples"], frequency)

        timings = read_block_timings(local_paths["timing_samples"])
        completed_timings = len(timings)
        phases = parse_block_phases(local_paths["phase_events"])
        assessment = assess_block_frequency(
            read_block_frequency(local_paths["frequency_samples"]),
            phases.measured_start_ns,
            phases.measured_end_ns,
        )
        before_values = parse_current_hal_cpu_temperatures(before.decode("utf-8", errors="strict"))
        after_values = parse_current_hal_cpu_temperatures(after.decode("utf-8", errors="strict"))
        telemetry = complete_block_telemetry(assessment, before_values, after_values, phases)
        if assessment.accepted:
            status = "accepted"
            reason = ""
        else:
            reason = "block rejected because B < 0.9 * A"
    except BaseException as exc:  # every started attempt must retain failure evidence
        reason = str(exc) or type(exc).__name__
        if not isinstance(exc, Exception):
            fatal = exc
        if not local_paths["stderr"].exists():
            _write_bytes(local_paths["stderr"], (reason + "\n").encode("utf-8"))
        if not local_paths["stdout"].exists():
            _write_bytes(local_paths["stdout"], b"")
        if not local_paths["temperature_after"].exists():
            try:
                _write_bytes(
                    local_paths["temperature_after"],
                    _adb(commands, args.adb, args.adb_serial, "shell", "dumpsys", "thermalservice").stdout,
                )
            except Exception:
                pass
    finally:
        try:
            _adb(commands, args.adb, args.adb_serial, "shell", "touch", stop_remote, check=False)
            if sampler_process is not None:
                sampler_stdout, sampler_stderr = _wait_for_sampler(sampler_process)
        except Exception:
            pass
        # Stabilization failures occur before timing_samples.csv is created,
        # but their phase and raw sampler diagnostics remain scientifically
        # relevant. Retrieve every available collision-safe artifact before
        # removing the uniquely owned device paths.
        if not local_paths["frequency_samples"].exists():
            try:
                result = _adb(
                    commands, args.adb, args.adb_serial, "exec-out", "cat", frequency_remote, check=False
                )
                if result.returncode == 0:
                    _write_bytes(local_paths["frequency_samples"], result.stdout)
            except Exception:
                pass
        for remote, role in (
            (timing_remote, "timing_samples"),
            (phase_remote, "phase_events"),
            (schedule_remote, "timed_schedule"),
        ):
            if not local_paths[role].exists():
                try:
                    commands.run(
                        _scp_from(args.scp, args.ssh_port, args.ssh_target, remote, local_paths[role]),
                        check=False,
                    )
                except Exception:
                    pass
        try:
            _adb(
                commands,
                args.adb,
                args.adb_serial,
                "shell",
                "rm",
                "-f",
                frequency_remote,
                ready_remote,
                stop_remote,
                check=False,
            )
        except Exception:
            pass
        try:
            commands.run(
                _ssh_command(
                    args.ssh,
                    args.ssh_port,
                    args.ssh_target,
                    _termux_argv(args, ["rm", "-rf", termux_dir]),
                ),
                check=False,
            )
        except Exception:
            pass

    for role, content in (("sampler_stdout", sampler_stdout), ("sampler_stderr", sampler_stderr)):
        if not local_paths[role].exists():
            _write_bytes(local_paths[role], content)

    if completed_timings == 0 and local_paths["timing_samples"].exists():
        try:
            completed_timings = len(read_block_timings(local_paths["timing_samples"]))
        except Exception:
            pass
    files = {
        role: file_receipt(path, attempt_dir)
        for role, path in local_paths.items()
        if path.exists() and path.is_file() and not path.is_symlink()
    }
    manifest: dict[str, object] = {
        "schema": BLOCK_ATTEMPT_SCHEMA,
        "scientific": True,
        "status": status,
        "scheme": scheme,
        "operation": operation,
        "block_index": block_index,
        "attempt_index": attempt_index,
        "requested_timing_iterations": requested_iterations,
        "completed_timing_iterations": completed_timings,
        "accepted_timing_iterations": completed_timings if status == "accepted" else 0,
        "accepted_sample_start": accepted_sample_start if status == "accepted" else None,
        "accepted_sample_end_exclusive": (
            accepted_sample_start + completed_timings if status == "accepted" else None
        ),
        "cooldown_preceding_seconds": cooldown_preceding_seconds,
        "cooldown_escalation_seconds": list(COOLDOWN_ESCALATION_SECONDS),
        "normal_interblock_cooldown_seconds": 180,
        "warmup_iterations": 100,
        "methodology": block_methodology_document(args.stabilization_timeout_seconds),
        "device": EXPECTED_DEVICE,
        "provenance": provenance,
        "files": files,
        "telemetry": telemetry,
    }
    if status == "rejected":
        manifest["rejection_reason"] = reason
    _atomic_exclusive_json(attempt_dir / "manifest.json", manifest)
    if status == "accepted":
        validate_accepted_block(attempt_dir, manifest)
    else:
        validate_rejected_block(attempt_dir, manifest)
    if fatal is not None:
        raise fatal
    return BlockAttemptOutcome(status == "accepted", completed_timings, reason, attempt_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[2], type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-serial", required=True)
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-port", type=int, default=8022)
    parser.add_argument("--device-ssh-port", type=int, default=8022)
    parser.add_argument("--device-benchmark", required=True)
    parser.add_argument("--device-sampler", default="/data/local/tmp/e0freq-optimized")
    parser.add_argument("--expected-benchmark-sha256", required=True)
    parser.add_argument("--expected-sampler-sha256", required=True)
    parser.add_argument("--device-e0-module-dir", required=True)
    parser.add_argument("--device-liboqs-prefix", default="~/pqc-e0-android/ops-install")
    parser.add_argument("--device-temp-root", required=True)
    parser.add_argument("--termux-prefix", default="/data/data/com.termux/files/usr")
    parser.add_argument("--adb-temp-dir", default="/data/local/tmp")
    parser.add_argument("--sampler-cpu", type=int, default=0)
    parser.add_argument("--stabilization-port", type=int, default=37991)
    parser.add_argument("--stabilization-timeout-seconds", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sampler_cpu != 0:
        raise CollectionError("the registered Android E0 frequency sampler CPU is exactly 0")
    if not 1024 <= args.stabilization_port <= 65535 or args.stabilization_timeout_seconds < 3:
        raise CollectionError("invalid stabilization port or timeout")
    commands = Commands()
    provenance = collect_static_provenance(args, commands, args.project_root)
    root = create_new_evidence_root(args.evidence_root, args.project_root)
    previous_condition_completed = False
    for scheme, operation in SCHEME_OPERATIONS:
        initial_cooldown = 0
        if previous_condition_completed:
            time.sleep(COOLDOWN_SECONDS)
            initial_cooldown = COOLDOWN_SECONDS
        progress = ConditionProgress()
        cooldown_before = initial_cooldown
        attempt_index = 1
        while progress.accepted_count < 1000:
            outcome = collect_attempt(
                args,
                commands,
                root,
                provenance,
                scheme,
                operation,
                progress.block_index,
                attempt_index,
                progress.requested_iterations(),
                progress.accepted_count,
                cooldown_before,
            )
            if outcome.accepted:
                cooldown = progress.accept(outcome.completed_timings)
                attempt_index = 1
            else:
                cooldown = progress.reject()
                attempt_index += 1
            if progress.terminal_failure:
                raise CollectionError(
                    f"{scheme}/{operation} stopped after three consecutive rejected attempts; "
                    f"last evidence: {outcome.attempt_dir}"
                )
            if cooldown is not None:
                time.sleep(cooldown)
                cooldown_before = cooldown
        previous_condition_completed = True


if __name__ == "__main__":
    main()
