import csv
import json
import tempfile
import unittest
from pathlib import Path

from android_e0 import EvidenceError, file_receipt
from android_e0_blocks import (
    BLOCK_ATTEMPT_SCHEMA,
    BLOCK_HARD_MAXIMUM_NS,
    BLOCK_START_BUDGET_NS,
    ADMISSION_FIXED_MARGIN_NS,
    ADMISSION_POLICY,
    ADMISSION_PREDICTION_LIMITATION,
    COOLDOWN_ESCALATION_SECONDS,
    INITIAL_OPERATION_ESTIMATE_NS,
    MONITORING_TARGET_NS,
    ConditionProgress,
    FrequencyObservation,
    assess_block_frequency,
    block_methodology_document,
    complete_block_telemetry,
    distributed_target_offsets,
    parse_block_phases,
    validate_condition_aggregation,
)

HAL = """Thermal Status: 0
Current temperatures from HAL:
  Temperature{mValue=40.0, mType=0, mName=CPU, mStatus=0}
Current cooling devices from HAL:
"""


def frequency_observations(count=50, *, first=2_000_000, last=2_000_000, start=10_000_000_000):
    values = [2_000_000] * count
    window = 60 if count == 600 else __import__("math").ceil(count * 0.10)
    values[:window] = [first] * window
    values[-window:] = [last] * window
    return [
        FrequencyObservation(
            scheduled_ns=start + index * 100_000_000,
            actual_ns=start + index * 100_000_000 + 1,
            frequency_khz=value,
            success=True,
            missed=False,
            error="",
            phase="post_stabilization",
        )
        for index, value in enumerate(values)
    ]


def assess(observations):
    return assess_block_frequency(observations, observations[0].scheduled_ns, observations[-1].actual_ns)


def phase_document(duration_ns, requested, completed):
    stabilization_start = 1_000_000_000
    observations = [
        {
            "scheduled_monotonic_ns": stabilization_start + (26 + index) * 100_000_000,
            "actual_monotonic_ns": stabilization_start + (26 + index) * 100_000_000,
            "freq_khz": 2_000_000,
        }
        for index in range(5)
    ]
    ready = observations[-1]["actual_monotonic_ns"]
    measured_start = 10_000_000_000
    measured_end = measured_start + duration_ns
    readiness = {
        "schema": "pqc-energy-trading.e0-android-stabilization.v1",
        "type": "ready",
        "token": "token",
        "start_monotonic_ns": stabilization_start,
        "qualified_monotonic_ns": ready,
        "work_duration_ns": ready - stabilization_start,
        "minimum_khz": 2_000_000,
        "maximum_khz": 2_000_000,
        "observations": observations,
    }
    summary = {
        "warmup_iterations": 100,
        "requested_iterations": requested,
        "completed_iterations": completed,
        "start_budget_ns": BLOCK_START_BUDGET_NS,
        "hard_maximum_ns": BLOCK_HARD_MAXIMUM_NS,
        "block_duration_ns": duration_ns,
        "crossed_start_budget": duration_ns > BLOCK_START_BUDGET_NS,
        "exceeded_hard_maximum": duration_ns > BLOCK_HARD_MAXIMUM_NS,
        "schedule_schema": "pqc-energy-trading.e0-android-sustained-schedule.v1",
        "scheduling_algorithm": "precomputed linear target offsets; identical untimed primitive operations fill target gaps without sleeps",
        "monitoring_target_ns": MONITORING_TARGET_NS,
        "total_primitive_operations": completed,
        "timed_primitive_operations": completed,
        "untimed_primitive_operations": 0,
        "admission_policy": ADMISSION_POLICY,
        "initial_operation_estimate_ns": INITIAL_OPERATION_ESTIMATE_NS,
        "maximum_observed_operation_ns": 1_000_000,
        "final_admission_estimate_ns": INITIAL_OPERATION_ESTIMATE_NS,
        "final_admission_safety_margin_ns": max(
            ADMISSION_FIXED_MARGIN_NS, (INITIAL_OPERATION_ESTIMATE_NS + 3) // 4
        ),
        "admission_prediction_limitation": ADMISSION_PREDICTION_LIMITATION,
    }
    return [
        {"event": "stabilization_start", "monotonic_ns": stabilization_start},
        {"event": "stabilization_ready", "monotonic_ns": ready, "operation_count": 1, "stabilization": readiness},
        {"event": "measured_start", "monotonic_ns": measured_start},
        {"event": "measured_end", "monotonic_ns": measured_end},
        {"event": "block_summary", "monotonic_ns": measured_end, "block": summary},
    ]


