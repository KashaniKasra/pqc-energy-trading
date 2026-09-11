import csv
import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("analyze_timings.py")
SPEC = importlib.util.spec_from_file_location("analyze_timings", MODULE_PATH)
ANALYZER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ANALYZER)


class SustainabilityTests(unittest.TestCase):
    def successful_rows(
        self, count: int = 60, beginning_latency: float = 10.0,
        ending_latency: float = 20.0,
    ) -> list[dict[str, str | float]]:
        rows = []
        for index in range(count):
            offset = index * 59000 / (count - 1) if count > 1 else 0
            latency = ending_latency if offset >= 48000 else beginning_latency
            rows.append({
                "start_offset_ms": offset,
                "latency_ms": latency,
                "status": "success",
                "tx_id": f"success-{index}",
            })
        return rows

    def write_fixture(
        self,
        root: Path,
        rows: list[dict[str, str | float]],
        success: int | None = None,
        fail: int | None = None,
        run_namespace: str = "test_sphincs",
        round_label: str = "sphincs-1-tps",
        offered_rate: int = 1,
        caliper_send_rate: float | None = None,
    ) -> None:
        raw = root / "raw" / "e1"
        raw.mkdir(parents=True)
        if success is None:
            success = sum(row["status"] == "success" for row in rows)
        if fail is None:
            fail = sum(row["status"] == "failure" for row in rows)
        if caliper_send_rate is None:
            caliper_send_rate = float(offered_rate)
        (raw / f"{run_namespace}_caliper_run.log").write_text(
            f"| {round_label} | {success} | {fail} | {caliper_send_rate:.1f} | 10.0 | 1.0 | 10.0 | {caliper_send_rate:.1f} |\n",
            encoding="utf-8",
        )
        timing_path = raw / f"{run_namespace}_e2e_{round_label}_worker0_1.csv"
        with timing_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=ANALYZER.E2E_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def analyze(
        self, root: Path, run_namespace: str = "test_sphincs"
    ) -> dict[str, str]:
        return ANALYZER.build_sustainability_rows(
            root, run_namespace, validate_all_raw=False
        )[0]

    def test_all_three_criteria_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, self.successful_rows())
            row = self.analyze(root)
            self.assertEqual(row["sustainable"], "true")
            self.assertEqual(row["highest_tested_sustainable_tps"], "1")
            self.assertEqual(row["total"], "60")
            self.assertEqual(row["achieved_offered_ratio"], "1.000000")
            self.assertEqual(row["overall_successful_e2e_median_ms"], "10.000000")
            self.assertEqual(row["ending_to_beginning_p95_ratio"], "2.000000")
            self.assertEqual(row["latency_stability_pass"], "true")

    def test_end_p95_over_twice_beginning_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, self.successful_rows(ending_latency=20.001))
            row = self.analyze(root)
            self.assertEqual(row["latency_stability_pass"], "false")
            self.assertEqual(row["sustainable"], "false")

    def test_generalized_ecdsa_single_rate_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.successful_rows(13_320)
            self.write_fixture(
                root,
                rows,
                run_namespace="test_ecdsa",
                round_label="sustained-222-tps",
                offered_rate=222,
            )
            result = self.analyze(root, "test_ecdsa")
            self.assertEqual(result["config"], "ECDSA")
            self.assertEqual(result["run_namespace"], "test_ecdsa")
            self.assertEqual(result["offered_tps"], "222")
            self.assertEqual(result["total_count"], "13320")
            self.assertEqual(result["successful_throughput_tps"], "222.000000")
            self.assertEqual(result["successful_throughput_ratio"], "1.000000")
            self.assertEqual(result["begin_window_ms"], "[0,12000)")
            self.assertEqual(result["end_window_ms"], "[48000,60000)")
            self.assertEqual(result["sustainable"], "true")

    def test_configured_rate_not_rounded_caliper_send_rate_defines_offered_tps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.successful_rows(17_995)
            self.write_fixture(
                root,
                rows,
                run_namespace="test_ml-dsa-44",
                round_label="sustained-300-tps",
                offered_rate=300,
                caliper_send_rate=299.9,
            )
            result = self.analyze(root, "test_ml-dsa-44")
            self.assertEqual(result["offered_tps"], "300")
            self.assertEqual(result["success_count"], "17995")
            self.assertEqual(result["successful_throughput_tps"], "299.916667")
            self.assertEqual(result["successful_throughput_ratio"], "0.999722")
            self.assertEqual(result["throughput_gate_pass"], "true")

    def test_configured_benchmark_rate_must_match_round_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            benchmark = Path(directory) / "benchmark.yaml"
            benchmark.write_text(
                "test:\n"
                "  rounds:\n"
                "    - label: sustained-300-tps\n"
                "      txDuration: 60\n"
                "      rateControl:\n"
                "        type: fixed-rate\n"
                "        opts:\n"
                "          tps: 299\n"
                "      workload:\n"
                "        arguments:\n"
                "          roundLabel: sustained-300-tps\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "configured fixed-rate TPS"):
                ANALYZER.configured_sustainability_rate(
                    benchmark, "sustained-300-tps"
                )

            benchmark.write_text(
                benchmark.read_text(encoding="utf-8")
                .replace("tps: 299", "tps: 300")
                .replace(
                    "roundLabel: sustained-300-tps",
                    "roundLabel: sustained-299-tps",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "roundLabel"):
                ANALYZER.configured_sustainability_rate(
                    benchmark, "sustained-300-tps"
                )

    def test_ecdsa_223_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-223-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_223.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_224_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-224-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_224.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_227_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-227-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_227.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_228_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-228-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_228.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_229_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-229-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_229.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_adjacent_integer_boundary_requires_pass_then_fail(self) -> None:
        passing = {
            "config": "ECDSA",
            "offered_tps": "228",
            "sustainable": "true",
            "highest_tested_sustainable_tps": "228",
        }
        failing = {
            "config": "ECDSA",
            "offered_tps": "229",
            "sustainable": "false",
            "highest_tested_sustainable_tps": "",
        }
        self.assertEqual(
            ANALYZER.validated_adjacent_integer_boundary(passing, failing), "228"
        )
        with self.assertRaisesRegex(ValueError, "not adjacent"):
            ANALYZER.validated_adjacent_integer_boundary(
                passing, {**failing, "offered_tps": "230"}
            )
        with self.assertRaisesRegex(ValueError, "pass/fail ordering"):
            ANALYZER.validated_adjacent_integer_boundary(
                passing, {**failing, "sustainable": "true"}
            )

    def test_ecdsa_230_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-230-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_230.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_237_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-237-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_237.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ecdsa_250_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ecdsa", ("sustained-250-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ecdsa_sustained_250.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_250_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ml-dsa-44", ("sustained-250-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ml_dsa_sustained_250.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_300_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ml-dsa-44", ("sustained-300-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ml_dsa_sustained_300.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_350_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ml-dsa-44", ("sustained-350-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ml_dsa_sustained_350.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_prepared_midpoint_profiles_are_registered_immutably(self) -> None:
        for tps in (356, 357, 358, 359, 360, 361, 362, 368, 381, 387, 393):
            with self.subTest(tps=tps):
                spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
                    ("ml-dsa-44", (f"sustained-{tps}-tps",))
                ]
                self.assertEqual(
                    spec["path"],
                    f"env/caliper/e1/benchmark_ml_dsa_sustained_{tps}.yaml",
                )
                self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_375_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ml-dsa-44", ("sustained-375-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ml_dsa_sustained_375.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_ml_dsa_44_400_profile_is_registered_immutably(self) -> None:
        spec = ANALYZER.SUSTAINABILITY_PROFILE_SPECS[
            ("ml-dsa-44", ("sustained-400-tps",))
        ]
        self.assertEqual(
            spec["path"],
            "env/caliper/e1/benchmark_ml_dsa_sustained_400.yaml",
        )
        self.assertEqual(spec["run_type"], "sustainability")

    def test_success_rate_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.successful_rows(99)
            rows.append({
                "start_offset_ms": 30000,
                "latency_ms": 9999,
                "status": "failure",
                "tx_id": "failure-0",
            })
            self.write_fixture(root, rows)
            self.assertEqual(self.analyze(root)["success_rate_pass"], "true")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.successful_rows(98)
            for index in range(2):
                rows.append({
                    "start_offset_ms": 30000 + index,
                    "latency_ms": 9999,
                    "status": "failure",
                    "tx_id": f"failure-{index}",
                })
            self.write_fixture(root, rows)
            self.assertEqual(self.analyze(root)["success_rate_pass"], "false")

    def test_successful_throughput_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, self.successful_rows(57))
            self.assertEqual(self.analyze(root)["throughput_pass"], "true")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, self.successful_rows(56))
            self.assertEqual(self.analyze(root)["throughput_pass"], "false")

    def test_window_boundaries_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offsets = [0, 1000, 2000, 3000, 11999, 12000, 30000,
                       48000, 50000, 55000, 59000, 59999, 60000]
            rows = [{
                "start_offset_ms": offset,
                "latency_ms": 10,
                "status": "success",
                "tx_id": f"tx-{index}",
            } for index, offset in enumerate(offsets)]
            self.write_fixture(root, rows)
            result = self.analyze(root)
            self.assertEqual(result["beginning_success_n"], "5")
            self.assertEqual(result["ending_success_n"], "5")

    def test_each_window_requires_five_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offsets = [0, 1000, 2000, 11999, 12000, 30000,
                       48000, 50000, 55000, 59000, 59999]
            rows = [{
                "start_offset_ms": offset,
                "latency_ms": 10,
                "status": "success",
                "tx_id": f"tx-{index}",
            } for index, offset in enumerate(offsets)]
            self.write_fixture(root, rows)
            with self.assertRaisesRegex(ValueError, "at least five successful"):
                self.analyze(root)

    def test_percentile_interpolates_rank_p_times_n_minus_one(self) -> None:
        self.assertEqual(ANALYZER.percentile([0.0, 10.0], 0.95), 9.5)

    def test_failures_count_but_do_not_enter_latency_population(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = self.successful_rows(60, ending_latency=10)
            for index in range(10):
                rows.append({
                    "start_offset_ms": 1000 if index < 5 else 50000,
                    "latency_ms": 999999,
                    "status": "failure",
                    "tx_id": f"failure-{index}",
                })
            self.write_fixture(root, rows)
            result = self.analyze(root)
            self.assertEqual(result["total"], "70")
            self.assertEqual(result["caliper_fail"], "10")
            self.assertEqual(result["overall_successful_e2e_p99_ms"], "10.000000")

    def test_source_count_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, self.successful_rows(59), success=60, fail=0)
            with self.assertRaisesRegex(ValueError, "does not reconcile"):
                self.analyze(root)


