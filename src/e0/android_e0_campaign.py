#!/usr/bin/env python3
"""Crash-safe campaign reconstruction for Android E0 v3 collection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from android_e0 import EXPECTED_DEVICE, SCHEME_OPERATIONS, EvidenceError
from android_e0_blocks import (
    COOLDOWN_ESCALATION_SECONDS,
    NORMAL_INTERBLOCK_COOLDOWN_SECONDS,
    TARGET_ACCEPTED_TIMINGS,
    validate_accepted_block,
    validate_rejected_block,
)

CAMPAIGN_SCHEMA = "pqc-energy-trading.e0-android-campaign.v1"
CAMPAIGN_ATTEMPT_SCHEMA = "pqc-energy-trading.e0-android-campaign-attempt.v1"
LOCK_SCHEMA = "pqc-energy-trading.e0-android-campaign-lock.v1"
CAMPAIGN_FILE = "campaign.json"
LOCK_FILE = ".collection.lock"
CAMPAIGN_SOURCE_PATHS = (
    "src/e0/android_condition.go",
    "src/e0/android_e0.py",
    "src/e0/android_e0_blocks.py",
    "src/e0/android_e0_campaign.py",
    "src/e0/android_e0_reporting.py",
    "src/e0/android_stabilization.go",
    "src/e0/cmd/e0freq/main.go",
    "src/e0/ecdsa_benchmark.go",
    "src/e0/finalize_e0_android.py",
    "src/e0/go.mod",
    "src/e0/go.sum",
    "src/e0/main.go",
    "src/e0/monotonic.go",
    "src/e0/oqs_benchmark.go",
    "src/e0/output.go",
    "src/e0/run_e0_android.py",
    "src/e0/stats.go",
    "src/e0/timing.go",
)


@dataclass(frozen=True)
class ConditionCampaignState:
    scheme: str
    operation: str
    accepted_count: int
    accepted_blocks: int
    rejected_attempts: int
    consecutive_rejections: int
    next_block_index: int
    next_attempt_index: int
    previous_status: str | None
    terminal_failure: bool
    complete: bool
    last_campaign_sequence: int | None
    last_completion_clock: dict[str, object] | None

    @property
    def remaining(self) -> int:
        return TARGET_ACCEPTED_TIMINGS - self.accepted_count


@dataclass(frozen=True)
class CampaignReconstruction:
    campaign: dict[str, object]
    conditions: dict[tuple[str, str], ConditionCampaignState]
    next_campaign_sequence: int
    last_global_condition: tuple[str, str] | None
    last_global_status: str | None
    last_global_completion_clock: dict[str, object] | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if exclusive:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_hashes(project_root: Path, relative_paths: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    root = project_root.resolve()
    for relative_text in relative_paths:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise EvidenceError("unsafe campaign source path")
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise EvidenceError(f"missing campaign source file: {relative_text}")
        result[relative_text] = _sha256(path)
    return result


def build_campaign_manifest(
    provenance: dict[str, object], project_root: Path, source_paths: tuple[str, ...]
) -> dict[str, object]:
    if source_paths != CAMPAIGN_SOURCE_PATHS:
        raise EvidenceError("campaign source provenance set is not canonical")
    return {
        "schema": CAMPAIGN_SCHEMA,
        "experiment": "E0-Android-v3",
        "scientific": True,
        "registered_conditions": [list(item) for item in SCHEME_OPERATIONS],
        "target_accepted_timings_per_condition": TARGET_ACCEPTED_TIMINGS,
        "device": EXPECTED_DEVICE,
        "provenance": provenance,
        "campaign_manager": "src/e0/run_e0_android.py",
        "source_sha256": source_hashes(project_root, source_paths),
    }


def load_campaign(root: Path) -> dict[str, object]:
    path = root / CAMPAIGN_FILE
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("campaign manifest must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid campaign manifest: {exc}") from exc
    required = {
        "schema", "experiment", "scientific", "registered_conditions",
        "target_accepted_timings_per_condition", "device", "provenance",
        "campaign_manager", "source_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise EvidenceError("invalid campaign manifest fields")
    if (
        value["schema"] != CAMPAIGN_SCHEMA
        or value["experiment"] != "E0-Android-v3"
        or value["scientific"] is not True
        or value["registered_conditions"] != [list(item) for item in SCHEME_OPERATIONS]
        or value["target_accepted_timings_per_condition"] != TARGET_ACCEPTED_TIMINGS
        or value["device"] != EXPECTED_DEVICE
        or value["campaign_manager"] != "src/e0/run_e0_android.py"
        or not isinstance(value["provenance"], dict)
        or not isinstance(value["source_sha256"], dict)
    ):
        raise EvidenceError("campaign identity or methodology mismatch")
    hashes = value["source_sha256"]
    if set(hashes) != set(CAMPAIGN_SOURCE_PATHS) or any(
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in hashes.values()
    ):
        raise EvidenceError("campaign source provenance set is incomplete or malformed")
    return value


def validate_campaign_identity(
    campaign: dict[str, object], provenance: dict[str, object], project_root: Path
) -> None:
    if campaign["provenance"] != provenance:
        raise EvidenceError("campaign provenance differs from the current source, device, or binaries")
    recorded = campaign["source_sha256"]
    if recorded != source_hashes(project_root, CAMPAIGN_SOURCE_PATHS):
        raise EvidenceError("campaign source hashes differ from current committed files")


def validate_campaign_attempt_context(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"schema", "attempt_sequence", "completion_clock"}:
        raise EvidenceError("missing or invalid campaign attempt context")
    if value["schema"] != CAMPAIGN_ATTEMPT_SCHEMA or not isinstance(value["attempt_sequence"], int) or value["attempt_sequence"] <= 0:
        raise EvidenceError("invalid campaign attempt identity")
    clock = value["completion_clock"]
    required_clock = {"host_boot_id", "host_monotonic_ns", "android_boot_id", "wall_time_utc"}
    if not isinstance(clock, dict) or set(clock) != required_clock:
        raise EvidenceError("invalid campaign completion clock")
    if (
        not isinstance(clock["host_boot_id"], str) or not clock["host_boot_id"]
        or not isinstance(clock["android_boot_id"], str) or not clock["android_boot_id"]
        or not isinstance(clock["host_monotonic_ns"], int) or clock["host_monotonic_ns"] <= 0
        or not isinstance(clock["wall_time_utc"], str)
    ):
        raise EvidenceError("malformed campaign completion clock")
    try:
        datetime.fromisoformat(clock["wall_time_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("invalid campaign completion wall time") from exc
    return value


def completion_clock(android_boot_id: str) -> dict[str, object]:
    host_boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not host_boot or not android_boot_id:
        raise EvidenceError("boot identity is unavailable")
    return {
        "host_boot_id": host_boot,
        "host_monotonic_ns": time.monotonic_ns(),
        "android_boot_id": android_boot_id,
        "wall_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def cooldown_remaining_seconds(
    required_seconds: int,
    recorded_clock: dict[str, object] | None,
    current_host_boot_id: str | None,
    current_android_boot_id: str | None,
    current_host_monotonic_ns: int | None,
) -> int:
    if required_seconds <= 0:
        return 0
    if (
        recorded_clock is None
        or recorded_clock.get("host_boot_id") != current_host_boot_id
        or recorded_clock.get("android_boot_id") != current_android_boot_id
        or not isinstance(current_host_monotonic_ns, int)
        or not isinstance(recorded_clock.get("host_monotonic_ns"), int)
        or current_host_monotonic_ns < recorded_clock["host_monotonic_ns"]
    ):
        return required_seconds
    elapsed_ns = current_host_monotonic_ns - recorded_clock["host_monotonic_ns"]
    elapsed_seconds = elapsed_ns // 1_000_000_000
    return max(0, required_seconds - elapsed_seconds)


def _safe_condition_name(scheme: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", scheme)


def _validate_attempt_inventory(attempt_dir: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise EvidenceError("campaign attempt is missing its evidence inventory")
    expected = {"manifest.json"}
    for receipt in files.values():
        if not isinstance(receipt, dict) or set(receipt) != {"path", "bytes", "sha256"}:
            raise EvidenceError("invalid campaign attempt file receipt")
        relative = Path(str(receipt["path"]))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise EvidenceError("unsafe campaign attempt evidence path")
        expected.add(relative.as_posix())
    actual: set[str] = set()
    for path in attempt_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise EvidenceError("campaign attempt contains unsafe or nested evidence")
        actual.add(path.name)
    if actual != expected:
        raise EvidenceError("campaign attempt file inventory mismatch")


def reconstruct_campaign(
    root: Path,
    *,
    accepted_validator: Callable[[Path, dict[str, object]], tuple[list[float], object]] = validate_accepted_block,
    rejected_validator: Callable[[Path, dict[str, object]], None] = validate_rejected_block,
) -> CampaignReconstruction:
    campaign = load_campaign(root)
    condition_states: dict[tuple[str, str], ConditionCampaignState] = {}
    global_attempts: list[
        tuple[int, tuple[str, str], str, int, dict[str, object]]
    ] = []
    known_top = {CAMPAIGN_FILE, LOCK_FILE, "campaign_state.json", "lock_recoveries"} | {
        _safe_condition_name(scheme) for scheme, _ in SCHEME_OPERATIONS
    }
    extras = {path.name for path in root.iterdir()} - known_top
    if extras:
        raise EvidenceError(f"unknown campaign entries: {sorted(extras)}")
    allowed_operations: dict[str, set[str]] = {}
    for scheme, operation in SCHEME_OPERATIONS:
        allowed_operations.setdefault(_safe_condition_name(scheme), set()).add(operation)
    for safe_scheme, operations in allowed_operations.items():
        scheme_dir = root / safe_scheme
        if not scheme_dir.exists():
            continue
        if not scheme_dir.is_dir() or scheme_dir.is_symlink():
            raise EvidenceError(f"unsafe campaign scheme directory: {scheme_dir}")
        unexpected = {path.name for path in scheme_dir.iterdir()} - operations
        if unexpected:
            raise EvidenceError(
                f"unknown campaign operations below {safe_scheme}: {sorted(unexpected)}"
            )

    for condition in SCHEME_OPERATIONS:
        scheme, operation = condition
        directory = root / _safe_condition_name(scheme) / operation
        manifests: list[tuple[int, int, Path, dict[str, object]]] = []
        if directory.exists():
            if not directory.is_dir() or directory.is_symlink():
                raise EvidenceError("unsafe condition directory")
            for block_dir in directory.iterdir():
                if block_dir.is_symlink() or not block_dir.is_dir() or re.fullmatch(r"block_\d{3}", block_dir.name) is None:
                    raise EvidenceError(f"invalid block directory: {block_dir}")
                for attempt_dir in block_dir.iterdir():
                    if attempt_dir.is_symlink() or not attempt_dir.is_dir() or re.fullmatch(r"attempt_\d{3}", attempt_dir.name) is None:
                        raise EvidenceError(f"invalid attempt directory: {attempt_dir}")
                    manifest_path = attempt_dir / "manifest.json"
                    if manifest_path.is_symlink() or not manifest_path.is_file():
                        raise EvidenceError(f"interrupted attempt requires explicit recovery: {attempt_dir}")
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise EvidenceError(f"invalid attempt manifest: {manifest_path}") from exc
                    manifests.append((int(block_dir.name[6:]), int(attempt_dir.name[8:]), attempt_dir, manifest))
        manifests.sort(key=lambda item: (item[0], item[1]))
        accepted_count = accepted_blocks = rejected_attempts = consecutive = 0
        expected_block = expected_attempt = 1
        previous_status: str | None = None
        terminal = False
        last_sequence = None
        last_clock = None
        for block, attempt, attempt_dir, manifest in manifests:
            if terminal or block != expected_block or attempt != expected_attempt:
                raise EvidenceError(f"noncanonical or post-terminal attempt ordering: {attempt_dir}")
            if manifest.get("provenance") != campaign["provenance"] or manifest.get("device") != campaign["device"]:
                raise EvidenceError("attempt provenance differs from campaign identity")
            context = validate_campaign_attempt_context(manifest.get("campaign_context"))
            sequence = context["attempt_sequence"]
            cooldown = manifest.get("cooldown_preceding_seconds")
            if not isinstance(cooldown, int) or cooldown < 0:
                raise EvidenceError("invalid campaign cooldown evidence")
            global_attempts.append(
                (sequence, condition, str(manifest.get("status")), cooldown, context["completion_clock"])
            )
            last_sequence, last_clock = sequence, context["completion_clock"]
            status = manifest.get("status")
            if status == "accepted":
                timings, _ = accepted_validator(attempt_dir, manifest)
                _validate_attempt_inventory(attempt_dir, manifest)
                start, end = manifest.get("accepted_sample_start"), manifest.get("accepted_sample_end_exclusive")
                if start != accepted_count or end != accepted_count + len(timings):
                    raise EvidenceError("accepted campaign sample indices are not contiguous")
                if end > TARGET_ACCEPTED_TIMINGS:
                    raise EvidenceError("campaign accepted timing count exceeds 1000")
                accepted_count = end
                accepted_blocks += 1
                consecutive = 0
                previous_status = "accepted"
                expected_block += 1
                expected_attempt = 1
            elif status == "rejected":
                rejected_validator(attempt_dir, manifest)
                _validate_attempt_inventory(attempt_dir, manifest)
                rejected_attempts += 1
                consecutive += 1
                previous_status = "rejected"
                expected_attempt += 1
                terminal = consecutive >= 3
            else:
                raise EvidenceError("campaign contains an incomplete or unknown attempt status")
        condition_states[condition] = ConditionCampaignState(
            scheme, operation, accepted_count, accepted_blocks, rejected_attempts, consecutive,
            expected_block, expected_attempt, previous_status, terminal,
            accepted_count == TARGET_ACCEPTED_TIMINGS, last_sequence, last_clock,
        )

    global_attempts.sort(key=lambda item: item[0])
    if [item[0] for item in global_attempts] != list(range(1, len(global_attempts) + 1)):
        raise EvidenceError("campaign attempt sequence is duplicated, missing, or noncontiguous")
    local_status: dict[tuple[str, str], str | None] = {
        condition: None for condition in SCHEME_OPERATIONS
    }
    local_rejections: dict[tuple[str, str], int] = {
        condition: 0 for condition in SCHEME_OPERATIONS
    }
    for index, (_, condition, status, recorded_cooldown, _) in enumerate(global_attempts):
        if local_status[condition] == "rejected":
            required_local = COOLDOWN_ESCALATION_SECONDS[local_rejections[condition] - 1]
        elif local_status[condition] == "accepted":
            required_local = NORMAL_INTERBLOCK_COOLDOWN_SECONDS
        else:
            required_local = 0
        required_global = NORMAL_INTERBLOCK_COOLDOWN_SECONDS if index else 0
        if recorded_cooldown != max(required_local, required_global):
            raise EvidenceError("campaign cooldown history mismatch")
        if status == "rejected":
            local_rejections[condition] += 1
        else:
            local_rejections[condition] = 0
        local_status[condition] = status
    last = global_attempts[-1] if global_attempts else None
    return CampaignReconstruction(
        campaign, condition_states, len(global_attempts) + 1,
        last[1] if last else None, last[2] if last else None, last[4] if last else None,
    )


def required_resume_cooldowns(
    state: CampaignReconstruction, condition: tuple[str, str]
) -> list[tuple[int, dict[str, object] | None]]:
    selected = state.conditions[condition]
    constraints: list[tuple[int, dict[str, object] | None]] = []
    if selected.previous_status == "rejected":
        if selected.consecutive_rejections >= 3:
            raise EvidenceError("condition is terminal after three consecutive rejections")
        constraints.append(
            (COOLDOWN_ESCALATION_SECONDS[selected.consecutive_rejections - 1], selected.last_completion_clock)
        )
    if state.last_global_condition is not None:
        constraints.append((NORMAL_INTERBLOCK_COOLDOWN_SECONDS, state.last_global_completion_clock))
    return constraints or [(0, None)]


class CampaignLock:
    def __init__(self, root: Path):
        self.path = root / LOCK_FILE

    def acquire(self) -> None:
        try:
            atomic_json(self.path, {
                "schema": LOCK_SCHEMA,
                "pid": os.getpid(),
                "host_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }, exclusive=True)
        except FileExistsError as exc:
            raise EvidenceError("campaign has an active or crash-stale writer lock") from exc

    def release(self) -> None:
        self.path.unlink()

    def __enter__(self) -> "CampaignLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def recover_stale_lock(root: Path) -> Path:
    lock = root / LOCK_FILE
    if lock.is_symlink() or not lock.is_file():
        raise EvidenceError("stale campaign lock must be a regular non-symlink file")
    try:
        value = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError("stale campaign lock is unreadable; manual inspection required") from exc
    validate_lock_document(value)
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    pid = value.get("pid")
    if value.get("host_boot_id") == current_boot and isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise EvidenceError("cannot prove stale campaign writer is absent") from exc
        else:
            raise EvidenceError("campaign writer process is still alive")
    recovery = root / "lock_recoveries"
    if recovery.exists():
        if recovery.is_symlink() or not recovery.is_dir():
            raise EvidenceError("unsafe campaign lock recovery directory")
    else:
        recovery.mkdir()
    destination = recovery / f"lock-{time.time_ns()}.json"
    try:
        os.link(lock, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise EvidenceError("campaign lock recovery evidence collision") from exc
    lock.unlink()
    return destination


def validate_lock_document(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "pid", "host_boot_id"}
        or value.get("schema") != LOCK_SCHEMA
        or not isinstance(value.get("pid"), int)
        or value["pid"] <= 0
        or not isinstance(value.get("host_boot_id"), str)
        or not value["host_boot_id"]
    ):
        raise EvidenceError("stale campaign lock identity is invalid")


def state_snapshot(state: CampaignReconstruction) -> dict[str, object]:
    def status_fields(
        condition: tuple[str, str], value: ConditionCampaignState
    ) -> dict[str, object]:
        if value.complete:
            condition_state = "complete"
            required_cooldown = 0
        elif value.terminal_failure:
            condition_state = "terminal"
            required_cooldown = 0
        else:
            condition_state = "incomplete"
            constraints = required_resume_cooldowns(state, condition)
            required_cooldown = max(required for required, _ in constraints)
        return {
            "state": condition_state,
            "required_cooldown_seconds_if_resumed": required_cooldown,
            "pending_cooldown_seconds_without_live_boot_proof": required_cooldown,
        }

    return {
        "schema": "pqc-energy-trading.e0-android-campaign-state.v1",
        "authoritative_source": "reconstructed validated attempt evidence",
        "next_campaign_sequence": state.next_campaign_sequence,
        "conditions": [
            {
                "scheme": condition[0], "operation": condition[1],
                "accepted_count": value.accepted_count, "remaining": value.remaining,
                "accepted_blocks": value.accepted_blocks, "rejected_attempts": value.rejected_attempts,
                "consecutive_rejections": value.consecutive_rejections,
                "next_block_index": value.next_block_index, "next_attempt_index": value.next_attempt_index,
                "previous_status": value.previous_status, "terminal_failure": value.terminal_failure,
                "complete": value.complete,
                **status_fields(condition, value),
            }
            for condition, value in state.conditions.items()
        ],
    }
