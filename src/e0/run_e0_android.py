#!/usr/bin/env python3
"""Collect or resume isolated Android E0 v3 campaigns through SSH and ADB.

This program never writes the repository's canonical E0 CSV or raw/e0 tree.
New roots are exclusive; an existing root is usable only through validated
campaign resume or read-only status modes.
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
    EXPECTED_DEVICE,
    FREQUENCY_INTERVAL_MS,
    SCIENTIFIC_STABILIZATION_FREQUENCY_INTERVAL_MS,
    SCHEME_OPERATIONS,
    SCIENTIFIC_LIMITATIONS,
    EvidenceError,
    file_receipt,
    parse_current_hal_cpu_temperatures,
    sha256_file,
)
from android_e0_blocks import (
    BLOCK_ATTEMPT_SCHEMA,
    COOLDOWN_ESCALATION_SECONDS,
    block_methodology_document,
    complete_block_telemetry,
    parse_block_phases,
    read_block_frequency,
    read_block_timings,
    assess_block_frequency,
    validate_accepted_block,
    validate_rejected_block,
)
from android_e0_campaign import (
    CAMPAIGN_FILE,
    CAMPAIGN_SOURCE_PATHS,
    CampaignLock,
    atomic_json,
    build_campaign_manifest,
    completion_clock,
    cooldown_remaining_seconds,
    load_campaign,
    recover_stale_lock,
    reconstruct_campaign,
    required_resume_cooldowns,
    state_snapshot,
    validate_campaign_identity,
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
ANDROID_SOURCE_PATHS = CAMPAIGN_SOURCE_PATHS


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


def validate_existing_evidence_root(path: Path, project_root: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise CollectionError("Android campaign evidence root must not be a symlink")
    resolved = expanded.resolve()
    project = project_root.resolve()
    if resolved == project or project in resolved.parents:
        raise CollectionError("Android scientific evidence root must be outside the repository")
    if not resolved.is_dir():
        raise CollectionError(f"Android campaign evidence root does not exist: {resolved}")
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
    campaign_sequence: int | None = None,
    completion_clock_provider=None,
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
    if campaign_sequence is not None:
        if completion_clock_provider is None:
            raise CollectionError("campaign attempt requires a completion clock provider")
        manifest["campaign_context"] = {
            "schema": "pqc-energy-trading.e0-android-campaign-attempt.v1",
            "attempt_sequence": campaign_sequence,
            "completion_clock": completion_clock_provider(),
        }
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new-campaign", action="store_true")
    mode.add_argument("--resume-campaign", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--scheme")
    parser.add_argument("--operation")
    parser.add_argument("--all-conditions", action="store_true")
    parser.add_argument("--recover-stale-lock", action="store_true")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[2], type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--adb-serial")
    parser.add_argument("--ssh", default="ssh")
    parser.add_argument("--scp", default="scp")
    parser.add_argument("--ssh-target")
    parser.add_argument("--ssh-port", type=int, default=8022)
    parser.add_argument("--device-ssh-port", type=int, default=8022)
    parser.add_argument("--device-benchmark")
    parser.add_argument("--device-sampler", default="/data/local/tmp/e0freq-optimized")
    parser.add_argument("--expected-benchmark-sha256")
    parser.add_argument("--expected-sampler-sha256")
    parser.add_argument("--device-e0-module-dir")
    parser.add_argument("--device-liboqs-prefix", default="~/pqc-e0-android/ops-install")
    parser.add_argument("--device-temp-root")
    parser.add_argument("--termux-prefix", default="/data/data/com.termux/files/usr")
    parser.add_argument("--adb-temp-dir", default="/data/local/tmp")
    parser.add_argument("--sampler-cpu", type=int, default=0)
    parser.add_argument("--stabilization-port", type=int, default=37991)
    parser.add_argument("--stabilization-timeout-seconds", type=int, default=30)
    return parser.parse_args()


def _selected_conditions(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.all_conditions:
        if args.scheme is not None or args.operation is not None:
            raise CollectionError("--all-conditions cannot be combined with --scheme/--operation")
        return list(SCHEME_OPERATIONS)
    if args.scheme is None or args.operation is None:
        raise CollectionError("collection requires an explicit registered --scheme and --operation")
    condition = (args.scheme, args.operation)
    if condition not in SCHEME_OPERATIONS:
        raise CollectionError(f"unregistered Android E0 condition: {args.scheme}/{args.operation}")
    return [condition]


def _require_collection_arguments(args: argparse.Namespace) -> None:
    required = (
        "adb_serial", "ssh_target", "device_benchmark", "expected_benchmark_sha256",
        "expected_sampler_sha256", "device_e0_module_dir", "device_temp_root",
    )
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        raise CollectionError(f"collection is missing required arguments: {', '.join(missing)}")
    if args.sampler_cpu != 0:
        raise CollectionError("the registered Android E0 frequency sampler CPU is exactly 0")
    if not 1024 <= args.stabilization_port <= 65535 or args.stabilization_timeout_seconds < 3:
        raise CollectionError("invalid stabilization port or timeout")


def _ensure_no_surviving_processes(args: argparse.Namespace, commands: Commands) -> None:
    ssh_result = commands.run(
        _ssh_command(args.ssh, args.ssh_port, args.ssh_target, ["sh", "-c", "pgrep -af '[e]0bench|[e]0freq' || true"]),
        check=False,
    )
    adb_result = _adb(
        commands, args.adb, args.adb_serial, "shell", "sh", "-c",
        "pgrep -af '[e]0bench|[e]0freq' || true", check=False,
    )
    if ssh_result.returncode != 0 or adb_result.returncode != 0:
        raise CollectionError("could not inspect surviving Android E0 processes")
    if _text(ssh_result) or _text(adb_result):
        raise CollectionError("surviving Android E0 benchmark or sampler process detected")


def _android_boot_id(args: argparse.Namespace, commands: Commands) -> str:
    value = _text(_adb(commands, args.adb, args.adb_serial, "shell", "cat", "/proc/sys/kernel/random/boot_id"))
    if not value:
        raise CollectionError("Android boot identity is unavailable")
    return value


def _write_campaign_snapshot(root: Path) -> None:
    atomic_json(root / "campaign_state.json", state_snapshot(reconstruct_campaign(root)))


def _status(root: Path) -> None:
    try:
        state = reconstruct_campaign(root)
    except EvidenceError as exc:
        print(json.dumps({
            "schema": "pqc-energy-trading.e0-android-campaign-status.v1",
            "integrity": "FAIL",
            "state": "blocked",
            "error": str(exc),
        }, indent=2, sort_keys=True))
        raise
    document = state_snapshot(state)
    document["integrity"] = "PASS"
    document["writer_lock_present"] = (root / ".collection.lock").exists()
    document["provenance"] = state.campaign["provenance"]
    print(json.dumps(document, indent=2, sort_keys=True))


def _collect_condition(
    args: argparse.Namespace,
    commands: Commands,
    root: Path,
    provenance: dict[str, object],
    condition: tuple[str, str],
) -> None:
    while True:
        state = reconstruct_campaign(root)
        progress = state.conditions[condition]
        if progress.complete:
            return
        if progress.terminal_failure:
            raise CollectionError(f"{condition[0]}/{condition[1]} is terminal after three rejected attempts")

        constraints = required_resume_cooldowns(state, condition)
        host_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        android_boot = _android_boot_id(args, commands)
        now_ns = time.monotonic_ns()
        remaining = max(
            cooldown_remaining_seconds(required, clock, host_boot, android_boot, now_ns)
            for required, clock in constraints
        )
        required = max(required for required, _ in constraints)
        if remaining:
            time.sleep(remaining)
        _ensure_no_surviving_processes(args, commands)

        outcome = collect_attempt(
            args, commands, root, provenance, condition[0], condition[1],
            progress.next_block_index, progress.next_attempt_index,
            progress.remaining, progress.accepted_count, required,
            campaign_sequence=state.next_campaign_sequence,
            completion_clock_provider=lambda: completion_clock(_android_boot_id(args, commands)),
        )
        _write_campaign_snapshot(root)
        if not outcome.accepted and progress.consecutive_rejections + 1 >= 3:
            raise CollectionError(
                f"{condition[0]}/{condition[1]} stopped after three consecutive rejected attempts; "
                f"last evidence: {outcome.attempt_dir}"
            )


def main() -> None:
    args = parse_args()
    root = args.evidence_root.expanduser()
    if args.status:
        if args.scheme is not None or args.operation is not None or args.all_conditions or args.recover_stale_lock:
            raise CollectionError("--status is read-only and does not accept collection/recovery selection")
        _status(validate_existing_evidence_root(root, args.project_root))
        return

    _require_collection_arguments(args)
    conditions = _selected_conditions(args)
    commands = Commands()
    provenance = collect_static_provenance(args, commands, args.project_root)
    if args.new_campaign:
        if args.recover_stale_lock:
            raise CollectionError("a new campaign cannot recover a stale lock")
        campaign_manifest = build_campaign_manifest(
            provenance, args.project_root, ANDROID_SOURCE_PATHS
        )
        root = create_new_evidence_root(root, args.project_root)
        atomic_json(root / CAMPAIGN_FILE, campaign_manifest, exclusive=True)
        _write_campaign_snapshot(root)
    elif args.resume_campaign:
        root = validate_existing_evidence_root(root, args.project_root)
        campaign = load_campaign(root)
        validate_campaign_identity(campaign, provenance, args.project_root)
        if args.recover_stale_lock:
            _ensure_no_surviving_processes(args, commands)
            recover_stale_lock(root)
    else:
        raise CollectionError("collection mode is required")

    with CampaignLock(root):
        for condition in conditions:
            _collect_condition(args, commands, root, provenance, condition)
        _write_campaign_snapshot(root)


if __name__ == "__main__":
    main()
