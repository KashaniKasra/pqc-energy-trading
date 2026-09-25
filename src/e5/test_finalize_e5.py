import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("finalize_e5.py")
SPEC = importlib.util.spec_from_file_location("finalize_e5", MODULE_PATH)
finalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalizer)


def scientific_environment():
    policy = lambda value: {
        "status": "available",
        "values_by_policy": {"policy0": value, "policy1": value},
    }
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "go_version": "go1.22.2",
        "kernel": "test-kernel",
        "timing": {
            "cpu_model": "test CPU",
            "process_affinity": [2, 4, 6, 8, 10, 12, 14],
            "scaling_driver": policy("amd-pstate-epp"),
            "governor": policy("performance"),
            "energy_performance_preference": policy("performance"),
            "scaling_min_freq_khz": policy("3200000"),
            "scaling_max_freq_khz": policy("3200000"),
            "boost_status": "available",
            "boost_enabled": False,
            "ac_status": "available",
            "ac_online": True,
        },
    }


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def write_condition(root, n_states, *, scientific=True, artifact_root=None):
    condition = root / f"n{n_states}"
    condition.mkdir(parents=True)
    sample_path = condition / f"samples_layer_aware_n{n_states}.csv"
    total_bytes = n_states * (8 + 5387)
    timings = []
    with sample_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(finalizer.RAW_FIELDS)
        for phase, count in (("warmup", 100), ("measured", 1000)):
            for iteration in range(count):
                timing = (iteration + 1) / 1000
                if phase == "measured":
                    timings.append(timing)
                writer.writerow(
                    (
                        iteration,
                        phase,
                        str(scientific).lower(),
                        n_states,
                        5387,
                        total_bytes,
                        f"{timing:.9f}",
                        "true",
                        "",
                    )
                )
    sample_content = sample_path.read_bytes()
    payload = b"P" * 5387
    artifact_name = f"watchtower_layer_aware_n{n_states}.bin"
    artifact_sha = "b" * 64
    if artifact_root is not None:
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_root / artifact_name
        with artifact_path.open("wb") as output:
            for state_id in range(n_states):
                output.write(state_id.to_bytes(8, "big"))
                output.write(payload)
        artifact_sha = sha256(artifact_path.read_bytes())
    manifest = {
        "schema": finalizer.SCHEMA,
        "scientific": scientific,
        "condition": {
            "config": finalizer.CONFIG,
            "n_states": n_states,
            "warmup_iterations": 100,
            "measured_iterations": 1000,
            "worst_case_target_state": n_states - 1,
        },
        "blob": {
            "source": finalizer.BLOB_SOURCE,
            "bytes": 5387,
            "observed_blob_bytes_median": 5387,
            "sha256": sha256(payload),
        },
        "storage_artifact": {
            "filename": artifact_name,
            "bytes": total_bytes,
            "sha256": artifact_sha,
            "record_layout": finalizer.RECORD_LAYOUT,
            "validation": dict(finalizer.ARTIFACT_VALIDATION_RECEIPT),
        },
        "samples": {
            "filename": sample_path.name,
            "bytes": len(sample_content),
            "sha256": sha256(sample_content),
            "row_count": 1100,
            "warmup_count": 100,
            "measured_count": 1000,
            "failure_count": 0,
        },
        "scan_methodology": {
            **finalizer.METHODOLOGY,
            "measured_median_ms": finalizer.percentile(timings, 0.5),
            "measured_p95_ms": finalizer.percentile(timings, 0.95),
            "measured_p99_ms": finalizer.percentile(timings, 0.99),
        },
        "environment": scientific_environment(),
    }
    manifest_path = condition / f"manifest_layer_aware_n{n_states}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path, sample_path


