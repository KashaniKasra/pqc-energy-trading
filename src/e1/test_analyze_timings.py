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
    def write_fixture(self, root: Path, ending_latency: float) -> None:
        raw = root / "raw" / "e1"
        raw.mkdir(parents=True)
        (raw / "test_sphincs_caliper_run.log").write_text(
            "| sphincs-1-tps | 60 | 0 | 1.0 | 10.0 | 1.0 | 10.0 | 1.0 |\n",
            encoding="utf-8",
        )
        timing_path = raw / "test_sphincs_e2e_sphincs-1-tps_worker0_1.csv"
        with timing_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=ANALYZER.E2E_FIELDS)
            writer.writeheader()
            for index in range(60):
                latency = ending_latency if index >= 48 else 10.0
                writer.writerow({
                    "start_offset_ms": index * 1000,
                    "latency_ms": latency,
                    "status": "success",
                    "tx_id": f"tx-{index}",
                })

    def test_all_three_criteria_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, ending_latency=20.0)
            rows = ANALYZER.build_sustainability_rows(root, "test_sphincs")
            self.assertEqual(rows[0]["sustainable"], "true")
            self.assertEqual(rows[0]["highest_tested_sustainable_tps"], "1")

    def test_end_p95_over_twice_beginning_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_fixture(root, ending_latency=20.001)
            rows = ANALYZER.build_sustainability_rows(root, "test_sphincs")
            self.assertEqual(rows[0]["latency_stability_pass"], "false")
            self.assertEqual(rows[0]["sustainable"], "false")


class TransactionEvidenceTests(unittest.TestCase):
    def test_exact_envelope_summary_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "e1"
            raw.mkdir(parents=True)
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
