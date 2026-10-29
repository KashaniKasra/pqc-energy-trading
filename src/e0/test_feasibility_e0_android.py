import csv
import hashlib
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess

from feasibility_e0_android import (
    ADB_CPU_MASK,
    FeasibilityError,
    PROJECT_ROOT,
    analyze,
    benchmark_argv,
    create_output_directory,
    parse_args,
    sampler_argv,
    wait_for_ready,
)


class FakeSampler:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeCommands:
    def __init__(self, returncodes):
        self.returncodes = iter(returncodes)
        self.calls = []

    def run(self, command, *, check=True, timeout=None):
        self.calls.append(command)
        return CompletedProcess(command, next(self.returncodes), b"", b"")


def args():
    return Namespace(
        adb="adb",
        adb_serial="05a96d6d0405",
        ssh="ssh",
        scp="scp",
        ssh_target="u0_a21@127.0.0.1",
        ssh_port=8022,
        sampler="/data/local/tmp/e0freq-optimized",
        benchmark="/data/data/com.termux/files/home/pqc-e0-android/android-runner/e0bench-android-v2",
        liboqs_prefix="/data/data/com.termux/files/home/pqc-e0-android/ops-install",
        termux_prefix="/data/data/com.termux/files/usr",
        scheme="ECDSA-P-256",
        operation="verify",
    )


class CommandTests(unittest.TestCase):
    def test_sampler_uses_toybox_mask_and_samples_cpu_seven(self):
        command = sampler_argv(args(), "/f", "/ready", "/stop")
        self.assertEqual(ADB_CPU_MASK, "1")
        self.assertEqual(command[:3], ["taskset", "1", "/data/local/tmp/e0freq-optimized"])
        self.assertNotIn("-c", command)
        self.assertEqual(command[command.index("--cpu") + 1], "7")

    def test_benchmark_is_exactly_one_fixed_condition_on_cpu_seven(self):
        command = benchmark_argv(args(), "/timing", "/phases")
        self.assertIn("GOMAXPROCS=1", command)
        taskset = command.index("taskset")
        self.assertEqual(command[taskset : taskset + 3], ["taskset", "-c", "7"])
        self.assertEqual(command.count("android-condition"), 1)
        self.assertEqual(command[command.index("--scheme") + 1], "ECDSA-P-256")
        self.assertEqual(command[command.index("--operation") + 1], "verify")

    def test_valid_keygen_selection(self):
        selected = args()
        selected.operation = "keygen"
        command = benchmark_argv(selected, "/timing", "/phases")
        self.assertEqual(command[command.index("--scheme") + 1], "ECDSA-P-256")
        self.assertEqual(command[command.index("--operation") + 1], "keygen")

    def test_default_selection_is_backward_compatible(self):
        parsed = parse_args([])
        self.assertEqual((parsed.scheme, parsed.operation), ("ECDSA-P-256", "verify"))

    def test_invalid_condition_is_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--scheme", "ECDSA-P-256", "--operation", "encaps"])

    def test_ready_poll_uses_mocked_adb_and_succeeds(self):
        commands = FakeCommands([1, 0])
        clock = iter([0.0, 0.0, 0.1, 0.2])
        wait_for_ready(
            args(),
            commands,
            FakeSampler(),
            "/ready",
            timeout_seconds=1.0,
            monotonic=lambda: next(clock),
            sleep=lambda _: None,
        )
        self.assertEqual(len(commands.calls), 2)
        self.assertEqual(commands.calls[0][:4], ["adb", "-s", "05a96d6d0405", "shell"])

    def test_ready_poll_rejects_early_sampler_exit(self):
        sampler = FakeSampler()
        sampler.returncode = 1
        with self.assertRaisesRegex(FeasibilityError, "before readiness"):
            wait_for_ready(
                args(),
                FakeCommands([]),
                sampler,
                "/ready",
                timeout_seconds=1.0,
                monotonic=lambda: 0.0,
                sleep=lambda _: None,
            )


