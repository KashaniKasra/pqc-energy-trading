import csv
import io
from pathlib import Path
import tempfile
import unittest

import config
import finalize_e7
import prepare_trace
from test_prepare_trace import GRID_EXPORT, GRID_IMPORT, PV, TIMESTAMP, upstream_bytes


def write_trace(root):
    source = root / prepare_trace.UPSTREAM_FILENAME
    trace = root / "opsd_household_15min_e7.csv"
    provenance = root / "provenance.json"
    source.write_bytes(upstream_bytes())
    prepare_trace.prepare_trace(
        source,
        trace,
        provenance,
        timestamp_column=TIMESTAMP,
        pv_column=PV,
        grid_import_column=GRID_IMPORT,
        grid_export_column=GRID_EXPORT,
    )
    return trace, provenance


def valid_evidence_rows():
    return [
        {
            "config": name,
            "run_id": "1",
            "sample_idx": str(sample_idx),
            "latency_ms": f"{1 + sample_idx / 1000:.6f}",
        }
        for name in config.CONFIG_NAMES
        for sample_idx in range(1000)
    ]


def write_evidence(path, rows, fields=finalize_e7.FINAL_FIELDS):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class FinalizeE7Tests(unittest.TestCase):
    def test_valid_finalization_schema_order_and_determinism(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace, provenance = write_trace(root)
            evidence = root / "evidence.csv"
            rows = list(reversed(valid_evidence_rows()))
            write_evidence(evidence, rows)
            first = root / "first.csv"
            second = root / "second.csv"
            finalize_e7.finalize(evidence, trace, provenance, first)
            finalize_e7.finalize(evidence, trace, provenance, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with first.open(newline="", encoding="utf-8") as source:
                output = list(csv.reader(source))
            self.assertEqual(tuple(output[0]), finalize_e7.FINAL_FIELDS)
            self.assertEqual(len(output), 4001)
            self.assertEqual(output[1][:3], ["l1_only", "1", "0"])
            self.assertEqual(output[1001][:3], ["sets_reimpl", "1", "0"])
            self.assertEqual(output[-1][:3], ["layer_aware_l2", "1", "999"])

    def test_unknown_and_misspelled_sets_config_rejected(self):
        for value in ("unknown", "sets-reimpl"):
            rows = valid_evidence_rows()
            rows[1000]["config"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "unknown"):
                finalize_e7.validate_rows(rows, 1100)

    def test_duplicate_sample_rejected(self):
        rows = valid_evidence_rows()
        rows.append(dict(rows[0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            finalize_e7.validate_rows(rows, 1100)

    def test_fewer_than_1000_samples_rejected(self):
        rows = [
            row
            for row in valid_evidence_rows()
            if not (row["config"] == "l1_only" and row["sample_idx"] == "999")
        ]
        with self.assertRaisesRegex(ValueError, "at least 1000"):
            finalize_e7.validate_rows(rows, 1100)

    def test_mismatched_sample_population_rejected(self):
        rows = valid_evidence_rows()
        for row in rows:
            if row["config"] == "sets_reimpl" and row["sample_idx"] == "999":
                row["sample_idx"] = "1000"
                break
        with self.assertRaisesRegex(ValueError, "populations differ"):
            finalize_e7.validate_rows(rows, 1101)

    def test_mismatched_run_id_population_rejected(self):
        rows = valid_evidence_rows()
        for row in rows:
            if row["config"] == "sets_reimpl":
                row["run_id"] = "2"
        with self.assertRaisesRegex(ValueError, r"run_id, sample_idx.*differ"):
            finalize_e7.validate_rows(rows, 1100)

    def test_nonpositive_or_nonfinite_latency_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            rows = valid_evidence_rows()
            rows[0]["latency_ms"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite and positive"):
                finalize_e7.validate_rows(rows, 1100)

    def test_missing_evidence_run_id_or_latency_rejected_cleanly(self):
        for field in ("run_id", "latency_ms"):
            rows = valid_evidence_rows()
            rows[0][field] = None
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "malformed E7 evidence row"
            ):
                finalize_e7.validate_rows(rows, 1100)

    def test_duplicate_evidence_header_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.csv"
            path.write_text(
                "config,run_id,run_id,sample_idx,latency_ms\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate CSV header"):
                finalize_e7.load_evidence(path)

    def test_wrong_final_evidence_schema_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.csv"
            write_evidence(path, [], fields=("wrong",))
            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                finalize_e7.load_evidence(path)

    def test_trace_provenance_and_spacing_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace, provenance = write_trace(root)
            self.assertEqual(finalize_e7.validate_trace(trace, provenance), 1100)
            lines = trace.read_text().splitlines()
            fields = lines[501].split(",")
            fields[0] = "2020-01-06T05:01:00+00:00"
            lines[501] = ",".join(fields)
            trace.write_text("\n".join(lines) + "\n")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                finalize_e7.validate_trace(trace, provenance)


if __name__ == "__main__":
    unittest.main()