class FrequencyBlockTests(unittest.TestCase):
    def test_first_last_ten_percent_uses_ceiling_and_disjoint_windows(self):
        observations = frequency_observations(51, first=2_000_000, last=1_900_000)
        result = assess(observations)
        self.assertEqual(result.window_count, 6)
        self.assertEqual(result.first_window_indices, tuple(range(6)))
        self.assertEqual(result.last_window_indices, tuple(range(45, 51)))

    def test_exactly_ten_percent_drop_is_accepted(self):
        observations = frequency_observations(first=2_000_000, last=1_800_000)
        result = assess(observations)
        self.assertTrue(result.accepted)

    def test_greater_than_ten_percent_drop_is_rejected(self):
        observations = frequency_observations(first=2_000_000, last=1_799_000)
        result = assess(observations)
        self.assertFalse(result.accepted)

    def test_exactly_six_hundred_uses_sixty_samples(self):
        observations = frequency_observations(600)
        result = assess(observations)
        self.assertEqual(result.window_count, 60)

    def test_missing_observation_fails_closed(self):
        observations = frequency_observations()
        observations[20] = FrequencyObservation(
            observations[20].scheduled_ns, 0, 0, False, True, "scheduled observation missed", "post_stabilization"
        )
        with self.assertRaisesRegex(EvidenceError, "failed or missed"):
            assess(observations)

    def test_short_block_has_inadequate_evidence(self):
        observations = frequency_observations(49)
        with self.assertRaisesRegex(EvidenceError, "inadequate frequency evidence"):
            assess(observations)

    def test_frequency_read_completed_after_phase_boundary_fails(self):
        observations = frequency_observations(60)
        final = observations[-1]
        observations[-1] = FrequencyObservation(
            final.scheduled_ns, final.scheduled_ns + 100_000_001, final.frequency_khz,
            True, False, "", final.phase,
        )
        with self.assertRaisesRegex(EvidenceError, "outside the measured block"):
            assess_block_frequency(observations, observations[0].scheduled_ns, final.scheduled_ns + 100_000_000)

    def test_frequency_observation_with_wrong_phase_fails(self):
        observations = frequency_observations(60)
        item = observations[10]
        observations[10] = FrequencyObservation(
            item.scheduled_ns, item.actual_ns, item.frequency_khz, True, False, "", "stabilization"
        )
        with self.assertRaisesRegex(EvidenceError, "wrong sampler phase"):
            assess_block_frequency(observations, observations[0].scheduled_ns, observations[-1].actual_ns)


class SustainedScheduleTests(unittest.TestCase):
    def test_six_second_targets_cover_beginning_and_end(self):
        targets = distributed_target_offsets(1000)
        self.assertEqual(len(targets), 1000)
        self.assertEqual(targets[0], 0)
        self.assertEqual(targets[-1], 6_000_000_000)
        self.assertTrue(all(left < right for left, right in zip(targets, targets[1:])))

    def test_singleton_remainder_is_not_discarded_or_duplicated(self):
        self.assertEqual(distributed_target_offsets(1), (6_000_000_000,))

    def test_target_generation_is_independent_of_measurements(self):
        first = distributed_target_offsets(17)
        # No latency, temperature, frequency, or crypto result is an input.
        second = distributed_target_offsets(17)
        self.assertEqual(first, second)


