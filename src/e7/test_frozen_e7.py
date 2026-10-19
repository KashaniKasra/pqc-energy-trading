import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import finalize_e7
import freeze_e7


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN_ROOT = PROJECT_ROOT / "raw/e7"
TRACE = PROJECT_ROOT / "env/e7/trace/opsd_household_15min_e7.csv"
PROVENANCE = PROJECT_ROOT / "env/e7/trace/provenance.json"
FINAL_CSV = PROJECT_ROOT / "data/e7_endtoend.csv"


class FrozenE7EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def copy_archive(self):
        destination = self.root / "raw-e7"
        shutil.copytree(FROZEN_ROOT, destination)
        return destination

    @staticmethod
    def load_manifest(root):
        return json.loads((root / "evidence_manifest.json").read_text(encoding="utf-8"))

    @staticmethod
    def write_manifest(root, manifest):
        (root / "evidence_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def entry(manifest, group="funding", config="classical_l2", filename="manifest.json"):
        return next(
            item for item in manifest["entries"]
            if item["evidence_group"] == group
            and item["config"] == config
            and item["original_filename"] == filename
        )

    def replace_original(self, root, transform, **selector):
        manifest = self.load_manifest(root)
        entry = self.entry(manifest, **selector)
        tracked = root / entry["tracked_filename"]
        original = gzip.decompress(tracked.read_bytes())
        changed = transform(original)
        compressed = freeze_e7.deterministic_gzip(changed)
        tracked.write_bytes(compressed)
        entry.update(
            original_bytes=len(changed),
            original_sha256=hashlib.sha256(changed).hexdigest(),
            tracked_bytes=len(compressed),
            tracked_sha256=hashlib.sha256(compressed).hexdigest(),
        )
        self.write_manifest(root, manifest)

    def assert_rejected(self, root):
        output = self.root / "rejected.csv"
        with self.assertRaises(ValueError):
            finalize_e7.finalize_frozen(
                root, TRACE, PROVENANCE, output, PROJECT_ROOT
            )
        self.assertFalse(output.exists())

    def test_successful_frozen_loading_atomically_replaces_destination(self):
        output = self.root / "result.csv"
        output.write_bytes(b"previous valid destination\n")
        finalize_e7.finalize_frozen(FROZEN_ROOT, TRACE, PROVENANCE, output, PROJECT_ROOT)
        self.assertEqual(FINAL_CSV.read_bytes(), output.read_bytes())
        self.assertEqual(finalize_e7.FINAL_CSV_SHA256, hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual([], list(self.root.glob(".result.csv.frozen-*.tmp")))

    def test_hash_mismatch_preserves_existing_destination(self):
        output = self.root / "preserved.csv"
        original_destination = b"pre-existing user data that must survive\n"
        output.write_bytes(original_destination)
        expected_csv = FINAL_CSV.read_bytes()
        real_sha256 = finalize_e7.sha256

        def reject_candidate(content):
            if content == expected_csv:
                return "0" * 64
            return real_sha256(content)

        with mock.patch.object(finalize_e7, "sha256", side_effect=reject_candidate):
            with self.assertRaisesRegex(ValueError, "final CSV SHA-256 mismatch"):
                finalize_e7.finalize_frozen(
                    FROZEN_ROOT, TRACE, PROVENANCE, output, PROJECT_ROOT
                )
        self.assertEqual(original_destination, output.read_bytes())
        self.assertEqual([], list(self.root.glob(".preserved.csv.frozen-*.tmp")))

    def test_archive_generation_is_deterministic(self):
        with finalize_e7.extracted_frozen_evidence(FROZEN_ROOT) as (settlement, funding):
            regenerated = self.root / "regenerated"
            freeze_e7.freeze(
                settlement, funding, regenerated, TRACE, PROVENANCE,
                FINAL_CSV, PROJECT_ROOT,
            )
        expected = {
            path.relative_to(FROZEN_ROOT).as_posix(): path.read_bytes()
            for path in FROZEN_ROOT.rglob("*") if path.is_file()
        }
        actual = {
            path.relative_to(regenerated).as_posix(): path.read_bytes()
            for path in regenerated.rglob("*") if path.is_file()
        }
        self.assertEqual(expected, actual)

    def test_plain_directory_workflow_remains_compatible(self):
        with finalize_e7.extracted_frozen_evidence(FROZEN_ROOT) as (settlement, funding):
            output = self.root / "plain.csv"
            finalize_e7.finalize(
                settlement, funding, TRACE, PROVENANCE, output, PROJECT_ROOT
            )
            self.assertEqual(FINAL_CSV.read_bytes(), output.read_bytes())

    def test_modified_gzip_payload_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        entry = self.entry(manifest)
        tracked = root / entry["tracked_filename"]
        content = bytearray(tracked.read_bytes())
        content[len(content) // 2] ^= 1
        tracked.write_bytes(content)
        entry["tracked_sha256"] = hashlib.sha256(content).hexdigest()
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_truncated_gzip_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        entry = self.entry(manifest)
        tracked = root / entry["tracked_filename"]
        content = tracked.read_bytes()[:-8]
        tracked.write_bytes(content)
        entry["tracked_bytes"] = len(content)
        entry["tracked_sha256"] = hashlib.sha256(content).hexdigest()
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_modified_original_hash_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        self.entry(manifest)["original_sha256"] = "0" * 64
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_modified_compressed_hash_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        self.entry(manifest)["tracked_sha256"] = "0" * 64
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_missing_and_extra_files_are_rejected(self):
        missing = self.copy_archive()
        manifest = self.load_manifest(missing)
        (missing / self.entry(manifest)["tracked_filename"]).unlink()
        self.assert_rejected(missing)
        shutil.rmtree(missing)
        extra = self.copy_archive()
        (extra / "unexpected.gz").write_bytes(b"unexpected")
        self.assert_rejected(extra)

    def test_duplicate_entries_are_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        manifest["entries"].append(dict(manifest["entries"][0]))
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_path_traversal_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        self.entry(manifest)["tracked_filename"] = "../escape.gz"
        self.write_manifest(root, manifest)
        self.assert_rejected(root)

    def test_symlink_is_rejected(self):
        root = self.copy_archive()
        manifest = self.load_manifest(root)
        tracked = root / self.entry(manifest)["tracked_filename"]
        target = self.root / "target.gz"
        target.write_bytes(tracked.read_bytes())
        tracked.unlink()
        tracked.symlink_to(target)
        self.assert_rejected(root)

    def test_wrong_configuration_and_run_id_are_rejected(self):
        for field, value in (("config", "unknown"), ("run_id", "2")):
            root = self.copy_archive()
            manifest = self.load_manifest(root)
            self.entry(manifest)[field] = value
            self.write_manifest(root, manifest)
            self.assert_rejected(root)
            shutil.rmtree(root)

    def test_incorrect_funding_scientific_provenance_is_rejected(self):
        root = self.copy_archive()
        def alter(content):
            value = json.loads(content)
            value["git_commit"] = "0" * 40
            return (json.dumps(value) + "\n").encode()
        self.replace_original(root, alter)
        self.assert_rejected(root)

    def test_integration_smoke_funding_evidence_is_rejected(self):
        root = self.copy_archive()
        def alter(content):
            value = json.loads(content)
            value["schema"] = "pqc-energy-trading.e7-funding-integration-smoke.v1"
            value["mode"] = "integration-smoke"
            value["scientific"] = False
            return (json.dumps(value) + "\n").encode()
        self.replace_original(root, alter)
        self.assert_rejected(root)


if __name__ == "__main__":
    unittest.main()
