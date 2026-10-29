import csv
import hashlib
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess

from android_e0 import (
    ANDROID_RESULT_SCHEMA,
    ATTEMPT_SCHEMA,
    COOLDOWN_SECONDS,
    EXPECTED_DEVICE,
    EvidenceError,
    SCHEME_OPERATIONS,
    SCIENTIFIC_LIMITATIONS,
    assess_frequency,
    file_receipt,
    finalize_android,
    methodology_manifest_value,
    parse_current_hal_cpu_temperatures,
)
from run_e0_android import (
    ADB_SAMPLER_CPU_MASK,
    ANDROID_SOURCE_PATHS,
    CollectionError,
    _benchmark_argv,
    _resolve_termux_path,
    _sampler_argv,
    _termux_argv,
    _validate_android_go_version,
    _validate_git_provenance,
    _validate_deployed_binary_hash,
    _wait_for_sampler,
    create_attempt_directory,
    create_new_evidence_root,
)


HAL = """Thermal Status: 0
Current temperatures from HAL:
  Temperature{mValue=38.5, mType=0, mName=CPU, mStatus=0}
  Temperature{mValue=31.0, mType=2, mName=BATTERY, mStatus=0}
Current cooling devices from HAL:
"""


def frequency_samples(values, start=1_000_000_000):
    return [(start + index * 1_000_000, value) for index, value in enumerate(values)]


class FrequencyTests(unittest.TestCase):
    def test_exactly_ten_percent_drop_is_accepted(self):
        values = [2_000_000] * 4 + [1_800_000] * 16
        result = assess_frequency(frequency_samples(values), 1_000_000_000, 1_019_000_000)
        self.assertAlmostEqual(result.drop_percent, 10.0)
        self.assertTrue(result.accepted)

    def test_greater_than_ten_percent_drop_is_rejected(self):
        values = [2_000_000] * 4 + [1_799_000] * 16
        result = assess_frequency(frequency_samples(values), 1_000_000_000, 1_019_000_000)
        self.assertGreater(result.drop_percent, 10.0)
        self.assertFalse(result.accepted)

    def test_single_low_reading_is_not_a_sustained_drop(self):
        values = [2_000_000] * 20
        values[10] = 1_000_000
        result = assess_frequency(frequency_samples(values), 1_000_000_000, 1_019_000_000)
        self.assertTrue(result.accepted)
        self.assertEqual(result.drop_percent, 0.0)

    def test_insufficient_samples_fail_closed(self):
        with self.assertRaises(EvidenceError):
            assess_frequency(frequency_samples([2_000_000] * 9), 1_000_000_000, 1_008_000_000)

    def test_sampling_gap_fails_closed(self):
        samples = frequency_samples([2_000_000] * 20)
        samples[10] = (samples[9][0] + 4_000_000, samples[10][1])
        for index in range(11, 20):
            samples[index] = (samples[index - 1][0] + 1_000_000, samples[index][1])
        with self.assertRaises(EvidenceError):
            assess_frequency(samples, samples[0][0], samples[-1][0])


class TemperatureTests(unittest.TestCase):
    def test_only_current_hal_cpu_temperature_is_used(self):
        text = "Cached temperatures:\n  Temperature{mValue=99.0, mType=0, mName=CPU}\n" + HAL
        self.assertEqual(parse_current_hal_cpu_temperatures(text), [38.5])

    def test_missing_cpu_temperature_fails_closed(self):
        with self.assertRaises(EvidenceError):
            parse_current_hal_cpu_temperatures(HAL.replace("mType=0", "mType=2"))