class BlockDurationTests(unittest.TestCase):
    def parse_duration(self, duration):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "phase.jsonl"
            path.write_text("".join(json.dumps(value) + "\n" for value in phase_document(duration, 500, 500)))
            return parse_block_phases(path)

    def test_block_before_sixty_seconds(self):
        self.assertEqual(self.parse_duration(59_000_000_000).block_duration_ns, 59_000_000_000)

    def test_block_exactly_sixty_seconds(self):
        self.assertEqual(self.parse_duration(60_000_000_000).block_duration_ns, 60_000_000_000)

    def test_block_over_sixty_seconds_is_rejected(self):
        with self.assertRaisesRegex(EvidenceError, "exceeded 60 seconds"):
            self.parse_duration(60_000_000_001)


class ProgressTests(unittest.TestCase):
    def test_multiple_blocks_total_exactly_one_thousand(self):
        progress = ConditionProgress()
        self.assertEqual(progress.accept(600), 180)
        self.assertEqual(progress.requested_iterations(), 400)
        self.assertIsNone(progress.accept(400))
        self.assertEqual(progress.accepted_count, 1000)
        with self.assertRaises(EvidenceError):
            progress.requested_iterations()

    def test_rejected_samples_are_excluded_and_counter_resets(self):
        progress = ConditionProgress()
        self.assertEqual(progress.reject(), 180)
        self.assertEqual(progress.accepted_count, 0)
        self.assertEqual(progress.accept(500), 180)
        self.assertEqual(progress.consecutive_rejections, 0)
        self.assertEqual(progress.reject(), 180)

    def test_cooldown_escalation_and_terminal_third_rejection(self):
        progress = ConditionProgress()
        self.assertEqual(COOLDOWN_ESCALATION_SECONDS, (180, 360, 720))
        self.assertEqual(progress.reject(), 180)
        self.assertEqual(progress.reject(), 360)
        self.assertIsNone(progress.reject())
        self.assertTrue(progress.terminal_failure)


