import csv
import hashlib
import json
import statistics
import tempfile
import unittest
from pathlib import Path

from android_e0 import (
    EXPECTED_DEVICE,
    SCHEME_OPERATIONS,
    SCIENTIFIC_LIMITATIONS,
    EvidenceError,
    file_receipt,
)
from android_e0_blocks import (
    BLOCK_ATTEMPT_SCHEMA,
    BLOCK_HARD_MAXIMUM_NS,
    BLOCK_START_BUDGET_NS,
    MONITORING_TARGET_NS,
    accepted_block_reporting_evidence,
    assess_block_frequency,
    block_methodology_document,
    complete_block_telemetry,
    distributed_target_offsets,
    parse_block_phases,
    read_block_frequency,
    validate_condition_aggregation,
)
from android_e0_reporting import (
    ANDROID_V3_RESULT_SCHEMA,
    FREQUENCY_COMPARISON_SCHEMA,
    AndroidConditionResult,
    finalize_android_v3,
    render_android_results,
    render_frequency_comparison,
)

HAL = """Thermal Status: 0
Current temperatures from HAL:
  Temperature{mValue=%s, mType=0, mName=CPU, mStatus=0}
Current cooling devices from HAL:
"""

PROVENANCE = {
    "git_commit": "a" * 40,
    "git_tracked_clean": True,
    "benchmark_sha256": "b" * 64,
    "frequency_sampler_sha256": "c" * 64,
    "go_version_output": "go version go1.27.3 android/arm64",
    "governor": "schedutil",
    "cpu_max_khz": 2_000_000,
    "benchmark_transport": "Termux SSH",
    "telemetry_transport": "ADB; frequency sampled on-device and retrieved after the measured run",
    "adb_serial": "fixture",
    "scientific_limitations": SCIENTIFIC_LIMITATIONS,
}


