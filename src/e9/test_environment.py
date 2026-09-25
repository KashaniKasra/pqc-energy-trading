import argparse
import importlib.util
from pathlib import Path
import tempfile
import unittest

from src.e9.environment import collect_timing_environment, validate_scientific_environment


class FakeTimingFilesystem:
    def __init__(self, root: Path):
        self.root = root
        self.sys_root = root / "sys"
        self.proc_cpuinfo = root / "proc/cpuinfo"
        self._write(self.proc_cpuinfo, "model name\t: Test Scientific CPU\n")
        self._write(self.sys_root / "devices/system/cpu/online", "0-3\n")
        self._write(self.sys_root / "devices/system/cpu/cpufreq/boost", "0\n")
        for policy in ("policy0", "policy1"):
            base = self.sys_root / "devices/system/cpu/cpufreq" / policy
            self._write(base / "scaling_driver", "test-driver\n")
            self._write(base / "scaling_governor", "performance\n")
            self._write(base / "energy_performance_preference", "performance\n")
            self._write(base / "scaling_min_freq", "3000000\n")
            self._write(base / "scaling_max_freq", "3000000\n")
        ac = self.sys_root / "class/power_supply/AC0"
        self._write(ac / "type", "Mains\n")
        self._write(ac / "online", "1\n")

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def collect(self, affinity=(2, 4, 6)):
        return collect_timing_environment(
            sys_root=self.sys_root,
            proc_cpuinfo=self.proc_cpuinfo,
            affinity_getter=lambda _pid: affinity,
        )


class TimingEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = FakeTimingFilesystem(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_collects_cpu_power_and_affinity_provenance(self):
        provenance = self.fixture.collect()
        cpu = provenance["cpu"]
        self.assertEqual(cpu["model"], {"status": "available", "value": "Test Scientific CPU"})
        self.assertEqual(cpu["online"]["cpus"], [0, 1, 2, 3])
        self.assertEqual(cpu["online"]["count"], 4)
        self.assertEqual(cpu["policy_names"], ["policy0", "policy1"])
        self.assertEqual(
            cpu["scaling_driver"]["values_by_policy"],
            {"policy0": "test-driver", "policy1": "test-driver"},
        )
        self.assertEqual(cpu["process_affinity"]["cpus"], [2, 4, 6])
        self.assertFalse(cpu["boost"]["enabled"])
        self.assertTrue(provenance["power"]["ac_online"]["online"])

    def test_consistent_pinned_environment_accepted(self):
        validate_scientific_environment(self.fixture.collect())

    def test_variable_min_max_rejected(self):
        provenance = self.fixture.collect()
        provenance["cpu"]["scaling_max_freq_khz"]["values_by_policy"]["policy1"] = "3100000"
        with self.assertRaisesRegex(ValueError, "scaling_min_freq == scaling_max_freq"):
            validate_scientific_environment(provenance)

    def test_inconsistent_pinned_frequency_rejected(self):
        provenance = self.fixture.collect()
        provenance["cpu"]["scaling_min_freq_khz"]["values_by_policy"]["policy1"] = "3100000"
        provenance["cpu"]["scaling_max_freq_khz"]["values_by_policy"]["policy1"] = "3100000"
        with self.assertRaisesRegex(ValueError, "inconsistent across CPU policies"):
            validate_scientific_environment(provenance)

    def test_boost_enabled_rejected(self):
        provenance = self.fixture.collect()
        provenance["cpu"]["boost"].update(value="1", enabled=True)
        with self.assertRaisesRegex(ValueError, "boost disabled"):
            validate_scientific_environment(provenance)

    def test_nonperformance_governor_rejected(self):
        provenance = self.fixture.collect()
        provenance["cpu"]["governor"]["values_by_policy"]["policy0"] = "powersave"
        with self.assertRaisesRegex(ValueError, "performance governor"):
            validate_scientific_environment(provenance)

    def test_nonperformance_epp_rejected_when_available(self):
        provenance = self.fixture.collect()
        provenance["cpu"]["energy_performance_preference"]["values_by_policy"][
            "policy0"
        ] = "balance_performance"
        with self.assertRaisesRegex(ValueError, "performance EPP"):
            validate_scientific_environment(provenance)

    def test_empty_affinity_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty process CPU affinity"):
            validate_scientific_environment(self.fixture.collect(affinity=()))

    def test_ac_offline_rejected_when_available(self):
        provenance = self.fixture.collect()
        provenance["power"]["ac_online"].update(online=False, sources={"AC0": 0})
        with self.assertRaisesRegex(ValueError, "AC online"):
            validate_scientific_environment(provenance)

    def test_optional_epp_and_ac_unavailable_are_explicit_and_accepted(self):
        for policy in ("policy0", "policy1"):
            (
                self.fixture.sys_root
                / "devices/system/cpu/cpufreq"
                / policy
                / "energy_performance_preference"
            ).unlink()
        ac_root = self.fixture.sys_root / "class/power_supply/AC0"
        (ac_root / "online").unlink()
        (ac_root / "type").unlink()
        provenance = self.fixture.collect()
        self.assertEqual(
            provenance["cpu"]["energy_performance_preference"]["status"], "unavailable"
        )
        self.assertEqual(provenance["power"]["ac_online"]["status"], "unavailable")
        validate_scientific_environment(provenance)

    def test_preflight_validation_does_not_require_scientific_cpu_policy(self):
        runner_path = Path(__file__).parents[2] / "env/mininet/e9/run_e9.py"
        specification = importlib.util.spec_from_file_location("e9_runner_environment_test", runner_path)
        runner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(runner)
        args = argparse.Namespace(
            config="classical",
            rtt_ms=20,
            hops=3,
            warmup=1,
            iterations=3,
            output_root="unused",
            scientific=False,
            preflight=True,
        )
        runner.validate_cli_mode(args)


if __name__ == "__main__":
    unittest.main()