class SafetyTests(unittest.TestCase):
    def test_output_directory_is_unique_and_labeled(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            output = create_output_directory(parent, None, "fixed-token")
            self.assertIn("nonscientific-feasibility", output.name)
            self.assertIn("NEVER INCLUDE", (output / "NON_SCIENTIFIC.txt").read_text())
            with self.assertRaises(FileExistsError):
                create_output_directory(parent, output, "unused")

    def test_repository_output_is_refused(self):
        canonical = PROJECT_ROOT / "data" / "e0_primitives.csv"
        before = hashlib.sha256(canonical.read_bytes()).hexdigest()
        with self.assertRaisesRegex(FeasibilityError, "outside the repository"):
            create_output_directory(PROJECT_ROOT, PROJECT_ROOT / "raw" / "feasibility", "unused")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), before)


class AnalysisTests(unittest.TestCase):
    def test_analysis_restricts_frequency_to_measured_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing.csv"
            phases = root / "phases.jsonl"
            frequency = root / "frequency.csv"
            with timing.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["iteration", "elapsed_ms"])
                for index in range(1, 1001):
                    writer.writerow([index, "0.001000000"])
            phases.write_text(
                '{"event":"measured_start","monotonic_ns":1000000000}\n'
                '{"event":"measured_end","monotonic_ns":1019000000}\n'
            )
            with frequency.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["monotonic_ns", "freq_khz", "success", "error"])
                writer.writerow([999000000, 1000000, "true", ""])
                for index in range(20):
                    writer.writerow([1000000000 + index * 1000000, 2000000, "true", ""])
                writer.writerow([1020000000, 1000000, "true", ""])
            result = analyze(timing, phases, frequency)
            self.assertEqual(result.timing_sample_count, 1000)
            self.assertEqual(result.frequency_sample_count, 20)
            self.assertEqual(result.frequency_mhz_minimum, 2000.0)
            self.assertEqual(result.frequency_reference_mhz, 2000.0)
            self.assertEqual(result.frequency_sustained_minimum_mhz, 2000.0)
            self.assertEqual(result.frequency_drop_percent, 0.0)
            self.assertTrue(result.frequency_drop_gate_pass)
            self.assertTrue(result.coverage_gates_pass)

    def test_analysis_reports_three_millisecond_gate_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing.csv"
            phases = root / "phases.jsonl"
            frequency = root / "frequency.csv"
            with timing.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["iteration", "elapsed_ms"])
                for index in range(1, 1001):
                    writer.writerow([index, "0.001000000"])
            phases.write_text(
                '{"event":"measured_start","monotonic_ns":1000000000}\n'
                '{"event":"measured_end","monotonic_ns":1019000000}\n'
            )
            timestamps = [1000000000 + index * 1000000 for index in range(20)]
            timestamps[10:] = [1013300000 + index * 1000000 for index in range(10)]
            with frequency.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["monotonic_ns", "freq_khz", "success", "error"])
                for timestamp in timestamps:
                    writer.writerow([timestamp, 2000000, "true", ""])
            result = analyze(timing, phases, frequency)
            self.assertEqual(result.gaps_above_3ms, 1)
            self.assertFalse(result.coverage_gates_pass)

    def test_frequency_drop_reporting_uses_registered_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing.csv"
            phases = root / "phases.jsonl"
            frequency = root / "frequency.csv"
            with timing.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["iteration", "elapsed_ms"])
                for index in range(1, 1001):
                    writer.writerow([index, "0.001000000"])
            phases.write_text(
                '{"event":"measured_start","monotonic_ns":1000000000}\n'
                '{"event":"measured_end","monotonic_ns":1019000000}\n'
            )
            with frequency.open("w", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["monotonic_ns", "freq_khz", "success", "error"])
                for index in range(20):
                    value = 2000000 if index < 4 else 1700000
                    writer.writerow([1000000000 + index * 1000000, value, "true", ""])
            result = analyze(timing, phases, frequency)
            self.assertEqual(result.frequency_baseline_samples, 4)
            self.assertEqual(result.frequency_rolling_window_samples, 3)
            self.assertEqual(result.frequency_reference_mhz, 2000.0)
            self.assertEqual(result.frequency_sustained_minimum_mhz, 1700.0)
            self.assertAlmostEqual(result.frequency_drop_percent, 15.0)
            self.assertFalse(result.frequency_drop_gate_pass)


if __name__ == "__main__":
    unittest.main()