class OutputSafetyTests(unittest.TestCase):
    def test_registered_cooldown_is_three_minutes(self):
        self.assertEqual(COOLDOWN_SECONDS, 180)
        methodology = methodology_manifest_value()
        self.assertEqual(methodology["cooldown_between_schemes_seconds"], 180)
        self.assertEqual(methodology["cooldown_before_rejected_retry_seconds"], 180)

    def test_evidence_root_must_be_new_and_external(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            project.mkdir()
            external = base / "evidence"
            self.assertEqual(create_new_evidence_root(external, project), external)
            with self.assertRaises(CollectionError):
                create_new_evidence_root(external, project)
            with self.assertRaises(CollectionError):
                create_new_evidence_root(project / "raw", project)

    def test_attempt_directory_refuses_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_attempt_directory(root, "ML-DSA-65", "sign", 1)
            with self.assertRaises(FileExistsError):
                create_attempt_directory(root, "ML-DSA-65", "sign", 1)


class IntegrationCommandTests(unittest.TestCase):
    def args(self):
        return Namespace(
            device_sampler="/data/local/tmp/e0freq-optimized",
            device_benchmark="/data/data/com.termux/files/home/e0bench",
            device_liboqs_prefix="/data/data/com.termux/files/home/pqc-e0-android/ops-install",
            termux_prefix="/data/data/com.termux/files/usr",
            stabilization_port=37991,
            stabilization_timeout_seconds=30,
        )

    def test_adb_toybox_sampler_uses_cpu_zero_hex_mask(self):
        command = _sampler_argv(self.args(), "/tmp/f.csv", "/tmp/ready", "/tmp/stop")
        self.assertEqual(ADB_SAMPLER_CPU_MASK, "1")
        self.assertEqual(command[:3], ["taskset", "1", "/data/local/tmp/e0freq-optimized"])
        self.assertNotIn("-c", command)
        self.assertEqual(command[command.index("--cpu") + 1], "7")

    def test_scientific_sampler_uses_100ms_and_tokenized_loopback_coordination(self):
        arguments = self.args()
        arguments.stabilization_port = 37991
        arguments.stabilization_timeout_seconds = 30
        command = _sampler_argv(arguments, "/freq", "/ready", "/stop", "unique-token")
        self.assertEqual(command[command.index("--interval-ms") + 1], "100")
        self.assertEqual(command[command.index("--stabilization-listen") + 1], "127.0.0.1:37991")
        self.assertEqual(command[command.index("--stabilization-token") + 1], "unique-token")
        self.assertEqual(command[command.index("--stabilization-timeout") + 1], "30s")

    def test_legacy_sampler_command_remains_one_millisecond(self):
        command = _sampler_argv(self.args(), "/freq", "/ready", "/stop")
        self.assertEqual(command[command.index("--interval-ms") + 1], "1")
        self.assertNotIn("--stabilization-listen", command)

    def test_scientific_benchmark_command_matches_v3_go_cli(self):
        command = _benchmark_argv(
            self.args(), "ECDSA-P-256", "keygen", "/timing", "/phases", "/schedule", "token", 17
        )
        self.assertEqual(command[:6], ["env", "GOMAXPROCS=1", "taskset", "-c", "7", self.args().device_benchmark])
        expected = {
            "--scheme": "ECDSA-P-256",
            "--operation": "keygen",
            "--raw-output": "/timing",
            "--phase-output": "/phases",
            "--schedule-output": "/schedule",
            "--stabilization-address": "127.0.0.1:37991",
            "--stabilization-token": "token",
            "--stabilization-timeout": "30s",
            "--requested-iterations": "17",
            "--block-start-budget": "59s",
        }
        self.assertEqual(command[6], "android-condition")
        for flag, value in expected.items():
            self.assertEqual(command[command.index(flag) + 1], value)

    def test_deployed_binary_hash_must_match_approved_fresh_build(self):
        digest = "a" * 64
        _validate_deployed_binary_hash("benchmark", digest, digest)
        with self.assertRaisesRegex(CollectionError, "approved fresh build"):
            _validate_deployed_binary_hash("benchmark", digest, "b" * 64)
        with self.assertRaisesRegex(CollectionError, "invalid expected"):
            _validate_deployed_binary_hash("benchmark", "not-a-hash", digest)

    def test_android_go_version_must_match_finalizer_provenance_exactly(self):
        _validate_android_go_version("go version go1.27.3 android/arm64")
        with self.assertRaisesRegex(CollectionError, "unexpected Go version"):
            _validate_android_go_version("go version go1.27.30 android/arm64")

    def test_termux_environment_is_explicit_for_noninteractive_ssh(self):
        command = _termux_argv(self.args(), ["pkg-config", "--modversion", "liboqs"])
        self.assertEqual(
            command[:4],
            [
                "env",
                "PATH=/data/data/com.termux/files/usr/bin",
                "LD_LIBRARY_PATH=/data/data/com.termux/files/home/pqc-e0-android/ops-install/lib",
                "PKG_CONFIG_PATH=/data/data/com.termux/files/home/pqc-e0-android/ops-install/lib/pkgconfig",
            ],
        )
        self.assertEqual(command[4:], ["pkg-config", "--modversion", "liboqs"])

    def test_termux_home_path_resolution_is_explicit(self):
        home = "/data/data/com.termux/files/home"
        self.assertEqual(
            _resolve_termux_path("~/pqc-e0-android/android-runner/e0bench-android-v2", home),
            f"{home}/pqc-e0-android/android-runner/e0bench-android-v2",
        )
        with self.assertRaises(CollectionError):
            _resolve_termux_path("relative/path", home)

    def test_provenance_rejects_uncommitted_android_source(self):
        class FakeCommands:
            def __init__(self):
                self.calls = []

            def run(self, command, *, check=True):
                self.calls.append(command)
                if "ls-files" in command:
                    return CompletedProcess(command, 1, b"", b"untracked")
                return CompletedProcess(command, 0, b"", b"")

        commands = FakeCommands()
        with self.assertRaisesRegex(CollectionError, "must be committed"):
            _validate_git_provenance(commands, Path("/project"))
        tracked_call = next(call for call in commands.calls if "ls-files" in call)
        self.assertEqual(tuple(tracked_call[-len(ANDROID_SOURCE_PATHS) :]), ANDROID_SOURCE_PATHS)
        self.assertIn("src/e0/android_e0_blocks.py", ANDROID_SOURCE_PATHS)

    def test_provenance_rejects_other_untracked_e0_source(self):
        class FakeCommands:
            def run(self, command, *, check=True):
                if "--others" in command:
                    return CompletedProcess(command, 0, b"src/e0/untracked.go\n", b"")
                return CompletedProcess(command, 0, b"", b"")

        with self.assertRaisesRegex(CollectionError, "untracked E0 source"):
            _validate_git_provenance(FakeCommands(), Path("/project"))

    def test_sampler_timeout_terminates_process(self):
        class FakeProcess:
            def __init__(self):
                self.calls = 0
                self.terminated = False
                self.killed = False

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise __import__("subprocess").TimeoutExpired("adb", timeout)
                return b"", b""

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with self.assertRaisesRegex(CollectionError, "did not stop"):
            _wait_for_sampler(process, timeout=0.01)
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)


