import copy
import csv
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from src.e9 import finalize_e9 as finalizer
from src.e9 import freeze_e9 as freezer


def valid_timing_environment():
    def policy(value):
        return {
            "status": "available",
            "values_by_policy": {"policy0": value, "policy1": value},
            "missing_policies": [],
        }

    return {
        "cpu": {
            "model": {"status": "available", "value": "Test CPU"},
            "online": {
                "status": "available",
                "cpu_list": "0-1",
                "cpus": [0, 1],
                "count": 2,
            },
            "policy_names": ["policy0", "policy1"],
            "scaling_driver": policy("test-driver"),
            "governor": policy("performance"),
            "energy_performance_preference": policy("performance"),
            "scaling_min_freq_khz": policy("3200000"),
            "scaling_max_freq_khz": policy("3200000"),
            "boost": {
                "status": "available",
                "path": "/test/boost",
                "value": "0",
                "enabled": False,
            },
            "process_affinity": {"status": "available", "cpus": [0]},
        },
        "power": {
            "ac_online": {
                "status": "available",
                "sources": {"AC": 1},
                "online": True,
            }
        },
    }


def _ping_bytes(expected):
    return "".join(
        f"64 bytes from test: icmp_seq={index} ttl=64 time={expected:g} ms\n"
        for index in range(1, finalizer.PING_SAMPLES + 1)
    ).encode()


def _completion_bytes(config, rtt_ms, hops):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(finalizer.RAW_FIELDS)
    for phase, count in (
        (finalizer.PHASE_WARMUP, finalizer.WARMUP_ITERATIONS),
        (finalizer.PHASE_MEASURED, finalizer.MEASURED_ITERATIONS),
    ):
        for iteration in range(count):
            timing = rtt_ms * hops + (iteration + 1) / 1_000_000
            writer.writerow(
                (config, rtt_ms, hops, iteration, phase, "true", timing, "true", "")
            )
    return output.getvalue().encode()