class V3Fixture:
    @staticmethod
    def condition_dir(root: Path, scheme: str, operation: str) -> Path:
        return root / scheme.replace("+", "_") / operation

    @classmethod
    def accepted(
        cls,
        root: Path,
        scheme: str,
        operation: str,
        block: int,
        start: int,
        timings: list[float],
        cooldown: int,
        *,
        attempt: int = 1,
        frequency_base_khz: int = 2_000_000,
        temperature_before: float = 40.0,
        temperature_after: float = 41.0,
    ) -> Path:
        directory = cls.condition_dir(root, scheme, operation) / f"block_{block:03d}" / f"attempt_{attempt:03d}"
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
        with paths["timing_samples"].open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["iteration", "elapsed_ms"])
            for index, value in enumerate(timings, 1):
                writer.writerow([index, format(value, ".9f")])

        stabilization_start = 1_000_000_000
        stabilization_observations = [
            {
                "scheduled_monotonic_ns": stabilization_start + (26 + index) * 100_000_000,
                "actual_monotonic_ns": stabilization_start + (26 + index) * 100_000_000,
                "freq_khz": frequency_base_khz,
            }
            for index in range(5)
        ]
        ready = stabilization_observations[-1]["actual_monotonic_ns"]
        measured_start = 10_000_000_000
        measured_end = measured_start + MONITORING_TARGET_NS
        phases = [
            {"event": "stabilization_start", "monotonic_ns": stabilization_start},
            {
                "event": "stabilization_ready",
                "monotonic_ns": ready,
                "operation_count": 10_000,
                "stabilization": {
                    "schema": "pqc-energy-trading.e0-android-stabilization.v1",
                    "type": "ready",
                    "token": "fixture-token",
                    "start_monotonic_ns": stabilization_start,
                    "qualified_monotonic_ns": ready,
                    "work_duration_ns": ready - stabilization_start,
                    "minimum_khz": frequency_base_khz,
                    "maximum_khz": frequency_base_khz,
                    "observations": stabilization_observations,
                },
            },
            {"event": "measured_start", "monotonic_ns": measured_start},
            {"event": "measured_end", "monotonic_ns": measured_end},
            {
                "event": "block_summary",
                "monotonic_ns": measured_end,
                "block": {
                    "warmup_iterations": 100,
                    "requested_iterations": len(timings),
                    "completed_iterations": len(timings),
                    "start_budget_ns": BLOCK_START_BUDGET_NS,
                    "hard_maximum_ns": BLOCK_HARD_MAXIMUM_NS,
                    "block_duration_ns": MONITORING_TARGET_NS,
                    "crossed_start_budget": False,
                    "exceeded_hard_maximum": False,
                    "schedule_schema": "pqc-energy-trading.e0-android-sustained-schedule.v1",
                    "scheduling_algorithm": "precomputed linear target offsets; identical untimed primitive operations fill target gaps without sleeps",
                    "monitoring_target_ns": MONITORING_TARGET_NS,
                    "total_primitive_operations": len(timings),
                    "timed_primitive_operations": len(timings),
                    "untimed_primitive_operations": 0,
                },
            },
        ]
        paths["phase_events"].write_text("".join(json.dumps(value) + "\n" for value in phases), encoding="utf-8")

        targets = distributed_target_offsets(len(timings))
        with paths["timed_schedule"].open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["timed_index", "operation_ordinal", "target_offset_ns", "selection_monotonic_ns"])
            for index, target in enumerate(targets):
                writer.writerow([index, index + 1, target, measured_start + target])

        with paths["frequency_samples"].open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                ["scheduled_monotonic_ns", "actual_monotonic_ns", "freq_khz", "success", "missed", "error", "phase"]
            )
            for index in range(60):
                scheduled = measured_start + index * 100_000_000
                writer.writerow(
                    [scheduled, scheduled + 1, frequency_base_khz + index, "true", "false", "", "post_stabilization"]
                )
        paths["temperature_before"].write_text(HAL % temperature_before, encoding="utf-8")
        paths["temperature_after"].write_text(HAL % temperature_after, encoding="utf-8")
        for role in ("stdout", "stderr", "sampler_stdout", "sampler_stderr"):
            paths[role].write_text("", encoding="utf-8")

        phase_evidence = parse_block_phases(paths["phase_events"])
        observations = read_block_frequency(paths["frequency_samples"])
        assessment = assess_block_frequency(observations, measured_start, measured_end)
        manifest = {
            "schema": BLOCK_ATTEMPT_SCHEMA,
            "scientific": True,
            "status": "accepted",
            "scheme": scheme,
            "operation": operation,
            "block_index": block,
            "attempt_index": attempt,
            "requested_timing_iterations": len(timings),
            "completed_timing_iterations": len(timings),
            "accepted_timing_iterations": len(timings),
            "accepted_sample_start": start,
            "accepted_sample_end_exclusive": start + len(timings),
            "cooldown_preceding_seconds": cooldown,
            "cooldown_escalation_seconds": [180, 360, 720],
            "normal_interblock_cooldown_seconds": 180,
            "warmup_iterations": 100,
            "methodology": block_methodology_document(),
            "device": EXPECTED_DEVICE,
            "provenance": PROVENANCE,
            "files": {role: file_receipt(path, directory) for role, path in paths.items()},
            "telemetry": complete_block_telemetry(
                assessment, [temperature_before], [temperature_after], phase_evidence
            ),
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return directory

    @classmethod
    def rejected(cls, root: Path, scheme: str, operation: str, block: int, attempt: int, cooldown: int) -> Path:
        directory = cls.condition_dir(root, scheme, operation) / f"block_{block:03d}" / f"attempt_{attempt:03d}"
        directory.mkdir(parents=True)
        log = directory / "stderr.log"
        log.write_text("frequency rejection\n", encoding="utf-8")
        manifest = {
            "schema": BLOCK_ATTEMPT_SCHEMA,
            "scientific": True,
            "status": "rejected",
            "scheme": scheme,
            "operation": operation,
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
            "device": EXPECTED_DEVICE,
            "provenance": PROVENANCE,
            "files": {"stderr": file_receipt(log, directory)},
            "telemetry": {},
            "rejection_reason": "frequency rejection",
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return directory


class BlockReportingTests(unittest.TestCase):
    def test_one_accepted_block_has_exactly_1000_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = V3Fixture.accepted(root, "ECDSA-P-256", "keygen", 1, 0, [1.0] * 1000, 0)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            evidence = accepted_block_reporting_evidence(directory, manifest)
            self.assertEqual(len(evidence["timings_ms"]), 1000)
            self.assertEqual(len(evidence["frequencies_khz"]), 60)

    def test_multiple_accepted_blocks_total_exactly_1000(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            V3Fixture.accepted(root, "ECDSA-P-256", "keygen", 1, 0, [1.0] * 600, 0)
            V3Fixture.accepted(root, "ECDSA-P-256", "keygen", 2, 600, [2.0] * 400, 180)
            values = validate_condition_aggregation(V3Fixture.condition_dir(root, "ECDSA-P-256", "keygen"))
            self.assertEqual(values, [1.0] * 600 + [2.0] * 400)

    def test_rejected_block_contributes_no_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rejected = V3Fixture.rejected(root, "ECDSA-P-256", "keygen", 1, 1, 0)
            V3Fixture.accepted(root, "ECDSA-P-256", "keygen", 1, 0, [1.0] * 1000, 180, attempt=2)
            values = validate_condition_aggregation(V3Fixture.condition_dir(root, "ECDSA-P-256", "keygen"))
            self.assertEqual(len(values), 1000)
            self.assertTrue((rejected / "manifest.json").exists())


class StatisticsAndComparisonTests(unittest.TestCase):
    @staticmethod
    def results() -> list[AndroidConditionResult]:
        values = tuple(float(index) for index in range(1, 1001))
        return [
            AndroidConditionResult(scheme, operation, values, tuple(range(1_001_000, 1_061_000, 1000)), (40.0, 42.5))
            for scheme, operation in SCHEME_OPERATIONS
        ]

    def test_timing_and_frequency_statistics_use_raw_populations(self):
        content = render_android_results(self.results())
        rows = list(csv.DictReader(content.decode().splitlines()))
        row = rows[0]
        self.assertEqual(row["median_ms"], "500.500000000")
        self.assertEqual(row["p95_ms"], "950.050000000")
        self.assertEqual(row["p99_ms"], "990.010000000")
        self.assertEqual(row["stddev_ms"], format(statistics.stdev(range(1, 1001)), ".9f"))
        self.assertEqual(row["freq_mhz_median"], "1030.500000000")
        self.assertEqual(row["freq_mhz_p05"], "1003.950000000")
        self.assertEqual(row["freq_mhz_p95"], "1057.050000000")
        self.assertEqual(row["temp_c_max"], "42.500000000")

    def test_comparison_uses_only_matching_signature_operations(self):
        results = self.results()
        content = render_android_results(results)
        comparison = list(csv.DictReader(render_frequency_comparison(content).decode().splitlines()))
        self.assertEqual(tuple(comparison[0]), FREQUENCY_COMPARISON_SCHEMA)
        self.assertEqual(len(comparison), 21)
        for row in comparison[:3]:
            self.assertEqual(row["comparison_basis"], "not_comparable_kem_operation")
            self.assertEqual(row["reference_scheme"], "")
            self.assertEqual(row["freq_median_difference_percent"], "")
        for row in comparison[3:]:
            self.assertEqual(row["reference_scheme"], "ECDSA-P-256")
            self.assertEqual(row["reference_operation"], row["operation"])


class CompleteFinalizationTests(unittest.TestCase):
    def populate(self, root: Path, conditions=SCHEME_OPERATIONS):
        for index, (scheme, operation) in enumerate(conditions):
            V3Fixture.accepted(
                root, scheme, operation, 1, 0, [1.0 + index / 1000.0] * 1000, 0,
                frequency_base_khz=1_900_000 + index * 100,
            )

    def test_complete_21_condition_finalization_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            output = Path(temporary) / "android.csv"
            comparison = Path(temporary) / "frequency.csv"
            result_bytes, comparison_bytes = finalize_android_v3(root, output, comparison)
            self.assertEqual(tuple(next(csv.reader(result_bytes.decode().splitlines()))), ANDROID_V3_RESULT_SCHEMA)
            self.assertEqual(tuple(next(csv.reader(comparison_bytes.decode().splitlines()))), FREQUENCY_COMPARISON_SCHEMA)
            self.assertEqual(len(result_bytes.decode().splitlines()), 22)
            self.assertEqual(len(comparison_bytes.decode().splitlines()), 22)
            self.assertEqual(output.read_bytes(), result_bytes)
            self.assertEqual(comparison.read_bytes(), comparison_bytes)

    def test_incomplete_21_condition_set_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root, SCHEME_OPERATIONS[:-1])
            with self.assertRaisesRegex(EvidenceError, "incomplete Android v3 condition set"):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            target = next(root.glob("*/*/block_*/attempt_*/timing_samples.csv"))
            target.write_text(target.read_text(encoding="utf-8") + "1001,1.0\n", encoding="utf-8")
            with self.assertRaises(EvidenceError):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_missing_temperature_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            target = next(root.glob("*/*/block_*/attempt_*/temperature_before.txt"))
            target.unlink()
            with self.assertRaises(EvidenceError):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_missing_frequency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            target = next(root.glob("*/*/block_*/attempt_*/frequency_samples.csv"))
            target.unlink()
            with self.assertRaises(EvidenceError):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_non_v3_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            target = next(root.glob("*/*/block_*/attempt_*/manifest.json"))
            manifest = json.loads(target.read_text(encoding="utf-8"))
            manifest["schema"] = "pqc-energy-trading.e0-android-block-attempt.v2"
            target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "scientific v3"):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_runner_provenance_schema_and_values_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            target = next(root.glob("*/*/block_*/attempt_*/manifest.json"))
            manifest = json.loads(target.read_text(encoding="utf-8"))
            manifest["provenance"]["benchmark_transport"] = "unregistered transport"
            target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceError, "benchmark transport"):
                finalize_android_v3(root, Path(temporary) / "out.csv", Path(temporary) / "comparison.csv")

    def test_historical_server_output_is_unchanged(self):
        repository = Path(__file__).resolve().parents[2]
        historical = repository / "data/e0_primitives.csv"
        before = hashlib.sha256(historical.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            self.populate(root)
            finalize_android_v3(root, Path(temporary) / "android.csv", Path(temporary) / "frequency.csv")
        self.assertEqual(hashlib.sha256(historical.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