class AggregationEvidenceTests(unittest.TestCase):
    def make_accepted(self, root, block, start, count, cooldown, attempt=1, requested=None):
        requested = count if requested is None else requested
        directory = root / "ECDSA-P-256" / "keygen" / f"block_{block:03d}" / f"attempt_{attempt:03d}"
        directory.mkdir(parents=True)
        paths = {
            "timing_samples": directory / "timing_samples.csv",
            "phase_events": directory / "phase_events.jsonl",
            "timed_schedule": directory / "timed_schedule.csv",
            "frequency_samples": directory / "frequency_samples.csv",
            "temperature_before": directory / "temperature_before.txt",
            "temperature_after": directory / "temperature_after.txt",
            "stdout": directory / "stdout.log",
            "stderr": directory / "stderr.log",
            "sampler_stdout": directory / "sampler_stdout.log",
            "sampler_stderr": directory / "sampler_stderr.log",
        }
        with paths["timing_samples"].open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["iteration", "elapsed_ms"])
            for index in range(1, count + 1):
                writer.writerow([index, "1.000000000"])
        paths["phase_events"].write_text(
            "".join(json.dumps(value) + "\n" for value in phase_document(MONITORING_TARGET_NS, requested, count))
        )
        targets = distributed_target_offsets(requested)[:count]
        with paths["timed_schedule"].open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["timed_index", "operation_ordinal", "target_offset_ns", "selection_monotonic_ns"])
            for index, target in enumerate(targets):
                writer.writerow([index, index + 1, target, 10_000_000_000 + target])
        observations = frequency_observations(60)
        with paths["frequency_samples"].open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                ["scheduled_monotonic_ns", "actual_monotonic_ns", "freq_khz", "success", "missed", "error", "phase"]
            )
            for item in observations:
                writer.writerow([item.scheduled_ns, item.actual_ns, item.frequency_khz, "true", "false", "", item.phase])
        paths["temperature_before"].write_text(HAL)
        paths["temperature_after"].write_text(HAL)
        paths["stdout"].write_text("ok\n")
        paths["stderr"].write_text("")
        paths["sampler_stdout"].write_text("")
        paths["sampler_stderr"].write_text("")
        assessment = assess(observations)
        manifest = {
            "schema": BLOCK_ATTEMPT_SCHEMA,
            "scientific": True,
            "status": "accepted",
            "scheme": "ECDSA-P-256",
            "operation": "keygen",
            "block_index": block,
            "attempt_index": attempt,
            "requested_timing_iterations": requested,
            "completed_timing_iterations": count,
            "accepted_timing_iterations": count,
            "accepted_sample_start": start,
            "accepted_sample_end_exclusive": start + count,
            "cooldown_preceding_seconds": cooldown,
            "cooldown_escalation_seconds": [180, 360, 720],
            "normal_interblock_cooldown_seconds": 180,
            "warmup_iterations": 100,
            "methodology": block_methodology_document(),
            "files": {role: file_receipt(path, directory) for role, path in paths.items()},
            "telemetry": complete_block_telemetry(
                assessment,
                [40.0],
                [40.0],
                parse_block_phases(paths["phase_events"]),
            ),
            "provenance": {"fixture": True},
            "device": {"fixture": True},
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return directory

    def make_rejected(self, root, block, attempt, cooldown):
        directory = root / "ECDSA-P-256" / "keygen" / f"block_{block:03d}" / f"attempt_{attempt:03d}"
        directory.mkdir(parents=True)
        log = directory / "stderr.log"
        log.write_text("inadequate frequency evidence\n")
        manifest = {
            "schema": BLOCK_ATTEMPT_SCHEMA,
            "scientific": True,
            "status": "rejected",
            "scheme": "ECDSA-P-256",
            "operation": "keygen",
            "block_index": block,
            "attempt_index": attempt,
            "requested_timing_iterations": 1000,
            "completed_timing_iterations": 0,
            "accepted_timing_iterations": 0,
            "accepted_sample_start": None,
            "accepted_sample_end_exclusive": None,
            "cooldown_preceding_seconds": cooldown,
            "cooldown_escalation_seconds": [180, 360, 720],
            "normal_interblock_cooldown_seconds": 180,
            "warmup_iterations": 100,
            "methodology": block_methodology_document(),
            "files": {"stderr": file_receipt(log, directory)},
            "telemetry": {},
            "provenance": {"fixture": True},
            "device": {"fixture": True},
            "rejection_reason": "inadequate frequency evidence",
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return directory

    def test_two_accepted_blocks_aggregate_exactly_1000(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_accepted(root, 1, 0, 600, 0)
            self.make_accepted(root, 2, 600, 400, 180)
            values = validate_condition_aggregation(root / "ECDSA-P-256" / "keygen")
            self.assertEqual(len(values), 1000)

    def test_slow_partial_block_is_accepted_with_adequate_frequency_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_accepted(root, 1, 0, 43, 0, requested=1000)
            self.make_accepted(root, 2, 43, 957, 180, requested=957)
            first_manifest = json.loads((first / "manifest.json").read_text())
            self.assertEqual(first_manifest["requested_timing_iterations"], 1000)
            self.assertEqual(first_manifest["accepted_timing_iterations"], 43)
            self.assertEqual(first_manifest["telemetry"]["frequency"]["frequency_sample_count"], 60)
            values = validate_condition_aggregation(root / "ECDSA-P-256" / "keygen")
            self.assertEqual(len(values), 1000)

    def test_rejected_evidence_is_preserved_but_never_aggregated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rejected = self.make_rejected(root, 1, 1, 0)
            self.make_accepted(root, 1, 0, 600, 180, attempt=2)
            self.make_accepted(root, 2, 600, 400, 180)
            values = validate_condition_aggregation(root / "ECDSA-P-256" / "keygen")
            self.assertEqual(len(values), 1000)
            self.assertTrue((rejected / "manifest.json").exists())

    def test_final_singleton_remainder_is_explicitly_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_accepted(root, 1, 0, 999, 0)
            self.make_accepted(root, 2, 999, 1, 180)
            values = validate_condition_aggregation(root / "ECDSA-P-256" / "keygen")
            self.assertEqual(len(values), 1000)


if __name__ == "__main__":
    unittest.main()
