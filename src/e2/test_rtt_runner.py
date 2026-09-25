import argparse
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


REPO_ROOT = Path(__file__).parents[2]
RUNNER_PATH = REPO_ROOT / "env/mininet/e2/run_e2.py"
specification = importlib.util.spec_from_file_location("e2_rtt_runner_test", RUNNER_PATH)
runner = importlib.util.module_from_spec(specification)
specification.loader.exec_module(runner)


def ping_output(value):
    return "\n".join(
        f"64 bytes from 10.20.0.2: icmp_seq={index} ttl=64 time={value} ms"
        for index in range(1, 4)
    )


def arguments(**overrides):
    values = {
        "config": "classical",
        "transition": "penalty",
        "warmup": 1,
        "iterations": 3,
        "ping_samples": 3,
        "output_root": "unused",
        "preflight": True,
        "scientific": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write_raw(path, *, scientific, warmup, measured, failure=False):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(runner.RAW_FIELDS)
        for phase, count in (("warmup", warmup), ("measured", measured)):
            for iteration in range(count):
                failed = failure and phase == "measured" and iteration == 0
                writer.writerow(
                    (
                        "classical",
                        "penalty",
                        iteration,
                        phase,
                        str(scientific).lower(),
                        222,
                        "20.000000",
                        str(not failed).lower(),
                        "verification failed" if failed else "",
                    )
                )


class E2RunnerTests(unittest.TestCase):
    def test_mininet_version_captures_stderr_and_rejects_empty_or_failed(self):
        def completed(returncode=0, stdout="", stderr=""):
            return SimpleNamespace(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(
            runner.detect_mininet_version(
                lambda *args, **kwargs: completed(stderr="2.3.0\n")
            ),
            "2.3.0",
        )
        self.assertEqual(
            runner.detect_mininet_version(
                lambda *args, **kwargs: completed(stdout="Mininet 2.4.1\n")
            ),
            "2.4.1",
        )
        with self.assertRaisesRegex(RuntimeError, "was empty"):
            runner.detect_mininet_version(
                lambda *args, **kwargs: completed()
            )
        with self.assertRaisesRegex(RuntimeError, "exited 1"):
            runner.detect_mininet_version(
                lambda *args, **kwargs: completed(returncode=1, stderr="failed")
            )

    def test_scientific_provenance_rejects_empty_mininet_version(self):
        runner.validate_scientific_software_provenance(
            {"software": {"mininet": "2.3.0"}}
        )
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "Mininet version"):
                    runner.validate_scientific_software_provenance(
                        {"software": {"mininet": value}}
                    )

    def test_topology_is_one_direct_twenty_ms_link(self):
        topology = runner.E2RTTTopology()
        self.assertCountEqual(topology.hosts(), ["a", "b"])
        self.assertEqual(topology.switches(), [])
        links = topology.links(withInfo=True)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][2]["delay"], "10.000ms")

    def test_ping_gate_is_strictly_below_ten_percent(self):
        self.assertTrue(runner.verify_ping(ping_output(20), 3)["pass"])
        self.assertTrue(runner.verify_ping(ping_output(21.99), 3)["pass"])
        self.assertFalse(runner.verify_ping(ping_output(22), 3)["pass"])
        self.assertFalse(runner.verify_ping(ping_output(18), 3)["pass"])
        with self.assertRaises(ValueError):
            runner.verify_ping(ping_output(20), 4)

    def test_scientific_guard_and_preflight_final_output_guard(self):
        with self.assertRaisesRegex(ValueError, "at least 1000"):
            runner.validate_args(arguments(preflight=False, scientific=True, iterations=999))
        with self.assertRaisesRegex(ValueError, "never writes"):
            runner.validate_args(arguments(output_root="data/e2_statemachine.csv"))
        runner.validate_args(arguments())

    def test_raw_audit_preserves_warmup_and_rejects_scientific_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "samples.csv"
            write_raw(raw, scientific=False, warmup=1, measured=3)
            audit = runner._audit_raw(raw, arguments())
            self.assertEqual(audit["warmup_count"], 1)
            self.assertEqual(audit["measured_count"], 3)
            self.assertEqual(audit["message_bytes_values"], [222])

            failed = Path(temporary) / "failed.csv"
            write_raw(failed, scientific=True, warmup=100, measured=1000, failure=True)
            retained_audit = runner._audit_raw(
                failed,
                arguments(
                    preflight=False,
                    scientific=True,
                    warmup=100,
                    iterations=1000,
                ),
                enforce_scientific_completeness=False,
            )
            self.assertEqual(retained_audit["failure_count"], 1)
            with self.assertRaisesRegex(ValueError, "failures"):
                runner._audit_raw(
                    failed,
                    arguments(
                        preflight=False,
                        scientific=True,
                        warmup=100,
                        iterations=1000,
                    ),
                )

    def test_manifest_hashes_and_collision_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "temporary.csv"
            binary = root / "e2rtt"
            binary.write_bytes(b"test binary")
            write_raw(raw, scientific=True, warmup=100, measured=1000)
            args = arguments(
                preflight=False,
                scientific=True,
                warmup=100,
                iterations=1000,
                output_root=str(root / "evidence"),
            )
            audit = runner._audit_raw(raw, args)
            ping_text = ping_output(20)
            ping = runner.verify_ping(ping_text, 3)
            detected_mininet = runner.detect_mininet_version(
                lambda *args, **kwargs: SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="2.3.0\n",
                )
            )
            manifest_path = runner._persist(
                Path(args.output_root),
                args,
                ping_text,
                ping,
                raw,
                audit,
                {
                    "software": {
                        "git_commit": "test",
                        "mininet": detected_mininet,
                    }
                },
                binary,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["environment"]["software"]["mininet"], "2.3.0")
            ping_path = manifest_path.parent / manifest["ping_verification"]["filename"]
            raw_path = manifest_path.parent / manifest["samples"]["filename"]
            self.assertEqual(ping_path.read_text(encoding="utf-8"), ping_text)
            self.assertEqual(
                hashlib.sha256(ping_path.read_bytes()).hexdigest(),
                manifest["ping_verification"]["raw_output_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                manifest["samples"]["sha256"],
            )
            tampered_ping = dict(ping)
            tampered_ping["raw_output_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "does not match"):
                runner._persist(
                    root / "tampered",
                    args,
                    ping_text,
                    tampered_ping,
                    raw,
                    audit,
                    {},
                    binary,
                )
            with self.assertRaises(FileExistsError):
                runner._persist(
                    Path(args.output_root), args, ping_text, ping, raw, audit, {}, binary
                )


if __name__ == "__main__":
    unittest.main()
