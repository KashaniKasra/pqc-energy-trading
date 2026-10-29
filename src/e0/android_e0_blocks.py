#!/usr/bin/env python3
"""Versioned block validation for the Android E0 scientific protocol."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from android_e0 import EvidenceError, SCHEME_OPERATIONS, file_receipt, parse_current_hal_cpu_temperatures

BLOCK_ATTEMPT_SCHEMA = "pqc-energy-trading.e0-android-block-attempt.v3"
SCHEDULE_SCHEMA = "pqc-energy-trading.e0-android-sustained-schedule.v1"
SCHEDULE_FIELDS = (
    "timed_index",
    "operation_ordinal",
    "target_offset_ns",
    "selection_monotonic_ns",
)
FREQUENCY_SCHEMA = (
    "scheduled_monotonic_ns",
    "actual_monotonic_ns",
    "freq_khz",
    "success",
    "missed",
    "error",
    "phase",
)
FREQUENCY_INTERVAL_NS = 100_000_000
MINIMUM_AB_WINDOW_SAMPLES = 5
MINIMUM_BLOCK_FREQUENCY_SAMPLES = 50
BLOCK_START_BUDGET_NS = 59_000_000_000
BLOCK_HARD_MAXIMUM_NS = 60_000_000_000
MONITORING_TARGET_NS = 6_000_000_000
INITIAL_OPERATION_ESTIMATE_NS = 1_000_000_000
ADMISSION_FIXED_MARGIN_NS = 100_000_000
ADMISSION_POLICY = (
    "admit only when elapsed + max(initial estimate, maximum observed occupancy) + "
    "max(100ms, ceil(25% estimate)) is strictly below 60s"
)
ADMISSION_PREDICTION_LIMITATION = (
    "reduces overrun risk but cannot guarantee an unexpectedly slower native operation finishes before 60s"
)
TARGET_ACCEPTED_TIMINGS = 1000
WARMUP_ITERATIONS = 100
COOLDOWN_ESCALATION_SECONDS = (180, 360, 720)
NORMAL_INTERBLOCK_COOLDOWN_SECONDS = 180


def block_methodology_document(stabilization_timeout_seconds: int = 30) -> dict[str, object]:
    if stabilization_timeout_seconds < 3:
        raise EvidenceError("stabilization timeout must be at least three seconds")
    return {
        "warmup_iterations_per_attempt": WARMUP_ITERATIONS,
        "stabilization_minimum_seconds": 3,
        "stabilization_consecutive_samples": 5,
        "stabilization_rule": "max_frequency * 100 <= min_frequency * 102",
        "stabilization_timeout_seconds": stabilization_timeout_seconds,
        "frequency_interval_ms": 100,
        "block_start_budget_seconds": 59,
        "block_hard_maximum_seconds": 60,
        "monitoring_target_seconds": 6,
        "monitoring_target_status": "engineering parameter; not a supervisor-specified scientific constant",
        "operation_admission_policy": ADMISSION_POLICY,
        "operation_admission_initial_estimate_ms": 1000,
        "operation_admission_fixed_margin_ms": 100,
        "operation_admission_relative_margin": "ceil(25% of estimate)",
        "operation_admission_prediction_limitation": ADMISSION_PREDICTION_LIMITATION,
        "scheduling_schema": SCHEDULE_SCHEMA,
        "scheduling_algorithm": "precomputed linear target offsets; identical untimed primitive operations fill target gaps without sleeps",
        "singleton_remainder_policy": "one final remainder is timed at the six-second target without fabrication or discard",
        "minimum_block_frequency_samples": MINIMUM_BLOCK_FREQUENCY_SAMPLES,
        "ab_window_rule": "exactly 600 observations uses first/last 60; otherwise ceil(10%); minimum 5 per disjoint window",
        "drop_rejection": "reject iff B < 0.9 * A; exactly 10% drop is accepted",
        "target_accepted_timings": TARGET_ACCEPTED_TIMINGS,
        "normal_interblock_cooldown_seconds": NORMAL_INTERBLOCK_COOLDOWN_SECONDS,
        "rejection_cooldown_escalation_seconds": list(COOLDOWN_ESCALATION_SECONDS),
        "maximum_consecutive_rejections": 3,
        "fourth_attempt_authorized": False,
    }


@dataclass(frozen=True)
class FrequencyObservation:
    scheduled_ns: int
    actual_ns: int
    frequency_khz: int
    success: bool
    missed: bool
    error: str
    phase: str


@dataclass(frozen=True)
class BlockFrequencyAssessment:
    sample_count: int
    window_count: int
    first_window_indices: tuple[int, ...]
    last_window_indices: tuple[int, ...]
    a_khz: Fraction
    b_khz: Fraction
    accepted: bool


@dataclass(frozen=True)
class BlockPhaseEvidence:
    stabilization_start_ns: int
    stabilization_ready_ns: int
    measured_start_ns: int
    measured_end_ns: int
    block_duration_ns: int
    requested_iterations: int
    completed_iterations: int
    total_operations: int
    timed_operations: int
    untimed_operations: int
    stabilization_minimum_khz: int
    stabilization_maximum_khz: int


@dataclass(frozen=True)
class TimedOperationSelection:
    timed_index: int
    operation_ordinal: int
    target_offset_ns: int
    selection_monotonic_ns: int


@dataclass
class ConditionProgress:
    accepted_count: int = 0
    block_index: int = 1
    consecutive_rejections: int = 0
    terminal_failure: bool = False

    def requested_iterations(self) -> int:
        remaining = TARGET_ACCEPTED_TIMINGS - self.accepted_count
        if remaining <= 0:
            raise EvidenceError("condition already has 1000 accepted timings")
        return remaining

    def accept(self, completed: int) -> int | None:
        if completed <= 0 or self.accepted_count + completed > TARGET_ACCEPTED_TIMINGS:
            raise EvidenceError("accepted block would underflow or exceed the 1000-sample target")
        self.accepted_count += completed
        self.consecutive_rejections = 0
        if self.accepted_count == TARGET_ACCEPTED_TIMINGS:
            return None
        self.block_index += 1
        return NORMAL_INTERBLOCK_COOLDOWN_SECONDS

    def reject(self) -> int | None:
        self.consecutive_rejections += 1
        if self.consecutive_rejections >= 3:
            self.terminal_failure = True
            return None
        return COOLDOWN_ESCALATION_SECONDS[self.consecutive_rejections - 1]


def _median_fraction(values: list[int]) -> Fraction:
    if not values:
        raise EvidenceError("median requires observations")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle], 1)
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def read_block_timings(path: Path) -> list[float]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["iteration", "elapsed_ms"]:
                raise EvidenceError("invalid block timing schema")
            values: list[float] = []
            for expected, row in enumerate(reader, 1):
                if None in row.values() or int(row["iteration"]) != expected:
                    raise EvidenceError("invalid block timing iteration sequence")
                value = float(row["elapsed_ms"])
                if not math.isfinite(value) or value <= 0:
                    raise EvidenceError("block timing must be finite and positive")
                values.append(value)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"invalid block timing evidence: {exc}") from exc
    if not values:
        raise EvidenceError("block contains no timing samples")
    return values


def read_block_frequency(path: Path) -> list[FrequencyObservation]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != FREQUENCY_SCHEMA:
                raise EvidenceError("invalid block frequency schema")
            observations: list[FrequencyObservation] = []
            previous_scheduled = -1
            for row in reader:
                if None in row.values():
                    raise EvidenceError("missing block frequency value")
                scheduled = int(row["scheduled_monotonic_ns"])
                actual = int(row["actual_monotonic_ns"])
                frequency = int(row["freq_khz"])
                if scheduled <= previous_scheduled:
                    raise EvidenceError("frequency schedules are not strictly monotonic")
                previous_scheduled = scheduled
                if row["success"] not in {"true", "false"} or row["missed"] not in {"true", "false"}:
                    raise EvidenceError("invalid frequency status boolean")
                success = row["success"] == "true"
                missed = row["missed"] == "true"
                error = row["error"]
                if success:
                    if missed or error or actual < scheduled or frequency <= 0:
                        raise EvidenceError("inconsistent successful frequency observation")
                elif not error or actual < 0 or frequency < 0:
                    raise EvidenceError("failed/missed frequency observation lacks diagnostics")
                observations.append(
                    FrequencyObservation(scheduled, actual, frequency, success, missed, error, row["phase"])
                )
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"invalid block frequency evidence: {exc}") from exc
    return observations


def distributed_target_offsets(count: int) -> tuple[int, ...]:
    if count <= 0 or count > TARGET_ACCEPTED_TIMINGS:
        raise EvidenceError("invalid sustained-workload target count")
    if count == 1:
        return (MONITORING_TARGET_NS,)
    denominator = count - 1
    return tuple(index * MONITORING_TARGET_NS // denominator for index in range(count))


def read_timed_schedule(path: Path, requested: int, measured_start_ns: int, measured_end_ns: int) -> list[TimedOperationSelection]:
    targets = distributed_target_offsets(requested)
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != SCHEDULE_FIELDS:
                raise EvidenceError("invalid timed-operation schedule schema")
            selections: list[TimedOperationSelection] = []
            previous_ordinal = 0
            previous_timestamp = 0
            for row in reader:
                if None in row.values():
                    raise EvidenceError("missing timed-operation schedule value")
                selection = TimedOperationSelection(
                    timed_index=int(row["timed_index"]),
                    operation_ordinal=int(row["operation_ordinal"]),
                    target_offset_ns=int(row["target_offset_ns"]),
                    selection_monotonic_ns=int(row["selection_monotonic_ns"]),
                )
                if selection.timed_index != len(selections) or selection.timed_index >= requested:
                    raise EvidenceError("timed-operation indices are incomplete, duplicated, or out of order")
                if selection.operation_ordinal <= previous_ordinal:
                    raise EvidenceError("timed-operation ordinals are not strictly increasing")
                if selection.target_offset_ns != targets[selection.timed_index]:
                    raise EvidenceError("timed-operation target was not predetermined by the registered schedule")
                if not measured_start_ns <= selection.selection_monotonic_ns <= measured_end_ns:
                    raise EvidenceError("timed-operation selection timestamp is outside the measured block")
                if selection.selection_monotonic_ns - measured_start_ns < selection.target_offset_ns:
                    raise EvidenceError("timed operation was selected before its predetermined target")
                if selection.selection_monotonic_ns <= previous_timestamp:
                    raise EvidenceError("timed-operation selection timestamps are not strictly monotonic")
                previous_ordinal = selection.operation_ordinal
                previous_timestamp = selection.selection_monotonic_ns
                selections.append(selection)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f"invalid timed-operation schedule: {exc}") from exc
    if not selections:
        raise EvidenceError("timed-operation schedule is empty")
    return selections


def assess_block_frequency(
    observations: list[FrequencyObservation], measured_start_ns: int, measured_end_ns: int
) -> BlockFrequencyAssessment:
    if measured_start_ns <= 0 or measured_end_ns <= measured_start_ns:
        raise EvidenceError("invalid measured block interval")
    in_block = [item for item in observations if measured_start_ns <= item.scheduled_ns <= measured_end_ns]
    if any(item.phase != "post_stabilization" for item in in_block):
        raise EvidenceError("measurement block frequency observation has the wrong sampler phase")
    if any(not item.success or item.missed for item in in_block):
        raise EvidenceError("measurement block contains failed or missed frequency observations")
    if any(item.actual_ns > measured_end_ns for item in in_block):
        raise EvidenceError("frequency observation completed outside the measured block")
    if len(in_block) < MINIMUM_BLOCK_FREQUENCY_SAMPLES:
        raise EvidenceError(
            f"inadequate frequency evidence: {len(in_block)} observations; "
            f"requires at least {MINIMUM_BLOCK_FREQUENCY_SAMPLES}"
        )
    if in_block[0].scheduled_ns - measured_start_ns > FREQUENCY_INTERVAL_NS:
        raise EvidenceError("frequency coverage starts more than one scheduled interval late")
    if measured_end_ns - in_block[-1].scheduled_ns > FREQUENCY_INTERVAL_NS:
        raise EvidenceError("frequency coverage ends more than one scheduled interval early")
    for left, right in zip(in_block, in_block[1:]):
        if right.scheduled_ns - left.scheduled_ns != FREQUENCY_INTERVAL_NS:
            raise EvidenceError("in-block frequency schedules are not consecutive")

    window_count = 60 if len(in_block) == 600 else math.ceil(len(in_block) * 0.10)
    if window_count < MINIMUM_AB_WINDOW_SAMPLES or 2 * window_count > len(in_block):
        raise EvidenceError("frequency A/B windows are not defensibly populated and disjoint")
    first_indices = tuple(range(window_count))
    last_indices = tuple(range(len(in_block) - window_count, len(in_block)))
    a = _median_fraction([in_block[index].frequency_khz for index in first_indices])
    b = _median_fraction([in_block[index].frequency_khz for index in last_indices])
    return BlockFrequencyAssessment(
        sample_count=len(in_block),
        window_count=window_count,
        first_window_indices=first_indices,
        last_window_indices=last_indices,
        a_khz=a,
        b_khz=b,
        accepted=b * 10 >= a * 9,
    )


def parse_block_phases(path: Path, *, allow_overrun: bool = False) -> BlockPhaseEvidence:
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid block phase evidence: {exc}") from exc
    names = [event.get("event") for event in events]
    if names != ["stabilization_start", "stabilization_ready", "measured_start", "measured_end", "block_summary"]:
        raise EvidenceError("block phase events are incomplete or out of order")
    try:
        timestamps = [int(event["monotonic_ns"]) for event in events]
        start, ready, measured_start, measured_end, summary_time = timestamps
        readiness = events[1]["stabilization"]
        summary = events[4]["block"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("malformed block phase fields") from exc
    if not (0 < start <= ready <= measured_start < measured_end) or summary_time != measured_end:
        raise EvidenceError("block phase timestamps are not monotonic")
    if (
        readiness.get("schema") != "pqc-energy-trading.e0-android-stabilization.v1"
        or readiness.get("type") != "ready"
        or not isinstance(readiness.get("token"), str)
        or not readiness["token"]
        or not isinstance(events[1].get("operation_count"), int)
        or events[1]["operation_count"] <= 0
    ):
        raise EvidenceError("invalid stabilization identity or continuous-work count")
    if int(readiness.get("start_monotonic_ns", -1)) != start or int(readiness.get("qualified_monotonic_ns", -1)) != ready:
        raise EvidenceError("stabilization timestamps disagree with phase markers")
    if int(readiness.get("work_duration_ns", -1)) < 3_000_000_000:
        raise EvidenceError("stabilization work was shorter than three seconds")
    if int(readiness.get("work_duration_ns", -1)) != ready - start:
        raise EvidenceError("stabilization duration is inconsistent")
    observations = readiness.get("observations")
    if not isinstance(observations, list) or len(observations) != 5:
        raise EvidenceError("stabilization requires five qualifying observations")
    frequencies: list[int] = []
    previous = None
    final_actual = None
    for observation in observations:
        try:
            scheduled = int(observation["scheduled_monotonic_ns"])
            actual = int(observation["actual_monotonic_ns"])
            frequency = int(observation["freq_khz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError("malformed stabilization observation") from exc
        if scheduled < start or actual < scheduled or actual > ready or frequency <= 0:
            raise EvidenceError("invalid stabilization observation")
        if previous is not None and scheduled - previous != FREQUENCY_INTERVAL_NS:
            raise EvidenceError("stabilization observations are not consecutive")
        previous = scheduled
        final_actual = actual
        frequencies.append(frequency)
    if final_actual != ready:
        raise EvidenceError("stabilization ready marker is not the final qualifying observation")
    minimum, maximum = min(frequencies), max(frequencies)
    if int(readiness.get("minimum_khz", -1)) != minimum or int(readiness.get("maximum_khz", -1)) != maximum:
        raise EvidenceError("stabilization min/max mismatch")
    if maximum * 100 > minimum * 102:
        raise EvidenceError("stabilization window exceeds two percent")

    duration = measured_end - measured_start
    try:
        evidence = BlockPhaseEvidence(
            stabilization_start_ns=start,
            stabilization_ready_ns=ready,
            measured_start_ns=measured_start,
            measured_end_ns=measured_end,
            block_duration_ns=duration,
            requested_iterations=int(summary["requested_iterations"]),
            completed_iterations=int(summary["completed_iterations"]),
            total_operations=int(summary["total_primitive_operations"]),
            timed_operations=int(summary["timed_primitive_operations"]),
            untimed_operations=int(summary["untimed_primitive_operations"]),
            stabilization_minimum_khz=minimum,
            stabilization_maximum_khz=maximum,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("malformed block summary") from exc
    if (
        int(summary.get("warmup_iterations", -1)) != WARMUP_ITERATIONS
        or int(summary.get("start_budget_ns", -1)) != BLOCK_START_BUDGET_NS
        or int(summary.get("hard_maximum_ns", -1)) != BLOCK_HARD_MAXIMUM_NS
        or int(summary.get("block_duration_ns", -1)) != duration
        or bool(summary.get("crossed_start_budget")) != (duration > BLOCK_START_BUDGET_NS)
        or bool(summary.get("exceeded_hard_maximum")) != (duration > BLOCK_HARD_MAXIMUM_NS)
        or summary.get("schedule_schema") != SCHEDULE_SCHEMA
        or summary.get("scheduling_algorithm")
        != "precomputed linear target offsets; identical untimed primitive operations fill target gaps without sleeps"
        or int(summary.get("monitoring_target_ns", -1)) != MONITORING_TARGET_NS
        or evidence.timed_operations != evidence.completed_iterations
        or evidence.total_operations != evidence.timed_operations + evidence.untimed_operations
        or evidence.total_operations < evidence.timed_operations
    ):
        raise EvidenceError("block summary methodology mismatch")
    try:
        maximum_observed = int(summary["maximum_observed_operation_ns"])
        final_estimate = int(summary["final_admission_estimate_ns"])
        final_margin = int(summary["final_admission_safety_margin_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("malformed block admission evidence") from exc
    expected_estimate = max(INITIAL_OPERATION_ESTIMATE_NS, maximum_observed)
    expected_margin = max(ADMISSION_FIXED_MARGIN_NS, (expected_estimate + 3) // 4)
    if (
        maximum_observed < 0
        or summary.get("admission_policy") != ADMISSION_POLICY
        or int(summary.get("initial_operation_estimate_ns", -1)) != INITIAL_OPERATION_ESTIMATE_NS
        or final_estimate != expected_estimate
        or final_margin != expected_margin
        or summary.get("admission_prediction_limitation") != ADMISSION_PREDICTION_LIMITATION
    ):
        raise EvidenceError("block admission evidence mismatch")
    if duration > BLOCK_HARD_MAXIMUM_NS and not allow_overrun:
        raise EvidenceError("measurement block exceeded 60 seconds")
    return evidence


def block_telemetry_document(assessment: BlockFrequencyAssessment) -> dict[str, object]:
    def exact_decimal(value: Fraction) -> str:
        if value.denominator == 1:
            return str(value.numerator)
        if value.denominator == 2:
            return f"{value.numerator // 2}.5"
        raise EvidenceError("frequency median is not exactly representable as an integer or half-integer")

    return {
        "frequency_sample_count": assessment.sample_count,
        "ab_window_rounding": "ceil(10% of valid scheduled observations); exactly 600 uses 60",
        "minimum_ab_window_samples": MINIMUM_AB_WINDOW_SAMPLES,
        "window_count": assessment.window_count,
        "first_window_indices": list(assessment.first_window_indices),
        "last_window_indices": list(assessment.last_window_indices),
        "a_khz": exact_decimal(assessment.a_khz),
        "b_khz": exact_decimal(assessment.b_khz),
        "accepted": assessment.accepted,
        "rejection_rule": "reject iff B < 0.9 * A; exactly 10% drop is accepted",
    }


def complete_block_telemetry(
    assessment: BlockFrequencyAssessment,
    before: list[float],
    after: list[float],
    phases: BlockPhaseEvidence,
) -> dict[str, object]:
    return {
        "frequency": block_telemetry_document(assessment),
        "stabilization_minimum_khz": phases.stabilization_minimum_khz,
        "stabilization_maximum_khz": phases.stabilization_maximum_khz,
        "temperature_before_cpu_c": [format(value, ".9f") for value in before],
        "temperature_after_cpu_c": [format(value, ".9f") for value in after],
        "temp_c_max": format(max(before + after), ".9f"),
        "temperature_definition": "maximum observed CPU mType=0 HAL value before/after this block attempt; not continuous peak",
    }


def validate_accepted_block(attempt_dir: Path, manifest: dict[str, object]) -> tuple[list[float], BlockFrequencyAssessment]:
    required = {
        "schema", "scientific", "status", "scheme", "operation", "block_index", "attempt_index",
        "requested_timing_iterations", "completed_timing_iterations", "accepted_timing_iterations",
        "accepted_sample_start", "accepted_sample_end_exclusive", "cooldown_preceding_seconds",
        "cooldown_escalation_seconds", "normal_interblock_cooldown_seconds",
        "warmup_iterations", "methodology", "files", "telemetry", "provenance", "device",
    }
    if frozenset(manifest) not in {frozenset(required), frozenset(required | {"campaign_context"})} or manifest.get("schema") != BLOCK_ATTEMPT_SCHEMA:
        raise EvidenceError("invalid accepted block manifest schema")
    if manifest.get("scientific") is not True or manifest.get("status") != "accepted":
        raise EvidenceError("block is not accepted scientific evidence")
    _validate_manifest_identity(attempt_dir, manifest)
    files = manifest.get("files")
    expected_roles = {
        "timing_samples", "phase_events", "timed_schedule", "frequency_samples", "temperature_before",
        "temperature_after", "stdout", "stderr", "sampler_stdout", "sampler_stderr",
    }
    if not isinstance(files, dict) or set(files) != expected_roles:
        raise EvidenceError("missing block file receipts")
    paths = {role: _validate_receipt(attempt_dir, receipt) for role, receipt in files.items()}
    expected_inventory = {Path(receipt["path"]).name for receipt in files.values()} | {"manifest.json"}
    if {path.name for path in attempt_dir.iterdir()} != expected_inventory:
        raise EvidenceError("block evidence inventory mismatch")
    timings = read_block_timings(paths["timing_samples"])
    phases = parse_block_phases(paths["phase_events"])
    schedule = read_timed_schedule(
        paths["timed_schedule"], phases.requested_iterations, phases.measured_start_ns, phases.measured_end_ns
    )
    frequency = assess_block_frequency(read_block_frequency(paths["frequency_samples"]), phases.measured_start_ns, phases.measured_end_ns)
    if not frequency.accepted:
        raise EvidenceError("manifest accepts a frequency-rejected block")
    completed = len(timings)
    before = parse_current_hal_cpu_temperatures(paths["temperature_before"].read_text(encoding="utf-8"))
    after = parse_current_hal_cpu_temperatures(paths["temperature_after"].read_text(encoding="utf-8"))
    methodology = manifest.get("methodology")
    timeout = methodology.get("stabilization_timeout_seconds") if isinstance(methodology, dict) else None
    if (
        phases.completed_iterations != completed
        or len(schedule) != completed
        or phases.timed_operations != completed
        or phases.total_operations != schedule[-1].operation_ordinal
        or not 1 <= phases.requested_iterations <= TARGET_ACCEPTED_TIMINGS
        or completed > phases.requested_iterations
        or manifest.get("completed_timing_iterations") != completed
        or manifest.get("accepted_timing_iterations") != completed
        or manifest.get("requested_timing_iterations") != phases.requested_iterations
        or manifest.get("warmup_iterations") != WARMUP_ITERATIONS
        or manifest.get("cooldown_escalation_seconds") != list(COOLDOWN_ESCALATION_SECONDS)
        or manifest.get("normal_interblock_cooldown_seconds") != NORMAL_INTERBLOCK_COOLDOWN_SECONDS
        or not isinstance(timeout, int)
        or manifest.get("methodology") != block_methodology_document(timeout)
        or manifest.get("telemetry") != complete_block_telemetry(frequency, before, after, phases)
    ):
        raise EvidenceError("accepted block manifest disagrees with raw evidence")
    start = manifest.get("accepted_sample_start")
    end = manifest.get("accepted_sample_end_exclusive")
    if not isinstance(start, int) or not isinstance(end, int) or end - start != completed:
        raise EvidenceError("invalid accepted sample index range")
    if phases.completed_iterations == phases.requested_iterations:
        if schedule[-1].target_offset_ns != MONITORING_TARGET_NS or phases.block_duration_ns < MONITORING_TARGET_NS:
            raise EvidenceError("completed schedule does not cover the six-second monitoring target")
    if phases.requested_iterations == 1 and start != TARGET_ACCEPTED_TIMINGS - 1:
        raise EvidenceError("singleton scheduling is allowed only for the final accepted-sample remainder")
    return timings, frequency


def accepted_block_reporting_evidence(attempt_dir: Path, manifest: dict[str, object]) -> dict[str, object]:
    """Return raw accepted values only after complete v3 block validation."""
    timings, assessment = validate_accepted_block(attempt_dir, manifest)
    files = manifest["files"]
    paths = {role: _validate_receipt(attempt_dir, receipt) for role, receipt in files.items()}
    phases = parse_block_phases(paths["phase_events"])
    observations = read_block_frequency(paths["frequency_samples"])
    frequencies = [
        item.frequency_khz
        for item in observations
        if phases.measured_start_ns <= item.scheduled_ns <= phases.measured_end_ns
    ]
    if len(frequencies) != assessment.sample_count:
        raise EvidenceError("accepted frequency population disagrees with the validated A/B assessment")
    before = parse_current_hal_cpu_temperatures(paths["temperature_before"].read_text(encoding="utf-8"))
    after = parse_current_hal_cpu_temperatures(paths["temperature_after"].read_text(encoding="utf-8"))
    return {
        "timings_ms": timings,
        "frequencies_khz": frequencies,
        "temperatures_c": before + after,
        "phases": phases,
    }


def _validate_manifest_identity(attempt_dir: Path, manifest: dict[str, object]) -> None:
    condition = (manifest.get("scheme"), manifest.get("operation"))
    if condition not in SCHEME_OPERATIONS:
        raise EvidenceError("unknown Android E0 block condition")
    block = manifest.get("block_index")
    attempt = manifest.get("attempt_index")
    if not isinstance(block, int) or block <= 0 or not isinstance(attempt, int) or attempt <= 0:
        raise EvidenceError("invalid block/attempt identity")
    safe_scheme = re.sub(r"[^A-Za-z0-9_.-]", "_", str(condition[0]))
    if (
        attempt_dir.name != f"attempt_{attempt:03d}"
        or attempt_dir.parent.name != f"block_{block:03d}"
        or attempt_dir.parent.parent.name != condition[1]
        or attempt_dir.parent.parent.parent.name != safe_scheme
    ):
        raise EvidenceError("block manifest identity does not match its path")


def validate_rejected_block(attempt_dir: Path, manifest: dict[str, object]) -> None:
    required = {
        "schema", "scientific", "status", "scheme", "operation", "block_index", "attempt_index",
        "requested_timing_iterations", "completed_timing_iterations", "accepted_timing_iterations",
        "accepted_sample_start", "accepted_sample_end_exclusive", "cooldown_preceding_seconds",
        "cooldown_escalation_seconds", "normal_interblock_cooldown_seconds",
        "warmup_iterations", "methodology", "files", "telemetry", "provenance", "device", "rejection_reason",
    }
    if frozenset(manifest) not in {frozenset(required), frozenset(required | {"campaign_context"})} or manifest.get("schema") != BLOCK_ATTEMPT_SCHEMA:
        raise EvidenceError("invalid rejected block manifest schema")
    if manifest.get("scientific") is not True or manifest.get("status") != "rejected":
        raise EvidenceError("invalid rejected block status")
    _validate_manifest_identity(attempt_dir, manifest)
    methodology = manifest.get("methodology")
    timeout = methodology.get("stabilization_timeout_seconds") if isinstance(methodology, dict) else None
    if (
        manifest.get("accepted_timing_iterations") != 0
        or manifest.get("accepted_sample_start") is not None
        or manifest.get("accepted_sample_end_exclusive") is not None
        or not isinstance(manifest.get("rejection_reason"), str)
        or not manifest["rejection_reason"]
        or manifest.get("cooldown_escalation_seconds") != list(COOLDOWN_ESCALATION_SECONDS)
        or manifest.get("normal_interblock_cooldown_seconds") != NORMAL_INTERBLOCK_COOLDOWN_SECONDS
        or not isinstance(timeout, int)
        or manifest.get("methodology") != block_methodology_document(timeout)
        or manifest.get("warmup_iterations") != WARMUP_ITERATIONS
    ):
        raise EvidenceError("rejected block contains accepted samples or invalid methodology")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise EvidenceError("missing rejected block receipts")
    paths = {role: _validate_receipt(attempt_dir, receipt) for role, receipt in files.items()}
    requested = manifest.get("requested_timing_iterations")
    completed = manifest.get("completed_timing_iterations")
    if (
        not isinstance(requested, int)
        or not 1 <= requested <= TARGET_ACCEPTED_TIMINGS
        or not isinstance(completed, int)
        or not 0 <= completed <= requested
    ):
        raise EvidenceError("invalid rejected block timing counts")
    if completed > 0 and not {"timing_samples", "phase_events", "timed_schedule"}.issubset(paths):
        raise EvidenceError("rejected block declares timings without retaining timing, phase, and schedule evidence")
    if "timing_samples" in paths and len(read_block_timings(paths["timing_samples"])) != completed:
        raise EvidenceError("rejected block timing count disagrees with raw evidence")
    if completed > 0:
        phases = parse_block_phases(paths["phase_events"], allow_overrun=True)
        schedule = read_timed_schedule(
            paths["timed_schedule"], phases.requested_iterations, phases.measured_start_ns, phases.measured_end_ns
        )
        if len(schedule) != completed or phases.completed_iterations != completed:
            raise EvidenceError("rejected block schedule count disagrees with raw evidence")
    if "frequency_samples" in paths:
        read_block_frequency(paths["frequency_samples"])
    expected_inventory = {Path(receipt["path"]).name for receipt in files.values()} | {"manifest.json"}
    if {path.name for path in attempt_dir.iterdir()} != expected_inventory:
        raise EvidenceError("rejected block evidence inventory mismatch")


def _validate_receipt(attempt_dir: Path, receipt: object) -> Path:
    if not isinstance(receipt, dict) or set(receipt) != {"path", "bytes", "sha256"}:
        raise EvidenceError("invalid block evidence receipt")
    relative = Path(str(receipt["path"]))
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise EvidenceError("unsafe block evidence path")
    path = attempt_dir / relative
    expected = file_receipt(path, attempt_dir)
    if expected != receipt:
        raise EvidenceError("block evidence hash or size mismatch")
    return path


def validate_condition_aggregation(condition_dir: Path) -> list[float]:
    manifests = sorted(condition_dir.glob("block_*/attempt_*/manifest.json"))
    if not manifests:
        raise EvidenceError("condition contains no block manifests")
    records: list[tuple[int, int, str, list[float], dict[str, object]]] = []
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("status") == "accepted":
            timings, _ = validate_accepted_block(path.parent, manifest)
            records.append((int(manifest["block_index"]), int(manifest["attempt_index"]), "accepted", timings, manifest))
        elif manifest.get("status") == "rejected":
            validate_rejected_block(path.parent, manifest)
            records.append((int(manifest["block_index"]), int(manifest["attempt_index"]), "rejected", [], manifest))
        else:
            raise EvidenceError("incomplete block attempt")
    records.sort(key=lambda value: (value[0], value[1]))
    result: list[float] = []
    expected_block = 1
    expected_attempt = 1
    consecutive_rejections = 0
    previous_status: str | None = None
    accepted_in_block = False
    for block, attempt, status, timings, manifest in records:
        if consecutive_rejections >= 3:
            raise EvidenceError("evidence continues after three consecutive rejected attempts")
        if block != expected_block or attempt != expected_attempt or accepted_in_block:
            raise EvidenceError("block attempts are not deterministically ordered")
        cooldown = manifest.get("cooldown_preceding_seconds")
        if previous_status == "rejected":
            expected_cooldown = COOLDOWN_ESCALATION_SECONDS[consecutive_rejections - 1]
            if cooldown != expected_cooldown:
                raise EvidenceError("rejection cooldown sequence mismatch")
        elif previous_status == "accepted" and cooldown != NORMAL_INTERBLOCK_COOLDOWN_SECONDS:
            raise EvidenceError("normal inter-block cooldown mismatch")
        elif previous_status is None and cooldown not in {0, NORMAL_INTERBLOCK_COOLDOWN_SECONDS}:
            raise EvidenceError("invalid initial condition cooldown")

        if status == "rejected":
            consecutive_rejections += 1
            if consecutive_rejections > 3:
                raise EvidenceError("more than three consecutive rejected attempts")
            expected_attempt += 1
            previous_status = "rejected"
            continue

        accepted_in_block = True
        if manifest["accepted_sample_start"] != len(result):
            raise EvidenceError("accepted sample indices are not contiguous")
        result.extend(timings)
        if manifest["accepted_sample_end_exclusive"] != len(result):
            raise EvidenceError("accepted sample range is inconsistent")
        consecutive_rejections = 0
        previous_status = "accepted"
        expected_block += 1
        expected_attempt = 1
        accepted_in_block = False
    if len(result) != TARGET_ACCEPTED_TIMINGS:
        raise EvidenceError(f"condition has {len(result)} accepted timings, expected 1000")
    return result