class TransactionEvidenceTests(unittest.TestCase):
    def test_exact_envelope_summary_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "e1"
            raw.mkdir(parents=True)
            blocks = raw / "test_blocks.csv"
            with blocks.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(destination, fieldnames=[
                    "config", "run_label", "benchmark_label", "block_number",
                    "block_bytes", "transaction_count", "header_types",
                    "classification", "accepted_for_mean",
                ])
                writer.writeheader()
                writer.writerow({
                    "config": "ecdsa",
                    "run_label": "test",
                    "benchmark_label": "test-rate",
                    "block_number": "1",
                    "block_bytes": "100",
                    "transaction_count": "1",
                    "header_types": "3",
                    "classification": "ordinary_transaction",
                    "accepted_for_mean": "true",
                })
            transactions = raw / "test_transactions.csv"
            with transactions.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(
                    destination, fieldnames=ANALYZER.TRANSACTION_EVIDENCE_FIELDS
                )
                writer.writeheader()
                writer.writerow({
                    "config": "ecdsa",
                    "run_label": "test",
                    "benchmark_label": "test-rate",
                    "block_number": "1",
                    "tx_index": "0",
                    "channel_header_type": "3",
                    "tx_id": "tx-0",
                    "envelope_bytes": "100",
                    "envelope_sha256": "0" * 64,
                    "validation_code": "0",
                    "validation_name": "VALID",
                    "endorsements": "2",
                    "block_classification": "ordinary_transaction",
                    "accepted_for_tx_mean": "true",
                })
            summary = raw / "test_summary.csv"
            fields = [
                "config", "run_label", "benchmark_label",
                "start_block", "end_block", "ordinary_block_count",
                "excluded_block_count", "block_bytes_mean",
                "transactions_per_block_mean", "effective_preferred_max_bytes",
                "block_utilisation", "raw_blocks_file", "raw_blocks_sha256",
                "ordinary_transaction_count", "valid_transaction_count",
                "invalid_transaction_count", "tx_bytes_mean", "endorsements_per_tx",
                "endorsements_min", "endorsements_max", "raw_transactions_file",
                "raw_transactions_sha256",
            ]
            with summary.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(destination, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "config": "ecdsa",
                    "run_label": "test",
                    "benchmark_label": "test-rate",
                    "start_block": "1",
                    "end_block": "1",
                    "ordinary_block_count": "1",
                    "excluded_block_count": "0",
                    "block_bytes_mean": "100.000000",
                    "transactions_per_block_mean": "1.000000",
                    "effective_preferred_max_bytes": "200",
                    "block_utilisation": "0.500000000",
                    "raw_blocks_file": "raw/e1/test_blocks.csv",
                    "raw_blocks_sha256": ANALYZER.sha256_file(blocks),
                    "ordinary_transaction_count": "1",
                    "valid_transaction_count": "1",
                    "invalid_transaction_count": "0",
                    "tx_bytes_mean": "100.000000",
                    "endorsements_per_tx": "2.000000",
                    "endorsements_min": "2",
                    "endorsements_max": "2",
                    "raw_transactions_file": "raw/e1/test_transactions.csv",
                    "raw_transactions_sha256": ANALYZER.sha256_file(transactions),
                })

            rows = ANALYZER.validate_transaction_summary(root, str(summary))
            self.assertEqual(rows[0]["tx_bytes_mean"], "100.000000")
            self.assertEqual(rows[0]["endorsements_per_tx"], "2.000000")