def read_manifest(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(path, manifest):
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def refresh_sample_identity(manifest_path, sample_path):
    manifest = read_manifest(manifest_path)
    content = sample_path.read_bytes()
    manifest["samples"]["bytes"] = len(content)
    manifest["samples"]["sha256"] = sha256(content)
    write_manifest(manifest_path, manifest)


def mutate_rows(sample_path, mutation):
    with sample_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
    mutation(rows)
    with sample_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=finalizer.RAW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class FinalizeE5Tests(unittest.TestCase):
    def test_percentile_linear_interpolation(self):
        self.assertEqual(finalizer.percentile([1.0], 0.99), 1.0)
        self.assertEqual(finalizer.percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(finalizer.percentile([1, 2, 3, 4], 0.95), 3.85)

    def test_frozen_finalization_requires_no_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for n_states in finalizer.STATE_COUNTS:
                write_condition(root, n_states)
            output = root / "final.csv"
            finalizer.finalize(root, output)
            with output.open(newline="", encoding="utf-8") as source:
                rows = list(csv.reader(source))
            self.assertEqual(tuple(rows[0]), finalizer.FINAL_FIELDS)
            self.assertEqual([int(row[1]) for row in rows[1:]], list(finalizer.STATE_COUNTS))
            self.assertTrue(all(row[2] == "5387" for row in rows[1:]))

    def test_collection_receipt_sha_and_methodology_fail_closed(self):
        mutations = (
            ("validation receipt", lambda manifest: manifest["storage_artifact"].pop("validation")),
            ("SHA-256 syntax", lambda manifest: manifest["storage_artifact"].update(sha256="ABC")),
            ("methodology", lambda manifest: manifest["scan_methodology"].update(clock="wall clock")),
        )
        for label, mutation in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest_path, _ = write_condition(root, 10)
                manifest = read_manifest(manifest_path)
                mutation(manifest)
                write_manifest(manifest_path, manifest)
                with self.assertRaises(ValueError):
                    finalizer.summarize_condition(manifest_path, None)

    def test_total_bytes_manifest_and_record_integrity_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, sample_path = write_condition(root, 10)
            mutate_rows(sample_path, lambda rows: rows[0].update(total_bytes="1"))
            refresh_sample_identity(manifest_path, sample_path)
            with self.assertRaisesRegex(ValueError, "total_bytes"):
                finalizer.summarize_condition(manifest_path, None)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, sample_path = write_condition(root, 10)
            manifest = read_manifest(manifest_path)
            wrong_total = manifest["storage_artifact"]["bytes"] + 1
            manifest["storage_artifact"]["bytes"] = wrong_total
            write_manifest(manifest_path, manifest)
            mutate_rows(sample_path, lambda rows: [row.update(total_bytes=str(wrong_total)) for row in rows])
            refresh_sample_identity(manifest_path, sample_path)
            with self.assertRaisesRegex(ValueError, "record-size integrity"):
                finalizer.summarize_condition(manifest_path, None)

    def test_optional_deep_audit_rejects_corrupted_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            manifest_path, _ = write_condition(root / "evidence", 10, artifact_root=artifacts)
            artifact = artifacts / "watchtower_layer_aware_n10.bin"
            with artifact.open("r+b") as output:
                output.seek(9)
                output.write(b"X")
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch"):
                finalizer.summarize_condition(manifest_path, artifacts)

    def test_raw_warmup_inconsistency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, sample_path = write_condition(root, 10)
            mutate_rows(sample_path, lambda rows: rows[0].update(scientific="false"))
            refresh_sample_identity(manifest_path, sample_path)
            with self.assertRaisesRegex(ValueError, "failed or inconsistent"):
                finalizer.summarize_condition(manifest_path, None)

    def test_rejects_incomplete_and_smoke_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_condition(root, 10)
            with self.assertRaisesRegex(ValueError, "exactly all five"):
                finalizer.finalize(root, root / "final.csv")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for n_states in finalizer.STATE_COUNTS:
                write_condition(root, n_states, scientific=False)
            with self.assertRaisesRegex(ValueError, "non-scientific"):
                finalizer.finalize(root, root / "final.csv")


if __name__ == "__main__":
    unittest.main()
