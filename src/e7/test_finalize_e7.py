import csv
import hashlib
import io
import json
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


def valid_rich_evidence_rows(trace):
    trace_sha, trace_rows = finalize_e7._trace_identity_rows(trace)
    rows = []
    for config_name in config.CONFIG_NAMES:
        for trace_idx, source in enumerate(trace_rows):
            phase = "warmup" if trace_idx < prepare_trace.WARMUP_SAMPLES else "measured"
            phase_idx = trace_idx if phase == "warmup" else trace_idx - prepare_trace.WARMUP_SAMPLES
            sample_idx = -1 if phase == "warmup" else phase_idx
            trade_id, direction, amount, payer, payee = finalize_e7._expected_trade(
                trace_sha, source, trace_idx
            )
            row = {field: "" for field in finalize_e7.RAW_FIELDS}
            row.update(
                config=config_name,
                run_id="1",
                phase=phase,
                phase_idx=str(phase_idx),
                sample_idx=str(sample_idx),
                trace_row_idx=str(trace_idx),
                trace_timestamp=source["timestamp"],
                trace_row_sha256=hashlib.sha256(
                    (",".join(source[field] for field in prepare_trace.TRACE_FIELDS) + "\n").encode()
                ).hexdigest(),
                logical_trade_id=trade_id,
                direction=direction,
                amount_kwh=amount,
                payer=payer,
                payee=payee,
                start_monotonic_ns=str(trace_idx * 2_000_000),
                end_monotonic_ns=str(trace_idx * 2_000_000 + 1_000_000),
                latency_ms="1.000000000",
                scientific="true",
                success="true",
                request_id=trade_id,
                completion_status="VALID",
            )
            if config_name.endswith("_l2"):
                row.update(
                    channel_id="c1",
                    htlc_id="e7-v1:" + trade_id,
                    authorization_verified="true",
                    receiver_verified="true",
                    verified="true",
                )
            else:
                row.update(
                    transaction_id=f"tx-{config_name}-{trace_idx}",
                    committed="true",
                    verified="true",
                    validation_status="VALID",
                    validation_code="0",
                    payload_sha256="1" * 64,
                )
            if config_name == "sets_reimpl":
                common = finalize_e7._expected_logical_settlement(
                    trace_sha, source, trace_idx, phase, sample_idx, trade_id,
                    direction, amount, payer, payee,
                )
                row.update(
                    sets_ecdsa_algorithm=finalize_e7.SETS_ECDSA_ALGORITHM,
                    sets_signed_preimage_sha256=hashlib.sha256(common).hexdigest(),
                    sets_public_key_sha256="2" * 64,
                    sets_signature_sha256="3" * 64,
                    sets_signature_verified="true",
                    sets_controller_eligible="true",
                    sets_controller_balance_sufficient="true",
                )
            rows.append(row)
    return rows


def valid_fabric_cpu_isolation():
    cpu_set = finalize_e7.EXPECTED_FABRIC_CPU_SET
    return {
        "expected_fabric_cpu_set": cpu_set,
        "components": [
            {"role": "orderer", "container": "orderer.example.com", "cpuset": cpu_set},
            {"role": "peer0.org1", "container": "peer0.org1.example.com", "cpuset": cpu_set,
             "chaincode_launch_cpuset": cpu_set},
            {"role": "peer1.org1", "container": "peer1.org1.example.com", "cpuset": cpu_set,
             "chaincode_launch_cpuset": cpu_set},
            {"role": "peer0.org2", "container": "peer0.org2.example.com", "cpuset": cpu_set,
             "chaincode_launch_cpuset": cpu_set},
            {"role": "peer1.org2", "container": "peer1.org2.example.com", "cpuset": cpu_set,
             "chaincode_launch_cpuset": cpu_set},
            {
                "role": "simplekv_chaincode",
                "container": "dev-peer0.org1-simplekv",
                "cpuset": cpu_set,
                "chaincode_id": "simplekv_1:" + "a" * 64,
            },
        ],
    }