class CleanFixedProfileTests(unittest.TestCase):
    def write_fixture(self, root: Path, config: str = "ecdsa", fail_200: int = 0) -> str:
        raw = root / "raw" / "e1"
        benchmark = root / "env" / "caliper" / "e1" / "benchmark.yaml"
        raw.mkdir(parents=True)
        benchmark.parent.mkdir(parents=True)
        benchmark.write_text("test:\n  name: fixture\n", encoding="utf-8")
        scientific_sources = {
            "configtx_sha256": root / "env/fabric/e1/configtx.yaml",
            "fabric_setup_sha256": root / "env/fabric/e1/setup_fabric_e1.sh",
            "chaincode_sha256": root / "src/e1/chaincode/chaincode.go",
            "caliper_workload_sha256": root / "env/caliper/e1/workload/set.js",
            "caliper_timing_patch_sha256": (
                root / "env/caliper/e1/patches/peer-gateway-e1-timing.patch"
            ),
        }
        for path in scientific_sources.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture: {path.name}\n", encoding="utf-8")
        namespace = f"clean-v1_{config}"
        meta = {
            "software_pins": {
                "hyperledger_fabric": {
                    "commit": "f871cf92a026aba7b12e6f06d71ded3e6e659d71"
                },
                "liboqs": {
                    "commit": "97f6b86b1b6d109cfd43cf276ae39c2e776aed80"
                },
            },
            "e1_benchmark": {
                "installed_images_at_metadata_update": {
                    "peer": "sha256:peer",
                    "orderer": "sha256:orderer",
                    "fabric_pq_patch_sha256": "a" * 64,
                },
                "fabric_block_parameters": {
                    "BatchTimeout": "2s",
                    "MaxMessageCount": 500,
                    "PreferredMaxBytes_effective_bytes": 2097152,
                    "AbsoluteMaxBytes_effective_bytes": 10485760,
                },
            },
        }
        (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        required = [
            f"[E1] configuration={config}",
            "[E1] run_type=fixed-profile",
            f"[E1] run_namespace={namespace}",
            "[E1] benchmark_config=env/caliper/e1/benchmark.yaml",
            f"[E1] benchmark_config_sha256={ANALYZER.sha256_file(benchmark)}",
            "[E1] project_tracked_state_before_log_creation=clean",
            "[E1] project_git_commit=" + "0" * 40,
            "[E1] started_at=2026-01-01T00:00:00+00:00",
            "[E1] fixed_cpu_set=2-15",
            "[E1] amd_pstate_mode=passive",
            "[E1] boost_state=0",
            "[E1] fabric_source_tag=v2.5.16",
            "[E1] fabric_source_commit=f871cf92a026aba7b12e6f06d71ded3e6e659d71",
            "[E1] fabric_source_state=clean",
            "[E1] fabric_peer_image_id=sha256:peer",
            "[E1] fabric_orderer_image_id=sha256:orderer",
            "[E1] fabric_pq_patch_sha256=" + "a" * 64,
            "[E1] fabric_image_label_fabric_commit=f871cf92a026aba7b12e6f06d71ded3e6e659d71",
            "[E1] fabric_image_label_liboqs_commit=97f6b86b1b6d109cfd43cf276ae39c2e776aed80",
            "[E1] fabric_image_label_patch_sha256=" + "a" * 64,
            "[E1] pq_verify_trace=0",
            "[E1] Removing previous E1 containers and generated artifacts...",
            "[E1] Previous generated state removed.",
            f"[E1] Fabric network ready for configuration: {config}",
            "[E1] effective_BatchTimeout=2s",
            "[E1] effective_MaxMessageCount=500",
            "[E1] effective_PreferredMaxBytes=2097152",
            "[E1] effective_AbsoluteMaxBytes=10485760",
            "[E1] Benchmark finished.",
            "[E1] finished_at=2026-01-01T00:05:00+00:00",
        ]
        required.extend(
            f"[E1] {key}={ANALYZER.sha256_file(path)}"
            for key, path in scientific_sources.items()
        )
        required.extend(
            f"[E1] cpu{cpu}_state=governor:performance,min_khz:3201000,max_khz:3201000"
            for cpu in range(2, 16)
        )
        for label, success, fail in (
            ("50-tps", 1000, 0),
            ("200-tps", 1000 - fail_200, fail_200),
        ):
            required.append(
                f"| {label} | {success} | {fail} | {label[:-4]}.0 | 1.0 | 0.1 | 0.5 | {label[:-4]}.0 |"
            )
            for sample_type in ("endorse", "commit"):
                with (raw / f"{namespace}_{sample_type}_{label}_worker0_1.csv").open(
                    "w", encoding="utf-8"
                ) as destination:
                    destination.write("latency_ms\n")
                    destination.writelines("1.0\n" for _ in range(success))
            with (raw / f"{namespace}_e2e_{label}_worker0_1.csv").open(
                "w", encoding="utf-8"
            ) as destination:
                destination.write("start_offset_ms,latency_ms,status,tx_id\n")
                for index in range(success):
                    destination.write(f"{index},2.0,success,{label}-ok-{index}\n")
                for index in range(fail):
                    destination.write(f"{index},3.0,failure,{label}-fail-{index}\n")
        (raw / f"{namespace}_caliper_run.log").write_text(
            "\n".join(required) + "\n", encoding="utf-8"
        )
        (raw / f"{namespace}_block_heights.csv").write_text(
            "marker,height\n"
            "before_warmup,7\nafter_warmup,8\n"
            "before_50-tps,8\nafter_50-tps,9\n"
            "before_200-tps,9\nafter_200-tps,10\n",
            encoding="utf-8",
        )
        return namespace

    def test_clean_common_profile_keeps_rates_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = self.write_fixture(root)
            rows = ANALYZER.build_clean_fixed_profile_rows(root, namespace)
            self.assertEqual([row["round_label"] for row in rows], ["50-tps", "200-tps"])
            self.assertTrue(all(row["status"] == "clean_equivalent_latency_candidate" for row in rows))

    def test_failed_common_population_is_not_promoted_to_latency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = self.write_fixture(root, fail_200=1)
            rows = ANALYZER.build_clean_fixed_profile_rows(root, namespace)
            self.assertEqual(rows[1]["status"], "not_a_valid_normal_latency_candidate")
            self.assertEqual(rows[1]["commit_median_ms"], "")

    def test_clean_common_profile_requires_height_seven_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = self.write_fixture(root)
            heights = root / "raw" / "e1" / f"{namespace}_block_heights.csv"
            heights.write_text(heights.read_text().replace("before_warmup,7", "before_warmup,8"))
            with self.assertRaisesRegex(ValueError, "ledger height 7"):
                ANALYZER.build_clean_fixed_profile_rows(root, namespace)

    def test_clean_common_profile_reconciles_scientific_source_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = self.write_fixture(root)
            log = root / "raw" / "e1" / f"{namespace}_caliper_run.log"
            text = log.read_text(encoding="utf-8")
            text = text.replace("[E1] configtx_sha256=", "[E1] stale_configtx_sha256=")
            log.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configtx_sha256"):
                ANALYZER.build_clean_fixed_profile_rows(root, namespace)


class PQVerificationAuditTests(unittest.TestCase):
    def write_fixture(self, root: Path) -> None:
        raw = root / "raw" / "e1"
        raw.mkdir(parents=True)
        fabric_commit = "f" * 40
        liboqs_commit = "9" * 40
        patch_hash = "a" * 64
        peer_image = "sha256:" + "b" * 64
        orderer_image = "sha256:" + "c" * 64
        metadata = {
            "software_pins": {
                "hyperledger_fabric": {"commit": fabric_commit},
                "liboqs": {"commit": liboqs_commit},
            },
            "e1_benchmark": {"installed_images_at_metadata_update": {
                "peer": peer_image,
                "orderer": orderer_image,
                "fabric_pq_patch_sha256": patch_hash,
            }},
        }
        (root / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        containers = [
            "orderer.example.com",
            "peer0.org1.example.com", "peer1.org1.example.com",
            "peer0.org2.example.com", "peer1.org2.example.com",
        ]
        for config, spec in ANALYZER.PQ_VERIFICATION_AUDITS.items():
            namespace = spec["run_namespace"]
            evidence = raw / f"{namespace}_pq_verification_audit.csv"
            traces = []
            rows = []
            for container in containers:
                trace = (
                    "timestamp E1_PQ_VERIFY_TRACE implementation=liboqs "
                    f"function=oqs.Signature.Verify algorithm={spec['algorithm']} result=success"
                )
                traces.append(f"{container}\t{trace}")
                rows.append({
                    "config": config, "algorithm": spec["algorithm"],
                    "container": container, "implementation": "liboqs",
                    "function": "oqs.Signature.Verify", "result": "success",
                    "trace_line_sha256": hashlib.sha256(trace.encode()).hexdigest(),
                })
            with evidence.open("w", newline="", encoding="utf-8") as destination:
                writer = csv.DictWriter(destination, fieldnames=ANALYZER.PQ_VERIFICATION_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            log_lines = [
                f"[E1-PQ-VERIFY] run_namespace={namespace}",
                f"[E1-PQ-VERIFY] configuration={config}",
                f"[E1-PQ-VERIFY] algorithm={spec['algorithm']}",
                "[E1-PQ-VERIFY] purpose=functional_verification_path_audit_not_performance_measurement",
                "[E1-PQ-VERIFY] controlled_cpu_or_ac_required=false",
                "[E1-PQ-VERIFY] project_git_commit=" + "d" * 40,
                "[E1-PQ-VERIFY] project_tracked_state_before_log_creation=clean",
                "[E1-PQ-VERIFY] project_tracked_status_sha256_before_log_creation=" + hashlib.sha256(b"").hexdigest(),
                f"[E1-PQ-VERIFY] fabric_source_commit={fabric_commit}",
                f"[E1-PQ-VERIFY] liboqs_commit={liboqs_commit}",
                f"[E1-PQ-VERIFY] fabric_pq_patch_sha256={patch_hash}",
                f"[E1-PQ-VERIFY] peer_image_id={peer_image}",
                f"[E1-PQ-VERIFY] orderer_image_id={orderer_image}",
                "[E1-PQ-VERIFY] negative_regression_test=TestPQVerifierDispatchRejectsClassicalFallback",
                "[E1] Removing previous E1 containers and generated artifacts...",
                "[E1] Previous generated state removed.",
                "[E1] Chaincode smoke test passed.",
                "[E1] Fabric E1 setup completed successfully.",
                *traces,
                f"[E1-PQ-VERIFY] evidence_csv_sha256={ANALYZER.sha256_file(evidence)}",
            ]
            (raw / f"{namespace}_pq_verification_audit.log").write_text(
                "\n".join(log_lines) + "\n", encoding="utf-8"
            )

    def test_all_algorithms_and_organizations_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root)
            rows = ANALYZER.validate_pq_verification_audits(root)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["trace_count"] == "5" for row in rows))
            self.assertTrue(all(row["org1_peer_success_count"] == "2" for row in rows))
            self.assertTrue(all(row["org2_peer_success_count"] == "2" for row in rows))

    def test_trace_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root)
            path = root / "raw/e1/pq-verify-v1_ml-dsa-44_pq_verification_audit.csv"
            path.write_text(path.read_text().replace("success,", "success,0", 1))
            with self.assertRaisesRegex(ValueError, "invalid or unreconciled trace row"):
                ANALYZER.validate_pq_verification_audits(root)


if __name__ == "__main__":
    unittest.main()
