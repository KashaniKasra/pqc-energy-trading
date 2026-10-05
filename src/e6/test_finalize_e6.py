import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("finalize_e6.py")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUTHORITATIVE_E2 = REPOSITORY_ROOT / "data/e2_statemachine.csv"
SPEC = importlib.util.spec_from_file_location("finalize_e6", MODULE_PATH)
finalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalizer)


def fixture_bytes(rows, fields=finalizer.E2_FIELDS):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    return output.getvalue().encode()


VALID_ROWS = [
    ("classical", "funding", "222", "1.0", "2.0"),
    ("uniform_mldsa", "funding", "10648", "3.0", "4.0"),
]


class FinalizeE6Tests(unittest.TestCase):
    def write_fixture(self, root, rows=VALID_ROWS, fields=finalizer.E2_FIELDS):
        content = fixture_bytes(rows, fields)
        path = root / "e2.csv"
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def finalize_authoritative(self, root):
        output = root / "e6.csv"
        finalizer.finalize(AUTHORITATIVE_E2, output)
        return output

    def test_valid_input_produces_exact_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.finalize_authoritative(Path(temporary))
            self.assertEqual(
                output.read_bytes(),
                b"scheme,aggregated,k_sig,k_pk,tx_bytes\n"
                b"classical,true,1,1,222\n"
                b"ML-DSA-65,false,2,2,10648\n",
            )

    def test_exact_schema_order_and_row_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = self.finalize_authoritative(Path(temporary))
            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.reader(source))
            self.assertEqual(tuple(rows[0]), finalizer.E6_FIELDS)
            self.assertEqual(rows[1], ["classical", "true", "1", "1", "222"])
            self.assertEqual(rows[2], ["ML-DSA-65", "false", "2", "2", "10648"])

    def test_wrong_e2_sha_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            e2_path, _ = self.write_fixture(root)
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                finalizer.extract_funding_sizes(e2_path)

    def test_wrong_e2_schema_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = fixture_bytes(VALID_ROWS, fields=("wrong",))
            e2_path = root / "e2.csv"
            e2_path.write_bytes(content)
            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                finalizer._extract_funding_sizes(
                    e2_path, expected_sha256=hashlib.sha256(content).hexdigest()
                )

    def test_missing_classical_funding_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            e2_path, digest = self.write_fixture(root, VALID_ROWS[1:])
            with self.assertRaisesRegex(ValueError, "classical funding row; found 0"):
                finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_missing_mldsa_funding_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            e2_path, digest = self.write_fixture(root, VALID_ROWS[:1])
            with self.assertRaisesRegex(ValueError, "uniform_mldsa funding row; found 0"):
                finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_duplicate_matching_funding_row_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [*VALID_ROWS, VALID_ROWS[0]]
            e2_path, digest = self.write_fixture(root, rows)
            with self.assertRaisesRegex(ValueError, "classical funding row; found 2"):
                finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_wrong_classical_size_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [("classical", "funding", "221", "1", "2"), VALID_ROWS[1]]
            e2_path, digest = self.write_fixture(root, rows)
            with self.assertRaisesRegex(ValueError, "expected 222, got 221"):
                finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_wrong_mldsa_size_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [VALID_ROWS[0], ("uniform_mldsa", "funding", "10647", "3", "4")]
            e2_path, digest = self.write_fixture(root, rows)
            with self.assertRaisesRegex(ValueError, "expected 10648, got 10647"):
                finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_invalid_or_nonpositive_size_rejected(self):
        cases = (("not-an-int", "integer"), ("0", "positive"), ("-1", "positive"))
        for value, pattern in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = [("classical", "funding", value, "1", "2"), VALID_ROWS[1]]
                e2_path, digest = self.write_fixture(root, rows)
                with self.assertRaisesRegex(ValueError, pattern):
                    finalizer._extract_funding_sizes(e2_path, expected_sha256=digest)

    def test_missing_e2_file_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "cannot read"):
                finalizer.extract_funding_sizes(root / "missing.csv")

    def test_repeated_finalization_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.csv"
            second = root / "second.csv"
            finalizer.finalize(AUTHORITATIVE_E2, first)
            finalizer.finalize(AUTHORITATIVE_E2, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