def write_plain_evidence(root):
    environment = {
        "software": {
            "git_commit": finalizer.MEASUREMENT_COMMIT,
            "git_dirty": False,
            "kernel": "test-kernel",
            "mininet": "2.3.0",
            "python": "3.12.3",
            "tc": "test-tc",
        },
        "timing": valid_timing_environment(),
    }
    for config in finalizer.CONFIG_VALUES:
        for rtt_ms in finalizer.RTT_VALUES_MS:
            for hops in finalizer.HOP_VALUES:
                stem = finalizer.condition_stem(config, rtt_ms, hops)
                expected = float(rtt_ms * hops)
                ping = _ping_bytes(expected)
                completion = _completion_bytes(config, rtt_ms, hops)
                ping_name = f"ping_{stem}.txt"
                completion_name = f"completion_{stem}.csv"
                (root / ping_name).write_bytes(ping)
                (root / completion_name).write_bytes(completion)
                manifest = {
                    "schema": finalizer.CONDITION_SCHEMA,
                    "scientific": True,
                    "condition": {
                        "config": config,
                        "configured_per_channel_rtt_ms": rtt_ms,
                        "per_channel_rtt_compensation_ms": 0.75,
                        "effective_one_way_tc_delay_ms": (rtt_ms - 0.75) / 2,
                        "hops": hops,
                        "expected_end_to_end_rtt_ms": expected,
                        "prepared_message_bytes": finalizer.MESSAGE_BYTES_BY_CONFIG[config],
                        "warmup_iterations": 100,
                        "measured_iterations": 1000,
                    },
                    "ping_verification": {
                        "filename": ping_name,
                        "sha256": finalizer.sha256(ping),
                        "bytes": len(ping),
                        "sample_count": 20,
                        "samples_ms": [expected] * 20,
                        "measured_end_to_end_rtt_ms": expected,
                        "deviation_pct": 0.0,
                        "pass": True,
                    },
                    "completion_samples": {
                        "filename": completion_name,
                        "sha256": finalizer.sha256(completion),
                        "bytes": len(completion),
                        "row_count": 1100,
                    },
                    "environment": environment,
                }
                (root / f"manifest_{stem}.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )


class MemoryEvidence:
    def __init__(self, contents):
        self.contents = contents

    def read(self, name):
        return self.contents[name]


class FinalizeE9Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.source = Path(cls.temporary.name) / "source"
        cls.source.mkdir()
        write_plain_evidence(cls.source)
        cls.base = finalizer.EvidenceRoot(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def _condition_evidence(self):
        stem = finalizer.condition_stem("classical", 5, 1)
        names = (
            f"manifest_{stem}.json",
            f"completion_{stem}.csv",
            f"ping_{stem}.txt",
        )
        return stem, {name: self.base.read(name) for name in names}

    def _refresh_identity(self, manifest, role, content):
        section = manifest[role]
        section["bytes"] = len(content)
        section["sha256"] = finalizer.sha256(content)

    def test_plain_and_frozen_finalization_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain_output = root / "plain.csv"
            finalizer.finalize(self.source, plain_output)
            with plain_output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.reader(source))
            self.assertEqual(tuple(rows[0]), finalizer.FINAL_FIELDS)
            self.assertNotIn("p99", rows[0])
            self.assertEqual(len(rows), 61)
            expected_keys = [
                (config, str(rtt_ms), str(hops))
                for config in finalizer.CONFIG_VALUES
                for rtt_ms in finalizer.RTT_VALUES_MS
                for hops in finalizer.HOP_VALUES
            ]
            self.assertEqual([tuple(row[:3]) for row in rows[1:]], expected_keys)

            frozen_one = root / "frozen-one"
            frozen_two = root / "frozen-two"
            freezer.freeze(self.source, frozen_one)
            freezer.freeze(self.source, frozen_two)
            self.assertEqual(
                {path.name: path.read_bytes() for path in frozen_one.iterdir()},
                {path.name: path.read_bytes() for path in frozen_two.iterdir()},
            )
            self.assertEqual(len(list(frozen_one.glob("*.gz"))), 180)
            frozen_output = root / "frozen.csv"
            finalizer.finalize(frozen_one, frozen_output)
            self.assertEqual(plain_output.read_bytes(), frozen_output.read_bytes())

    def test_deterministic_gzip_uses_no_filename_or_timestamp(self):
        compressed = freezer.deterministic_gzip(b"scientific evidence\n")
        self.assertEqual(compressed[:3], b"\x1f\x8b\x08")
        self.assertEqual(compressed[3] & 0x08, 0)
        self.assertEqual(compressed[4:8], b"\0\0\0\0")

    def test_condition_provenance_and_samples_fail_closed(self):
        stem, base_contents = self._condition_evidence()
        manifest_name = f"manifest_{stem}.json"
        completion_name = f"completion_{stem}.csv"
        ping_name = f"ping_{stem}.txt"

        def expect_failure(label, mutation, pattern):
            contents = dict(base_contents)
            manifest = json.loads(contents[manifest_name])
            mutation(manifest, contents)
            contents[manifest_name] = (
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            ).encode()
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, pattern):
                    finalizer.audit_condition(
                        MemoryEvidence(contents), "classical", 5, 1
                    )

        expect_failure(
            "legacy schema",
            lambda manifest, _: manifest.__setitem__(
                "schema", "pqc-energy-trading.e9-condition-evidence.v3"
            ),
            "schema v4",
        )
        expect_failure(
            "wrong commit",
            lambda manifest, _: manifest["environment"]["software"].__setitem__(
                "git_commit", "0" * 40
            ),
            "Git provenance",
        )
        expect_failure(
            "dirty",
            lambda manifest, _: manifest["environment"]["software"].__setitem__(
                "git_dirty", True
            ),
            "Git provenance",
        )
        for label, key, value in (
            ("calibration", "per_channel_rtt_compensation_ms", 0.5),
            ("effective delay", "effective_one_way_tc_delay_ms", 2.25),
            ("logical RTT", "expected_end_to_end_rtt_ms", 10.0),
            ("message size", "prepared_message_bytes", 223),
        ):
            expect_failure(
                label,
                lambda manifest, _, key=key, value=value: manifest["condition"].__setitem__(
                    key, value
                ),
                "condition metadata",
            )
        expect_failure(
            "ping sample count",
            lambda manifest, _: manifest["ping_verification"].__setitem__(
                "sample_count", 19
            ),
            "ping sample provenance",
        )
        expect_failure(
            "ping hash",
            lambda manifest, _: manifest["ping_verification"].__setitem__(
                "sha256", "0" * 64
            ),
            "ping raw identity",
        )
        expect_failure(
            "completion hash",
            lambda manifest, _: manifest["completion_samples"].__setitem__(
                "sha256", "0" * 64
            ),
            "completion raw identity",
        )

        def exactly_ten_percent(manifest, contents):
            ping = _ping_bytes(5.5)
            contents[ping_name] = ping
            self._refresh_identity(manifest, "ping_verification", ping)
            manifest["ping_verification"].update(
                {
                    "samples_ms": [5.5] * 20,
                    "measured_end_to_end_rtt_ms": 5.5,
                    "deviation_pct": 10.0,
                    "pass": False,
                }
            )

        expect_failure("exactly 10 percent", exactly_ten_percent, "strict <10%")

        def mutate_csv(manifest, contents, mutation):
            text = contents[completion_name].decode().splitlines()
            rows = list(csv.DictReader(text))
            mutation(rows)
            output = io.StringIO(newline="")
            writer = csv.DictWriter(
                output, fieldnames=finalizer.RAW_FIELDS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
            content = output.getvalue().encode()
            contents[completion_name] = content
            self._refresh_identity(manifest, "completion_samples", content)
            manifest["completion_samples"]["row_count"] = len(rows)

        cases = (
            ("wrong rows", lambda rows: rows.pop(), "schema/count"),
            ("failed row", lambda rows: rows[100].__setitem__("success", "false"), "failed"),
            (
                "non-scientific row",
                lambda rows: rows[100].__setitem__("scientific", "false"),
                "failed",
            ),
            (
                "non-finite timing",
                lambda rows: rows[100].__setitem__("completion_ms", "nan"),
                "non-positive/non-finite",
            ),
            (
                "non-positive timing",
                lambda rows: rows[100].__setitem__("completion_ms", "0"),
                "non-positive/non-finite",
            ),
        )
        for label, mutation, pattern in cases:
            expect_failure(
                label,
                lambda manifest, contents, mutation=mutation: mutate_csv(
                    manifest, contents, mutation
                ),
                pattern,
            )

    def test_plain_and_frozen_inventory_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            shutil.copytree(self.source, missing)
            next(missing.glob("ping_*.txt")).unlink()
            with self.assertRaisesRegex(ValueError, "Cartesian product"):
                finalizer.EvidenceRoot(missing)

        with tempfile.TemporaryDirectory() as temporary:
            duplicate = Path(temporary) / "duplicate"
            shutil.copytree(self.source, duplicate)
            nested = duplicate / "nested"
            nested.mkdir()
            source = next(duplicate.glob("ping_*.txt"))
            shutil.copy2(source, nested / source.name)
            with self.assertRaisesRegex(ValueError, "duplicate logical"):
                finalizer.EvidenceRoot(duplicate)

        with tempfile.TemporaryDirectory() as temporary:
            frozen = Path(temporary) / "frozen"
            freezer.freeze(self.source, frozen)
            (frozen / "extra.gz").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "unmanifested"):
                finalizer.EvidenceRoot(frozen)

    def test_frozen_hashes_and_original_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            frozen = Path(temporary) / "frozen"
            freezer.freeze(self.source, frozen)
            tracked = next(frozen.glob("*.gz"))
            tracked.write_bytes(tracked.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(ValueError, "tracked gzip identity"):
                finalizer.EvidenceRoot(frozen)

        with tempfile.TemporaryDirectory() as temporary:
            frozen = Path(temporary) / "frozen"
            freezer.freeze(self.source, frozen)
            manifest_path = frozen / "evidence_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["entries"][0]["original_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            with self.assertRaisesRegex(ValueError, "original evidence identity"):
                finalizer.EvidenceRoot(frozen)

    def test_freezer_refuses_nonempty_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "raw-e9"
            destination.mkdir()
            (destination / "keep").write_text("do not overwrite")
            with self.assertRaisesRegex(FileExistsError, "non-empty"):
                freezer.freeze(self.source, destination)
            self.assertEqual((destination / "keep").read_text(), "do not overwrite")


if __name__ == "__main__":
    unittest.main()