class FinalizerTests(unittest.TestCase):
    def make_attempt(self, root: Path, scheme: str, operation: str, attempt=1):
        safe_scheme = "".join(character if character.isalnum() or character in "_.-" else "_" for character in scheme)
        directory = root / safe_scheme / operation / f"attempt_{attempt:03d}"
        directory.mkdir(parents=True)
        paths = {
            "timing_samples": directory / "timing_samples.csv",
            "phase_events": directory / "phase_events.jsonl",
            "frequency_samples": directory / "frequency_samples.csv",
            "temperature_before": directory / "temperature_before.txt",
            "temperature_after": directory / "temperature_after.txt",
            "stdout": directory / "stdout.log",
            "stderr": directory / "stderr.log",
        }
        with paths["timing_samples"].open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["iteration", "elapsed_ms"])
            for index in range(1, 1001):
                writer.writerow([index, "1.000000000"])
        paths["phase_events"].write_text(
            '{"event":"measured_start","monotonic_ns":1000000000}\n'
            '{"event":"measured_end","monotonic_ns":1019000000}\n',
            encoding="utf-8",
        )
        with paths["frequency_samples"].open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["monotonic_ns", "freq_khz", "success", "error"])
            for timestamp, value in frequency_samples([2_000_000] * 20):
                writer.writerow([timestamp, value, "true", ""])
        paths["temperature_before"].write_text(HAL, encoding="utf-8")
        paths["temperature_after"].write_text(HAL.replace("38.5", "39.0"), encoding="utf-8")
        paths["stdout"].write_text("ok\n", encoding="utf-8")
        paths["stderr"].write_text("", encoding="utf-8")
        manifest = {
            "schema": ATTEMPT_SCHEMA,
            "scientific": True,
            "status": "accepted",
            "attempt": attempt,
            "scheme": scheme,
            "operation": operation,
            "device": EXPECTED_DEVICE,
            "methodology": methodology_manifest_value(),
            "provenance": {
                "git_commit": "a" * 40,
                "git_tracked_clean": True,
                "benchmark_sha256": "b" * 64,
                "frequency_sampler_sha256": "c" * 64,
                "governor": "schedutil",
                "cpu_max_khz": 2000000,
                "scientific_limitations": SCIENTIFIC_LIMITATIONS,
            },
            "files": {role: file_receipt(path, directory) for role, path in paths.items()},
            "telemetry": {
                "frequency_sample_count": 20,
                "frequency_mhz_median": "2000.000000000",
                "frequency_reference_mhz": "2000.000000000",
                "frequency_sustained_min_mhz": "2000.000000000",
                "frequency_drop_percent": "0.000000000",
                "frequency_max_gap_ns": 1000000,
                "temperature_before_cpu_c": ["38.500000000"],
                "temperature_after_cpu_c": ["39.000000000"],
                "temp_c_max": "39.000000000",
            },
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def test_complete_finalization_is_deterministic_and_standalone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            for scheme, operation in SCHEME_OPERATIONS:
                self.make_attempt(root, scheme, operation)
            first = Path(temporary) / "android-first.csv"
            second = Path(temporary) / "android-second.csv"
            first_content = finalize_android(root, first)
            second_content = finalize_android(root, second)
            self.assertEqual(first_content, second_content)
            self.assertEqual(hashlib.sha256(first_content).hexdigest(), hashlib.sha256(second_content).hexdigest())
            rows = list(csv.reader(first_content.decode().splitlines()))
            self.assertEqual(tuple(rows[0]), ANDROID_RESULT_SCHEMA)
            self.assertEqual(len(rows), 22)
            self.assertTrue(all(row[0] == "xiaomi_redmi_note_9_m2003j15sc" for row in rows[1:]))

    def test_finalizer_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            for scheme, operation in SCHEME_OPERATIONS:
                self.make_attempt(root, scheme, operation)
            output = Path(temporary) / "existing.csv"
            output.write_bytes(b"preserve me")
            with self.assertRaises(FileExistsError):
                finalize_android(root, output)
            self.assertEqual(output.read_bytes(), b"preserve me")

    def test_rejected_attempt_is_preserved_but_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            for scheme, operation in SCHEME_OPERATIONS:
                self.make_attempt(root, scheme, operation)
            accepted_manifest_path = next(root.glob("*/*/attempt_001/manifest.json"))
            accepted = json.loads(accepted_manifest_path.read_text(encoding="utf-8"))
            rejected_dir = accepted_manifest_path.parent.parent / "attempt_002"
            rejected_dir.mkdir()
            log = rejected_dir / "stderr.log"
            log.write_text("sustained frequency drop\n", encoding="utf-8")
            rejected = dict(accepted)
            rejected.update(
                {
                    "status": "rejected",
                    "attempt": 2,
                    "files": {"stderr": file_receipt(log, rejected_dir)},
                    "telemetry": {},
                    "rejection_reason": "sustained frequency drop exceeds 10%",
                }
            )
            (rejected_dir / "manifest.json").write_text(
                json.dumps(rejected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            output = Path(temporary) / "android.csv"
            content = finalize_android(root, output)
            self.assertEqual(len(list(csv.reader(content.decode().splitlines()))), 22)
            self.assertTrue((rejected_dir / "manifest.json").exists())

    def test_missing_condition_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            for scheme, operation in SCHEME_OPERATIONS[:-1]:
                self.make_attempt(root, scheme, operation)
            with self.assertRaises(EvidenceError):
                finalize_android(root, Path(temporary) / "out.csv")

    def test_tampered_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            for scheme, operation in SCHEME_OPERATIONS:
                self.make_attempt(root, scheme, operation)
            target = next(root.glob("*/*/attempt_001/timing_samples.csv"))
            target.write_text(target.read_text(encoding="utf-8") + "1001,1.0\n", encoding="utf-8")
            with self.assertRaises(EvidenceError):
                finalize_android(root, Path(temporary) / "out.csv")


if __name__ == "__main__":
    unittest.main()
