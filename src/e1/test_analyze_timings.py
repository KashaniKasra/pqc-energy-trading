import csv
import importlib.util
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
    ) -> None:
        raw = root / "raw" / "e1"
        raw.mkdir(parents=True)
        if success is None:
            success = sum(row["status"] == "success" for row in rows)
        if fail is None:
            fail = sum(row["status"] == "failure" for row in rows)
        (raw / f"{run_namespace}_caliper_run.log").write_text(
            f"| {round_label} | {success} | {fail} | {offered_rate:.1f} | 10.0 | 1.0 | 10.0 | {offered_rate:.1f} |\n",
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


if __name__ == "__main__":
    unittest.main()
