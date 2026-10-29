import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess

from pilot_e0_android_overhead import (
    AttemptResult,
    PILOT_ORDER,
    cleanup_remote,
    paired_effects,
    parse_args,
    relative_difference_percent,
    run_pilot,
    unavailable_telemetry,
)


class FakeCommands:
    pass


class PairingTests(unittest.TestCase):
    def test_paired_effects_are_sampler_on_relative_to_off(self):
        results = [
            AttemptResult(1, 1, "on", 1.1, 11.0, "b1/on"),
            AttemptResult(1, 2, "off", 1.0, 10.0, "b1/off"),
            AttemptResult(2, 1, "off", 2.0, 20.0, "b2/off"),
            AttemptResult(2, 2, "on", 1.8, 18.0, "b2/on"),
        ]
        effects = paired_effects(results)
        self.assertEqual(effects[0]["execution_order"], ["on", "off"])
        self.assertEqual(effects[0]["median_latency_relative_difference_percent"], "10.000000000")
        self.assertEqual(effects[1]["execution_order"], ["off", "on"])
        self.assertEqual(effects[1]["median_latency_relative_difference_percent"], "-10.000000000")
        self.assertEqual(relative_difference_percent(11, 10), 10.0)

    def test_missing_sampler_mode_is_rejected(self):
        results = [
            AttemptResult(1, 1, "on", 1.0, 1.0, "one"),
            AttemptResult(1, 2, "on", 1.0, 1.0, "two"),
        ]
        with self.assertRaisesRegex(Exception, "exactly one"):
            paired_effects(results)


class OrchestrationTests(unittest.TestCase):
    def test_counterbalanced_two_block_order_and_recovery(self):
        calls = []
        sleeps = []

        def attempt_runner(args, commands, pilot, block, order, mode):
            calls.append((block, order, mode))
            value = 1.1 if mode == "on" else 1.0
            return AttemptResult(block, order, mode, value, value * 10, f"b{block}/{mode}")

        with tempfile.TemporaryDirectory() as temporary:
            arguments = parse_args(["--output-parent", temporary, "--recovery-seconds", "7"])
            # Environment collection is patched at its module boundary; no real
            # subprocess or device command is allowed in this test.
            import pilot_e0_android_overhead as module

            original = module.collect_environment
            module.collect_environment = lambda args, commands: {"mocked": True}
            try:
                output = run_pilot(
                    arguments,
                    FakeCommands(),
                    attempt_runner=attempt_runner,
                    sleep=sleeps.append,
                )
            finally:
                module.collect_environment = original
            self.assertEqual(calls, [(1, 1, "on"), (1, 2, "off"), (2, 1, "off"), (2, 2, "on")])
            self.assertEqual(sleeps, [7, 7, 7])
            summary = json.loads((output / "pilot_summary.json").read_text())
            self.assertFalse(summary["scientific"])
            self.assertEqual(summary["paired_blocks"], 2)

    def test_default_recovery_and_fixed_condition(self):
        arguments = parse_args([])
        self.assertEqual(arguments.recovery_seconds, 180)
        self.assertEqual((arguments.scheme, arguments.operation), ("ECDSA-P-256", "keygen"))
        self.assertEqual(PILOT_ORDER, (("on", "off"), ("off", "on")))


class CleanupTests(unittest.TestCase):
    def test_owned_remote_paths_are_cleaned_with_mocked_commands(self):
        calls = []

        class Commands:
            def run(self, command, *, check=True, timeout=None):
                calls.append((command, check))
                return CompletedProcess(command, 0, b"", b"")

        arguments = parse_args([])
        arguments.benchmark = "/home/bench"
        arguments.liboqs_prefix = "/home/oqs"
        arguments.termux_prefix = "/prefix"
        cleanup_remote(
            arguments,
            Commands(),
            remote_dir="/home/unique",
            remote_dir_owned=True,
            frequency="/data/frequency",
            ready="/data/ready",
            stop="/data/stop",
            adb_paths_owned=True,
        )
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0][1])
        self.assertIn("/home/unique", calls[0][0][-1])
        self.assertEqual(calls[1][0][-3:], ["/data/frequency", "/data/ready", "/data/stop"])
        self.assertFalse(calls[1][1])

    def test_unowned_remote_paths_are_never_removed(self):
        class Commands:
            def run(self, command, *, check=True, timeout=None):
                raise AssertionError("cleanup must not run for unowned paths")

        arguments = parse_args([])
        cleanup_remote(
            arguments,
            Commands(),
            remote_dir="/preexisting",
            remote_dir_owned=False,
            frequency="/preexisting-frequency",
            ready="/preexisting-ready",
            stop="/preexisting-stop",
            adb_paths_owned=False,
        )

    def test_sampler_off_telemetry_is_explicitly_unavailable(self):
        telemetry = unavailable_telemetry()
        self.assertEqual(telemetry["availability"], "unavailable")
        self.assertIn("intentionally disabled", telemetry["reason"])
        self.assertNotIn("frequency_mhz", telemetry)


if __name__ == "__main__":
    unittest.main()