def write_scientific_runs(root, trace, provenance, rows, mutate_manifest=None):
    evidence_root = root / "evidence"
    project_root = Path(__file__).resolve().parents[2]
    code_hashes = {
        relative: hashlib.sha256((project_root / relative).read_bytes()).hexdigest()
        for relative in finalize_e7.REQUIRED_CODE_CONFIG_PATHS
    }
    for config_name in config.CONFIG_NAMES:
        run = evidence_root / config_name / "run_1"
        run.mkdir(parents=True)
        samples = run / "samples.csv"
        write_evidence(
            samples,
            [row for row in rows if row["config"] == config_name],
            finalize_e7.RAW_FIELDS,
        )
        (run / "run.log").write_text("test run log\n", encoding="utf-8")
        if config_name in {"l1_only", "sets_reimpl"}:
            (run / "transactions.csv").write_text("transaction_id\ntest\n", encoding="utf-8")
            (run / "ledger_heights.csv").write_text("block_number\n1\n", encoding="utf-8")
        manifest = {
            "schema": finalize_e7.MANIFEST_SCHEMA,
            "experiment": "E7", "config": config_name, "run_id": "1",
            "git_commit": "a" * 40, "git_tracked_clean": True,
            "trace_path": str(trace), "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "trace_provenance_path": str(provenance),
            "trace_provenance_sha256": hashlib.sha256(provenance.read_bytes()).hexdigest(),
            "warmup_rows": 100, "measured_rows": 1000,
            "timer_definition": finalize_e7.TIMER_DEFINITION,
            "config_semantics": finalize_e7._expected_config_semantics(config_name),
            "process_network_reset_method": "fresh isolated test process/network",
            "code_config_sha256": dict(code_hashes),
            "runtime_versions": {
                "go": "go-test", "python": "python-test", "fabric": "Version: 2.5.16",
                "fabric_gateway_go": "1.7.1", "liboqs": "0.15.0", "liboqs_go": "0.15.0",
            },
            "scientific_environment": {
                "cpu_model": "AMD Ryzen 7 7735HS", "logical_cpu_count": 16,
                "scaling_driver": ["amd-pstate-epp"], "governor": ["performance"],
                "energy_performance_preference": ["performance"],
                "scaling_min_freq_khz": [3200000], "scaling_max_freq_khz": [3200000],
                "boost_enabled": False, "ac_online": True,
                "process_affinity": finalize_e7.EXPECTED_AFFINITY,
                "expected_affinity": finalize_e7.EXPECTED_AFFINITY, "passed": True,
            },
            "status": "complete", "prepared_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:01Z", "completed_at": "2026-01-01T00:01:00Z",
            "raw_files": {
                filename.name: finalize_e7._file_evidence(filename)
                for filename in run.iterdir() if filename.is_file()
            },
        }
        if config_name in {"l1_only", "sets_reimpl"}:
            manifest["fabric_cpu_isolation"] = valid_fabric_cpu_isolation()
        if mutate_manifest:
            mutate_manifest(config_name, manifest)
        (run / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return evidence_root


class FinalizeE7Tests(unittest.TestCase):
    def test_render_final_schema_order_and_determinism(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = list(reversed(valid_evidence_rows()))
            first = finalize_e7.render_final(finalize_e7.validate_rows(rows, 1100))
            second = finalize_e7.render_final(finalize_e7.validate_rows(rows, 1100))
            self.assertEqual(first, second)
            output = list(csv.reader(io.StringIO(first.decode("utf-8"))))
            self.assertEqual(tuple(output[0]), finalize_e7.FINAL_FIELDS)
            self.assertEqual(len(output), 4001)
            self.assertEqual(output[1][:3], ["l1_only", "1", "0"])
            self.assertEqual(output[1001][:3], ["sets_reimpl", "1", "0"])
            self.assertEqual(output[-1][:3], ["layer_aware_l2", "1", "999"])

    def test_finalize_rejects_final_shaped_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace, provenance = write_trace(root)
            evidence = root / "final-shaped.csv"
            write_evidence(evidence, valid_evidence_rows())
            with self.assertRaisesRegex(ValueError, "requires an evidence root"):
                finalize_e7.finalize(evidence, trace, provenance, root / "output.csv", Path(__file__).resolve().parents[2])

    def test_finalize_accepts_rich_raw_and_emits_professor_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = Path(__file__).resolve().parents[2]
            trace = repository / "env/e7/trace/opsd_household_15min_e7.csv"
            provenance = repository / "env/e7/trace/provenance.json"
            evidence = write_scientific_runs(
                root, trace, provenance, valid_rich_evidence_rows(trace)
            )
            output_path = root / "output.csv"
            finalize_e7.finalize(evidence, trace, provenance, output_path, repository)
            with output_path.open(newline="", encoding="utf-8") as source:
                output = list(csv.reader(source))
            self.assertEqual(tuple(output[0]), finalize_e7.FINAL_FIELDS)
            self.assertEqual(len(output), 4001)

    def test_finalize_requires_exact_onchain_fabric_cpu_provenance_only(self):
        cases = {
            "broad": lambda value: value["components"][0].update(cpuset="2-15"),
            "missing_peer": lambda value: value["components"].__delitem__(1),
            "missing_orderer": lambda value: value["components"].__delitem__(0),
            "missing_chaincode": lambda value: value["components"].pop(),
            "wrong_chaincode_cpu": lambda value: value["components"][-1].update(cpuset="2-15"),
            "missing_peer_launch_policy": lambda value: value["components"][1].pop("chaincode_launch_cpuset"),
            "wrong_peer_launch_policy": lambda value: value["components"][1].update(chaincode_launch_cpuset="2-15"),
        }
        repository = Path(__file__).resolve().parents[2]
        trace = repository / "env/e7/trace/opsd_household_15min_e7.csv"
        provenance = repository / "env/e7/trace/provenance.json"
        for name, mutation in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def mutate(config_name, manifest):
                    if config_name == "l1_only":
                        mutation(manifest["fabric_cpu_isolation"])

                evidence = write_scientific_runs(
                    root, trace, provenance, valid_rich_evidence_rows(trace), mutate
                )
                with self.assertRaisesRegex(ValueError, "Fabric CPU"):
                    finalize_e7.finalize(
                        evidence, trace, provenance, root / "output.csv", repository
                    )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def add_to_l2(config_name, manifest):
                if config_name == "classical_l2":
                    manifest["fabric_cpu_isolation"] = valid_fabric_cpu_isolation()

            evidence = write_scientific_runs(
                root, trace, provenance, valid_rich_evidence_rows(trace), add_to_l2
            )
            with self.assertRaisesRegex(ValueError, "L2 manifest"):
                finalize_e7.finalize(
                    evidence, trace, provenance, root / "output.csv", repository
                )

    def test_finalize_rejects_samples_without_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace, provenance = write_trace(root)
            evidence = root / "evidence"
            run = evidence / "l1_only" / "run_1"
            run.mkdir(parents=True)
            write_evidence(run / "samples.csv", [], finalize_e7.RAW_FIELDS)
            with self.assertRaisesRegex(ValueError, "manifest is missing"):
                finalize_e7.finalize(evidence, trace, provenance, root / "output.csv", Path(__file__).resolve().parents[2])

    def test_finalize_rejects_invalid_manifest_controls_and_hash(self):
        cases = (
            ("dirty", lambda _, m: m.update(git_tracked_clean=False)),
            ("governor", lambda _, m: m["scientific_environment"].update(governor=["powersave"])),
            ("affinity", lambda _, m: m["scientific_environment"].update(process_affinity=[0])),
            ("samples_hash", lambda _, m: m["raw_files"]["samples.csv"].update(sha256="0" * 64)),
        )
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                trace, provenance = write_trace(root)
                evidence = write_scientific_runs(
                    root, trace, provenance, valid_rich_evidence_rows(trace),
                    lambda config_name, manifest: mutate(config_name, manifest)
                    if config_name == "l1_only" else None,
                )
                with self.assertRaises(ValueError):
                    finalize_e7.finalize(evidence, trace, provenance, root / "output.csv", Path(__file__).resolve().parents[2])

    def test_finalize_rejects_manifest_identity_status_and_trace_mismatch(self):
        cases = (
            ("missing", lambda m: m.pop("timer_definition")),
            ("trace", lambda m: m.update(trace_sha256="0" * 64)),
            ("config", lambda m: m.update(config="sets_reimpl")),
            ("run_id", lambda m: m.update(run_id="2")),
            ("status", lambda m: m.update(status="failed")),
        )
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository = Path(__file__).resolve().parents[2]
                trace = repository / "env/e7/trace/opsd_household_15min_e7.csv"
                provenance = repository / "env/e7/trace/provenance.json"
                evidence = write_scientific_runs(
                    root, trace, provenance, valid_rich_evidence_rows(trace),
                    lambda config_name, manifest: mutate(manifest)
                    if config_name == "l1_only" else None,
                )
                with self.assertRaises(ValueError):
                    finalize_e7.finalize(evidence, trace, provenance, root / "output.csv", repository)

    def test_finalize_enforces_exact_real_code_config_hashes(self):
        def fabricated(manifest):
            manifest["code_config_sha256"] = {
                key: "0" * 64 for key in finalize_e7.REQUIRED_CODE_CONFIG_PATHS
            }

        def different(manifest):
            key = finalize_e7.REQUIRED_CODE_CONFIG_PATHS[0]
            manifest["code_config_sha256"][key] = "0" * 64

        def missing(manifest):
            manifest["code_config_sha256"].pop(finalize_e7.REQUIRED_CODE_CONFIG_PATHS[0])

        def extra(manifest):
            manifest["code_config_sha256"]["../escape"] = "0" * 64

        for name, mutation in (
            ("fabricated", fabricated), ("different_config", different),
            ("missing", missing), ("extra_or_traversal", extra),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository = Path(__file__).resolve().parents[2]
                trace = repository / "env/e7/trace/opsd_household_15min_e7.csv"
                provenance = repository / "env/e7/trace/provenance.json"
                evidence = write_scientific_runs(
                    root, trace, provenance, valid_rich_evidence_rows(trace),
                    lambda config_name, manifest: mutation(manifest)
                    if config_name == "l1_only" else None,
                )
                with self.assertRaises(ValueError):
                    finalize_e7.finalize(
                        evidence, trace, provenance, root / "output.csv", repository
                    )

    def test_sets_proof_fields_required_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace, _ = write_trace(root)
            rows = valid_rich_evidence_rows(trace)
            sets_row = next(row for row in rows if row["config"] == "sets_reimpl")
            for field, value in (
                ("sets_ecdsa_algorithm", "wrong"),
                ("sets_signed_preimage_sha256", "A" * 64),
                ("sets_public_key_sha256", "A" * 64),
                ("sets_signature_sha256", "short"),
                ("sets_signature_verified", "false"),
                ("sets_controller_eligible", "false"),
                ("sets_controller_balance_sufficient", "false"),
            ):
                original = sets_row[field]
                sets_row[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError, "SETS ECDSA/controller evidence mismatch"
                ):
                    finalize_e7.validate_raw_rows(rows, trace)
                sets_row[field] = original

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

    def test_rich_raw_identity_mismatch_fails_closed(self):
        trace = Path(__file__).resolve().parents[2] / "env/e7/trace/opsd_household_15min_e7.csv"
        trace_sha, trace_rows = finalize_e7._trace_identity_rows(trace)
        trace_idx = 100
        source = trace_rows[trace_idx]
        trade_id, direction, amount, payer, payee = finalize_e7._expected_trade(
            trace_sha, source, trace_idx
        )
        row = {field: "" for field in finalize_e7.RAW_FIELDS}
        row.update(
            config="l1_only", run_id="1", phase="measured", phase_idx="0",
            sample_idx="0", trace_row_idx=str(trace_idx),
            trace_timestamp=source["timestamp"],
            trace_row_sha256=hashlib.sha256(
                (",".join(source[field] for field in prepare_trace.TRACE_FIELDS) + "\n").encode()
            ).hexdigest(),
            logical_trade_id=trade_id, direction=direction, amount_kwh=amount,
            payer=payer, payee=payee, start_monotonic_ns="1",
            end_monotonic_ns="1000001", latency_ms="1.000000000",
            scientific="true", success="true", transaction_id="tx",
            completion_status="VALID", committed="true", verified="true",
        )
        tampered = dict(row, logical_trade_id="0" * 64)
        with self.assertRaisesRegex(ValueError, "logical trade identity mismatch"):
            finalize_e7.validate_raw_rows([tampered], trace)
        malformed = dict(row)
        malformed["run_id"] = None
        with self.assertRaisesRegex(ValueError, "malformed E7 raw evidence row"):
            finalize_e7.validate_raw_rows([malformed], trace)


if __name__ == "__main__":
    unittest.main()
