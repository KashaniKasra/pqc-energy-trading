#!/usr/bin/env python3
"""Validate frozen E7 settlement/funding evidence and emit the professor CSV."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from decimal import Decimal, InvalidOperation
from datetime import timedelta
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

try:
    from .config import CONFIG_NAMES, get_configuration
    from .prepare_trace import (
        DATASET_LICENCE,
        DATASET_NAME,
        DATASET_PRIMARY_SOURCE,
        DATASET_RESOLUTION,
        DATASET_VERSION,
        COUNTER_POLICY,
        INTERVAL,
        INTERVAL_TIMESTAMP_CONVENTION,
        INTERPOLATION_COLUMN,
        MINIMUM_TRACE_ROWS,
        PROVENANCE_SCHEMA,
        SELECTION_POLICY,
        TRACE_FIELDS,
        TRANSFORMATION,
        UPSTREAM_REPRESENTATION,
        UPSTREAM_FILENAME,
        WARMUP_SAMPLES,
        parse_energy,
        parse_timestamp,
        sha256,
    )
except ImportError:  # Direct execution: python3 src/e7/finalize_e7.py
    from config import CONFIG_NAMES, get_configuration
    from prepare_trace import (
        DATASET_LICENCE,
        DATASET_NAME,
        DATASET_PRIMARY_SOURCE,
        DATASET_RESOLUTION,
        DATASET_VERSION,
        COUNTER_POLICY,
        INTERVAL,
        INTERVAL_TIMESTAMP_CONVENTION,
        INTERPOLATION_COLUMN,
        MINIMUM_TRACE_ROWS,
        PROVENANCE_SCHEMA,
        SELECTION_POLICY,
        TRACE_FIELDS,
        TRANSFORMATION,
        UPSTREAM_REPRESENTATION,
        UPSTREAM_FILENAME,
        WARMUP_SAMPLES,
        parse_energy,
        parse_timestamp,
        sha256,
    )


SETTLEMENT_FIELDS = ("config", "run_id", "sample_idx", "latency_ms")
FINAL_FIELDS = (*SETTLEMENT_FIELDS, "amortized_setup_ms")
RAW_FIELDS = (
    "config", "run_id", "phase", "phase_idx", "sample_idx", "trace_row_idx",
    "trace_timestamp", "trace_row_sha256", "logical_trade_id", "direction",
    "amount_kwh", "payer", "payee", "start_monotonic_ns", "end_monotonic_ns",
    "latency_ms", "scientific", "success", "error", "request_id",
    "transaction_id", "completion_status", "channel_id", "htlc_id",
    "add_algorithm", "settle_algorithm", "add_serialized_bytes",
    "settle_serialized_bytes", "add_serialized_sha256",
    "settle_serialized_sha256", "authorization_verified", "receiver_verified",
    "committed", "verified", "validation_status", "validation_code",
    "fabric_block_number", "payload_sha256",
    "sets_ecdsa_algorithm", "sets_signed_preimage_sha256",
    "sets_public_key_sha256", "sets_signature_sha256",
    "sets_signature_verified", "sets_controller_eligible",
    "sets_controller_balance_sufficient",
)
MINIMUM_MEASURED_SAMPLES = 1000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SETS_ECDSA_ALGORITHM = "ECDSA-P256-SHA256"
MANIFEST_SCHEMA = "pqc-energy-trading.e7-run-manifest.v1"
FUNDING_MANIFEST_SCHEMA = "pqc-energy-trading.e7-funding-evidence.v2"
FUNDING_AUDIT_SCHEMA = "pqc-energy-trading.e7-funding-transaction-audit.v1"
FROZEN_EVIDENCE_SCHEMA = "pqc-energy-trading.raw-e7-evidence-manifest.v1"
SETTLEMENT_MEASUREMENT_COMMIT = "0a6d20cf06d8e452b0d9bbd85922baff058cdca0"
FUNDING_MEASUREMENT_COMMIT = "839f056966a6360466ba655f64a7e07b6a02fafc"
FINAL_CSV_SHA256 = "000a89eb0923a4f52ebc64e7e38c79696afe8c90809aa8d55435b83c25d49704"
FUNDING_TIMER_DEFINITION = "Linux CLOCK_MONOTONIC immediately before Fabric Gateway simplekv.Set submission through successful committed VALID commit-status return"
TIMER_DEFINITION = "Go monotonic time immediately before executor.Settle through successful completion return"
EXPECTED_AFFINITY = [2, 4, 6, 8, 10, 12, 14]
EXPECTED_FABRIC_CPU_SET = "2,4,6,8,10,12,14"
EXPECTED_TRACE_SHA256 = "7e472c032317a7e8f0af587782238cab48f22ddc50f74200cff65e9681f8bd1c"
EXPECTED_TRACE_PROVENANCE_SHA256 = "3f1e5933b0789d722b01f8593d673488e14b54d35fec335762185ef24f2e4eec"
SIMPLEKV_PACKAGE_ID_PATTERN = re.compile(r"^simplekv_1:[0-9a-f]{64}$")
REQUIRED_CODE_CONFIG_PATHS = (
    "src/e7/cmd/e7bench/main.go", "src/e7/runner.go", "src/e7/manifest.go",
    "src/e7/evidence.go", "src/e7/model.go", "src/e7/state.go", "src/e7/onchain.go",
    "src/e7/l2.go", "src/e7/fabric.go", "src/e7/runio.go", "src/e7/lifecycle.go",
    "src/e7/fabriccpu.go", "src/e7/versions.go", "src/e7/go.mod", "src/e7/go.sum", "src/e7/config.py",
    "src/e7/finalize_e7.py", "env/e7/fabric_network.sh",
    "env/e7/docker-compose.fabric-cpu.yaml", "env/e7/docker-shim/docker",
    "env/fabric/e1/setup_fabric_e1.sh", "env/fabric/e1/docker-compose.yaml",
    "env/e7/trace/opsd_household_15min_e7.csv", "env/e7/trace/provenance.json",
)
REQUIRED_FUNDING_CODE_CONFIG_PATHS = (
    "src/e7/cmd/e7funding/main.go", "src/e7/funding.go", "src/e7/fundingaudit.go",
    "src/e7/fabric.go", "src/e7/lifecycle.go", "src/e7/fabriccpu.go",
    "src/e7/onchain.go", "src/e7/manifest.go", "src/e7/versions.go", "src/e7/finalize_e7.py",
    "src/e7/go.mod", "src/e7/go.sum", "env/e7/fabric_network.sh",
    "env/e7/docker-compose.fabric-cpu.yaml", "env/e7/docker-shim/docker",
    "env/fabric/e1/setup_fabric_e1.sh", "env/fabric/e1/docker-compose.yaml",
    "env/fabric/e1/build_fabric_pq.sh", "env/fabric/e1/patches/fabric-2.5.16-pq-identities.patch",
    "env/fabric/e1/configtx.yaml", "env/fabric/e1/crypto-config.yaml",
    "src/e1/chaincode/chaincode.go", "src/e1/chaincode/go.mod", "src/e1/chaincode/go.sum",
    "src/e1/pqidentity/generate.go", "src/e1/pqidentity/go.mod", "src/e1/pqidentity/go.sum",
    "env/e7/trace/opsd_household_15min_e7.csv", "env/e7/trace/provenance.json",
)
FUNDING_RAW_FILES = {
    "funding.csv", "run.log", "funding_block.pb", "transaction_audit.json",
    "ledger_readback.json",
}
FUNDING_CSV_FIELDS = (
    "target_config", "run_id", "start_monotonic_ns", "end_monotonic_ns",
    "funding_duration_ns", "transaction_id",
    "block_number", "validation_status", "validation_code", "committed",
    "verified", "readback_verified", "payload_sha256",
)
SETTLEMENT_ARCHIVE_FILES = {
    "l1_only": {"manifest.json", "samples.csv", "run.log", "transactions.csv", "ledger_heights.csv"},
    "sets_reimpl": {"manifest.json", "samples.csv", "run.log", "transactions.csv", "ledger_heights.csv"},
    "classical_l2": {"manifest.json", "samples.csv", "run.log"},
    "layer_aware_l2": {"manifest.json", "samples.csv", "run.log"},
}
FUNDING_ARCHIVE_FILES = {
    config: {"manifest.json", *FUNDING_RAW_FILES}
    for config in ("classical_l2", "layer_aware_l2")
}
FROZEN_SOURCE_FILE_COUNT = 28
FUNDING_DURATIONS_NS = {
    "classical_l2": 2023056144,
    "layer_aware_l2": 2026076261,
}
FUNDING_TRANSACTION_IDS = {
    "classical_l2": "a931ab3092622bf0922c5503de8aa9bafeca26362cba6763ef789d9645a5de7f",
    "layer_aware_l2": "01802db50c3c1b247e15a346d83ecdc6df95e383ff083c92f8f01c94589602db",
}


def _file_evidence(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _git_blob_sha256(project_root: Path, commit: str, relative: str) -> str:
    if (not GIT_COMMIT_PATTERN.fullmatch(commit)
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("E7 Git code/config provenance path is invalid")
    try:
        content = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{commit}:{relative}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("E7 recorded Git code/config blob is unavailable") from error
    return sha256(content)


def _validate_git_code_map(project_root: Path, commit: str, values: object,
                           expected_paths: tuple[str, ...]) -> None:
    if (not isinstance(values, dict) or set(values) != set(expected_paths)
            or any(not SHA256_PATTERN.fullmatch(str(value)) for value in values.values())):
        raise ValueError("E7 code/config provenance key set or digest is invalid")
    for relative in expected_paths:
        if _git_blob_sha256(project_root, commit, relative) != values[relative]:
            raise ValueError("E7 code/config SHA-256 does not match recorded Git commit")


def _expected_config_semantics(config: str) -> dict[str, object]:
    common = {"channel": "energychannel", "chaincode": "simplekv", "function": "Set"}
    if config == "l1_only":
        return {"path": "fabric_l1", "fabric_identity_mode": "ml-dsa-65", **common,
                "completion": "committed and VALID Fabric status"}
    if config == "sets_reimpl":
        return {"path": "fabric_l1", "fabric_identity_mode": "ecdsa",
                "application_ecdsa": SETS_ECDSA_ALGORITHM,
                "sets_provenance": "our reimplementation; not the original authors' code",
                **common, "completion": "committed and VALID Fabric status"}
    if config == "classical_l2":
        return {"path": "direct_l2", "e2_configuration": "classical",
                "crypto": "Ed25519 representative compact classical semantics; not MuSig2",
                "transport": "persistent direct TCP; no Mininet or artificial delay"}
    if config == "layer_aware_l2":
        return {"path": "direct_l2", "e2_configuration": "layer_aware",
                "crypto": "Falcon-padded-512 for HTLC add/settle",
                "setup": "channel setup, funding, keys and TCP readiness before row 0",
                "transport": "persistent direct TCP; no Mininet or artificial delay"}
    raise ValueError("unknown E7 configuration")


def validate_fabric_cpu_isolation(value: object) -> None:
    if not isinstance(value, dict) or value.get("expected_fabric_cpu_set") != EXPECTED_FABRIC_CPU_SET:
        raise ValueError("E7 Fabric CPU isolation provenance is missing")
    components = value.get("components")
    if not isinstance(components, list):
        raise ValueError("E7 Fabric CPU component provenance is malformed")
    required = {
        "orderer": "orderer.example.com",
        "peer0.org1": "peer0.org1.example.com",
        "peer1.org1": "peer1.org1.example.com",
        "peer0.org2": "peer0.org2.example.com",
        "peer1.org2": "peer1.org2.example.com",
    }
    observed, containers, chaincodes = set(), set(), 0
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("E7 Fabric CPU component provenance is malformed")
        role, container, cpuset = (
            component.get("role"), component.get("container"), component.get("cpuset")
        )
        if not isinstance(container, str) or not container or container in containers or cpuset != EXPECTED_FABRIC_CPU_SET:
            raise ValueError("E7 Fabric CPU component identity/cpuset is invalid")
        containers.add(container)
        if role == "simplekv_chaincode":
            if (not SIMPLEKV_PACKAGE_ID_PATTERN.fullmatch(str(component.get("chaincode_id", "")))
                    or component.get("chaincode_launch_cpuset") not in (None, "")):
                raise ValueError("E7 simplekv chaincode CPU provenance is invalid")
            chaincodes += 1
        elif (role in required and role not in observed
              and container == required[role] and not component.get("chaincode_id")):
            launch_cpuset = component.get("chaincode_launch_cpuset")
            if role == "orderer":
                if launch_cpuset not in (None, ""):
                    raise ValueError("E7 Fabric CPU orderer must not record chaincode launch provenance")
            elif launch_cpuset != EXPECTED_FABRIC_CPU_SET:
                raise ValueError("E7 Fabric CPU peer chaincode launch provenance is invalid")
            observed.add(role)
        else:
            raise ValueError("E7 Fabric CPU component role is invalid")
    if observed != set(required) or chaincodes < 1:
        raise ValueError("E7 Fabric CPU isolation provenance is incomplete")


def validate_manifest(manifest_path: Path, samples_path: Path, config: str,
                      trace_path: Path, provenance_path: Path,
                      project_root: Path) -> dict:
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("E7 scientific manifest is missing or malformed") from error
    required = {
        "schema", "experiment", "config", "run_id", "git_commit",
        "git_tracked_clean", "trace_path", "trace_sha256",
        "trace_provenance_path", "trace_provenance_sha256", "warmup_rows",
        "measured_rows", "timer_definition", "config_semantics",
        "process_network_reset_method", "code_config_sha256", "runtime_versions",
        "scientific_environment", "status", "prepared_at", "started_at",
        "completed_at", "raw_files",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("E7 scientific manifest is incomplete")
    if (manifest["schema"] != MANIFEST_SCHEMA or manifest["experiment"] != "E7"
            or manifest["config"] != config or manifest["run_id"] != "1"
            or manifest["status"] != "complete"):
        raise ValueError("E7 manifest identity/status mismatch")
    if (manifest["git_commit"] != SETTLEMENT_MEASUREMENT_COMMIT
            or manifest["git_tracked_clean"] is not True):
        raise ValueError("E7 manifest Git provenance is invalid")
    if (manifest["trace_sha256"] != EXPECTED_TRACE_SHA256
            or manifest["trace_sha256"] != sha256(trace_path.read_bytes())
            or manifest["trace_provenance_sha256"] != sha256(provenance_path.read_bytes())):
        raise ValueError("E7 manifest trace provenance mismatch")
    if (manifest["warmup_rows"] != 100 or manifest["measured_rows"] != 1000
            or manifest["timer_definition"] != TIMER_DEFINITION):
        raise ValueError("E7 manifest population/timer mismatch")
    if manifest["config_semantics"] != _expected_config_semantics(config):
        raise ValueError("E7 manifest configuration semantics mismatch")
    if config in {"l1_only", "sets_reimpl"}:
        validate_fabric_cpu_isolation(manifest.get("fabric_cpu_isolation"))
    elif manifest.get("fabric_cpu_isolation") is not None:
        raise ValueError("E7 L2 manifest must not contain Fabric CPU isolation provenance")
    code_hashes = manifest["code_config_sha256"]
    if (not manifest["process_network_reset_method"]
            or not isinstance(manifest["runtime_versions"], dict)):
        raise ValueError("E7 manifest code/runtime/reset provenance missing")
    _validate_git_code_map(
        project_root, manifest["git_commit"], code_hashes, REQUIRED_CODE_CONFIG_PATHS
    )
    versions = manifest["runtime_versions"]
    if not versions.get("go") or not versions.get("python") or versions.get("fabric_gateway_go") != "1.7.1":
        raise ValueError("E7 manifest runtime versions are invalid")
    if config in {"l1_only", "sets_reimpl"} and "2.5.16" not in versions.get("fabric", ""):
        raise ValueError("E7 Fabric version provenance is invalid")
    if config.endswith("_l2") and (versions.get("liboqs") != "0.15.0" or versions.get("liboqs_go") not in {"0.15.0", "v0.15.0"}):
        raise ValueError("E7 liboqs version provenance is invalid")
    environment = manifest["scientific_environment"]
    if not isinstance(environment, dict):
        raise ValueError("E7 scientific controls missing")
    boost, ac = environment.get("boost_enabled"), environment.get("ac_online")
    if (environment.get("passed") is not True or environment.get("process_affinity") != EXPECTED_AFFINITY
            or set(environment.get("scaling_driver", [])) != {"amd-pstate-epp"}
            or set(environment.get("governor", [])) != {"performance"}
            or set(environment.get("energy_performance_preference", [])) != {"performance"}
            or not environment.get("scaling_min_freq_khz")
            or set(environment["scaling_min_freq_khz"]) != {3200000}
            or set(environment.get("scaling_max_freq_khz", [])) != {3200000}
            or boost is not False or ac is not True):
        raise ValueError("E7 scientific controls did not pass")
    required_raw = {"samples.csv", "run.log"}
    if config in {"l1_only", "sets_reimpl"}:
        required_raw |= {"transactions.csv", "ledger_heights.csv"}
    raw_files = manifest["raw_files"]
    if not isinstance(raw_files, dict) or not required_raw.issubset(raw_files):
        raise ValueError("E7 required raw evidence hashes are missing")
    for filename, recorded in raw_files.items():
        if Path(filename).name != filename or recorded != _file_evidence(manifest_path.parent / filename):
            raise ValueError("E7 raw evidence hash/size mismatch")
    return manifest


def load_scientific_runs(evidence_root: Path, trace_path: Path,
                         provenance_path: Path,
                         project_root: Path) -> tuple[list[dict[str, str]], list[dict]]:
    if not evidence_root.is_dir():
        raise ValueError("production E7 finalization requires an evidence root with manifests")
    if {path.name for path in evidence_root.iterdir()} != set(CONFIG_NAMES):
        raise ValueError("E7 settlement evidence root has missing or extra entries")
    all_rows, manifests = [], []
    for config in CONFIG_NAMES:
        run = evidence_root / config / "run_1"
        if ({path.name for path in (evidence_root / config).iterdir()} != {"run_1"}
                or not run.is_dir()):
            raise ValueError("E7 settlement evidence has duplicate or unexpected runs")
        samples, manifest_path = run / "samples.csv", run / "manifest.json"
        manifest = validate_manifest(
            manifest_path, samples, config, trace_path, provenance_path, project_root
        )
        rows = load_raw_evidence(samples)
        expected_files = {"manifest.json", "samples.csv", "run.log"}
        if config in {"l1_only", "sets_reimpl"}:
            expected_files |= {"transactions.csv", "ledger_heights.csv"}
        if {path.name for path in run.iterdir()} != expected_files:
            raise ValueError("E7 settlement run has missing or extra evidence files")
        if any(row.get("config") != config or row.get("run_id") != "1" for row in rows):
            raise ValueError("E7 run evidence config/run_id mismatch")
        all_rows.extend(rows)
        manifests.append(manifest)
    commits = {manifest["git_commit"] for manifest in manifests}
    if len(commits) != 1:
        raise ValueError("E7 manifests have mixed Git commits")
    code_maps = [manifest["code_config_sha256"] for manifest in manifests]
    if any(code_map != code_maps[0] for code_map in code_maps[1:]):
        raise ValueError("E7 manifests have inconsistent code/config hashes")
    return all_rows, manifests


def _load_json(path: Path, description: str) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} is missing or malformed") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def _validate_funding_audit(audit: dict, manifest: dict, block_path: Path) -> None:
    completion = manifest["completion"]
    expected_peer = (
        "ECDSA-P256-SHA256" if manifest["target_config"] == "classical_l2"
        else "ML-DSA-65-SHA256"
    )
    if (audit.get("schema") != FUNDING_AUDIT_SCHEMA
            or audit.get("transaction_id") != completion.get("transaction_id")
            or audit.get("transaction_id_verified") is not True
            or audit.get("block_number") != completion.get("block_number")
            or audit.get("validation_status") != "VALID"
            or audit.get("validation_code") != 0
            or audit.get("block_data_hash_verified") is not True
            or audit.get("channel") != "energychannel"
            or audit.get("chaincode") != "simplekv"
            or audit.get("function") != "Set"
            or audit.get("commitment_key") != manifest.get("commitment_key")
            or audit.get("commitment_sha256") != manifest.get("commitment_payload_sha256")
            or audit.get("block_sha256") != sha256(block_path.read_bytes())
            or audit.get("all_signatures_verified") is not True
            or audit.get("required_endorsement_msp_ids") != ["Org1MSP", "Org2MSP"]):
        raise ValueError("E7 funding transaction/block audit mismatch")
    creator = audit.get("gateway_client_creator")
    if (not isinstance(creator, dict)
            or creator.get("algorithm") != "ECDSA-P256-SHA256"
            or creator.get("signature_verified") is not True
            or not SHA256_PATTERN.fullmatch(str(creator.get("certificate_sha256", "")))
            or not SHA256_PATTERN.fullmatch(str(creator.get("signed_message_sha256", "")))
            or not SHA256_PATTERN.fullmatch(str(creator.get("signature_sha256", "")))):
        raise ValueError("E7 funding Gateway client signature audit is invalid")
    endorsements = audit.get("peer_endorsements")
    if not isinstance(endorsements, list) or len(endorsements) < 2:
        raise ValueError("E7 funding endorsement audit is incomplete")
    observed_msp = set()
    for endorsement in endorsements:
        if (not isinstance(endorsement, dict)
                or endorsement.get("algorithm") != expected_peer
                or endorsement.get("signature_verified") is not True
                or not SHA256_PATTERN.fullmatch(str(endorsement.get("certificate_sha256", "")))
                or not SHA256_PATTERN.fullmatch(str(endorsement.get("signed_message_sha256", "")))
                or not SHA256_PATTERN.fullmatch(str(endorsement.get("signature_sha256", "")))):
            raise ValueError("E7 funding peer endorsement audit is invalid")
        observed_msp.add(endorsement.get("msp_id"))
    if not {"Org1MSP", "Org2MSP"}.issubset(observed_msp):
        raise ValueError("E7 funding endorsements do not cover both organizations")


def _expected_funding_payload(target_config: str) -> bytes:
    values = (
        ("schema_version", "e7-channel-funding-v1"),
        ("target_config", target_config), ("run_id", "1"),
        ("settlement_measurement_commit", SETTLEMENT_MEASUREMENT_COMMIT),
        ("trace_sha256", EXPECTED_TRACE_SHA256), ("channel_id", "c1"),
        ("prosumer", "residential3"),
        ("counterparty", "external-market-grid-counterparty"),
        ("prosumer_initial_kwh_credit", "104.346"),
        ("counterparty_initial_kwh_credit", "0"),
        ("interpretation", "measurement commitment only; not enforceable escrow or on-chain asset locking"),
    )
    return b"".join(_identity_field(name, value) for name, value in values)


def validate_funding_manifest(run: Path, target_config: str,
                              project_root: Path) -> tuple[dict, int]:
    manifest_path = run / "manifest.json"
    manifest = _load_json(manifest_path, "E7 funding manifest")
    required = {
        "schema", "experiment", "mode", "scientific", "target_config", "fabric_network_mode", "run_id",
        "settlement_measurement_commit", "git_commit", "git_tracked_clean",
        "trace_sha256", "trace_provenance_sha256", "warmup_rows_excluded", "measured_settlement_denominator",
        "fabric_channel", "fabric_chaincode", "fabric_function", "commitment_key",
        "commitment_payload_sha256", "commitment_interpretation",
        "gateway_client_signature_algorithm", "peer_endorsement_signature_algorithm",
        "timer_definition", "start_monotonic_ns", "end_monotonic_ns",
        "funding_duration_ns", "completion",
        "independent_ledger_readback_verified",
        "transaction_specific_block_endorsement_verification",
        "code_config_sha256", "runtime_versions", "scientific_environment",
        "fabric_cpu_isolation", "status", "raw_files",
    }
    if not required.issubset(manifest):
        raise ValueError("E7 funding manifest is incomplete")
    expected_mode = "ecdsa" if target_config == "classical_l2" else "ml-dsa-65"
    expected_peer = "ECDSA-P256-SHA256" if target_config == "classical_l2" else "ML-DSA-65-SHA256"
    expected_payload_sha256 = sha256(_expected_funding_payload(target_config))
    if (manifest["schema"] != FUNDING_MANIFEST_SCHEMA
            or manifest["experiment"] != "E7 supplemental channel funding"
            or manifest["mode"] != "scientific" or manifest["scientific"] is not True
            or manifest["target_config"] != target_config or manifest["run_id"] != "1"
            or manifest["settlement_measurement_commit"] != SETTLEMENT_MEASUREMENT_COMMIT
            or manifest["git_tracked_clean"] is not True or manifest["status"] != "complete"
            or not GIT_COMMIT_PATTERN.fullmatch(str(manifest["git_commit"]))
            or manifest["trace_sha256"] != EXPECTED_TRACE_SHA256
            or manifest["trace_provenance_sha256"] != EXPECTED_TRACE_PROVENANCE_SHA256
            or manifest["warmup_rows_excluded"] != 100
            or manifest["measured_settlement_denominator"] != 1000
            or manifest["fabric_network_mode"] != expected_mode
            or manifest["fabric_channel"] != "energychannel"
            or manifest["fabric_chaincode"] != "simplekv"
            or manifest["fabric_function"] != "Set"
            or manifest["commitment_key"] != f"e7/funding/{target_config}/1"
            or manifest["commitment_payload_sha256"] != expected_payload_sha256
            or manifest["commitment_interpretation"] != "measurement commitment only; not enforceable escrow or on-chain asset locking"
            or manifest["gateway_client_signature_algorithm"] != "ECDSA-P256-SHA256"
            or manifest["peer_endorsement_signature_algorithm"] != expected_peer
            or manifest["timer_definition"] != FUNDING_TIMER_DEFINITION
            or manifest["independent_ledger_readback_verified"] is not True
            or manifest["transaction_specific_block_endorsement_verification"] is not True):
        raise ValueError("E7 funding manifest methodology/provenance mismatch")
    try:
        started = int(manifest["start_monotonic_ns"])
        ended = int(manifest["end_monotonic_ns"])
        duration = int(manifest["funding_duration_ns"])
    except (TypeError, ValueError) as error:
        raise ValueError("E7 funding duration is malformed") from error
    if (started < 0 or ended <= started or duration <= 0
            or duration != ended - started
            or any(isinstance(manifest[field], bool) for field in
                   ("start_monotonic_ns", "end_monotonic_ns", "funding_duration_ns"))):
        raise ValueError("E7 funding monotonic timing evidence is inconsistent")
    completion = manifest["completion"]
    if (not isinstance(completion, dict) or not completion.get("transaction_id")
            or completion.get("committed") is not True or completion.get("verified") is not True
            or completion.get("validation_status") != "VALID"
            or completion.get("validation_code") != 0
            or not isinstance(completion.get("block_number"), int)
            or completion.get("block_number") < 0):
        raise ValueError("E7 funding Fabric completion is invalid")
    validate_fabric_cpu_isolation(manifest["fabric_cpu_isolation"])
    environment = manifest["scientific_environment"]
    if (not isinstance(environment, dict) or environment.get("passed") is not True
            or environment.get("process_affinity") != EXPECTED_AFFINITY
            or set(environment.get("governor", [])) != {"performance"}
            or set(environment.get("energy_performance_preference", [])) != {"performance"}
            or set(environment.get("scaling_min_freq_khz", [])) != {3200000}
            or set(environment.get("scaling_max_freq_khz", [])) != {3200000}
            or environment.get("boost_enabled") is not False
            or environment.get("ac_online") is not True):
        raise ValueError("E7 funding scientific controls did not pass")
    versions = manifest["runtime_versions"]
    if (not isinstance(versions, dict) or not versions.get("go") or not versions.get("python")
            or versions.get("fabric") != "2.5.16"
            or versions.get("fabric_gateway_go") != "1.7.1"
            or versions.get("liboqs") != "0.15.0"
            or versions.get("liboqs_go") != "v0.15.0"):
        raise ValueError("E7 funding runtime provenance is invalid")
    _validate_git_code_map(
        project_root, manifest["git_commit"], manifest["code_config_sha256"],
        REQUIRED_FUNDING_CODE_CONFIG_PATHS,
    )
    raw_files = manifest["raw_files"]
    if not isinstance(raw_files, dict) or set(raw_files) != FUNDING_RAW_FILES:
        raise ValueError("E7 funding raw evidence inventory is invalid")
    for filename, recorded in raw_files.items():
        if recorded != _file_evidence(run / filename):
            raise ValueError("E7 funding raw evidence hash/size mismatch")
    if {path.name for path in run.iterdir()} != FUNDING_RAW_FILES | {"manifest.json"}:
        raise ValueError("E7 funding directory contains missing or extra files")

    try:
        with (run / "funding.csv").open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != FUNDING_CSV_FIELDS:
                raise ValueError("E7 funding CSV schema mismatch")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 funding CSV is malformed") from error
    if len(rows) != 1:
        raise ValueError("E7 funding evidence must contain exactly one transaction")
    row = rows[0]
    if (row["target_config"] != target_config or row["run_id"] != "1"
            or row["start_monotonic_ns"] != str(started)
            or row["end_monotonic_ns"] != str(ended)
            or row["funding_duration_ns"] != str(duration)
            or row["transaction_id"] != completion["transaction_id"]
            or row["block_number"] != str(completion["block_number"])
            or row["validation_status"] != "VALID" or row["validation_code"] != "0"
            or row["committed"] != "true" or row["verified"] != "true"
            or row["readback_verified"] != "true"
            or row["payload_sha256"] != manifest["commitment_payload_sha256"]):
        raise ValueError("E7 funding CSV disagrees with manifest")
    readback = _load_json(run / "ledger_readback.json", "E7 funding readback")
    if (readback != {"key": manifest["commitment_key"],
                     "payload_sha256": manifest["commitment_payload_sha256"],
                     "readback_sha256": manifest["commitment_payload_sha256"],
                     "matched": True}):
        raise ValueError("E7 funding independent ledger readback mismatch")
    audit = _load_json(run / "transaction_audit.json", "E7 funding transaction audit")
    _validate_funding_audit(audit, manifest, run / "funding_block.pb")
    return manifest, duration


def load_funding_runs(funding_root: Path, project_root: Path) -> dict[str, int]:
    expected = {"classical_l2", "layer_aware_l2"}
    if not funding_root.is_dir() or {path.name for path in funding_root.iterdir()} != expected:
        raise ValueError("E7 funding root must contain exactly both L2 funding conditions")
    durations, manifests = {}, []
    for config in sorted(expected):
        parent = funding_root / config
        if {path.name for path in parent.iterdir()} != {"run_1"}:
            raise ValueError("E7 funding evidence has duplicate or unexpected runs")
        manifest, duration = validate_funding_manifest(parent / "run_1", config, project_root)
        manifests.append(manifest)
        durations[config] = duration
    if len({manifest["git_commit"] for manifest in manifests}) != 1:
        raise ValueError("E7 funding manifests have mixed Git commits")
    if manifests[0]["code_config_sha256"] != manifests[1]["code_config_sha256"]:
        raise ValueError("E7 funding manifests have inconsistent code/config hashes")
    return durations


def validate_trace(trace_path: Path, provenance_path: Path) -> int:
    try:
        trace_content = trace_path.read_bytes()
        provenance = json.loads(provenance_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("E7 trace provenance is missing or malformed") from error
    if not isinstance(provenance, dict) or provenance.get("schema") != PROVENANCE_SCHEMA:
        raise ValueError("E7 trace provenance schema mismatch")
    dataset = provenance.get("dataset")
    if dataset != {
        "name": DATASET_NAME,
        "version": DATASET_VERSION,
        "primary_source": DATASET_PRIMARY_SOURCE,
        "resolution": DATASET_RESOLUTION,
        "licence": DATASET_LICENCE,
        "source_filename": UPSTREAM_FILENAME,
    }:
        raise ValueError("E7 dataset provenance mismatch")
    selected = provenance.get("selected_source_columns")
    if (
        not isinstance(selected, dict)
        or set(selected)
        != {"timestamp", "photovoltaic_generation", "grid_import", "grid_export"}
        or any(not isinstance(value, str) or not value for value in selected.values())
    ):
        raise ValueError("E7 selected source-column provenance is missing")
    if provenance.get("interpolation") != {
        "column": INTERPOLATION_COLUMN,
        "policy": "selected E7 source feeds must be non-interpolated",
        "selected_feed_interpolated_row_count": 0,
    }:
        raise ValueError("E7 interpolation provenance mismatch")
    if provenance.get("transformation") != {
        "upstream_data_representation": UPSTREAM_REPRESENTATION,
        "method": TRANSFORMATION,
        "interval_timestamp_convention": INTERVAL_TIMESTAMP_CONVENTION,
        "monotonic_counter_reset_policy": COUNTER_POLICY,
    }:
        raise ValueError("E7 cumulative-to-interval transformation mismatch")
    upstream_sha = provenance.get("upstream_sha256")
    if not isinstance(upstream_sha, str) or not SHA256_PATTERN.fullmatch(upstream_sha):
        raise ValueError("E7 upstream SHA-256 provenance is invalid")
    if provenance.get("prepared_trace_sha256") != sha256(trace_content):
        raise ValueError("E7 prepared trace SHA-256 mismatch")

    try:
        reader = csv.DictReader(io.StringIO(trace_content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 prepared trace has duplicate CSV header names")
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 prepared trace is malformed") from error
    if fields != TRACE_FIELDS:
        raise ValueError("E7 prepared trace schema mismatch")
    if len(rows) < MINIMUM_TRACE_ROWS:
        raise ValueError("E7 prepared trace is too short")
    timestamps = []
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in TRACE_FIELDS):
            raise ValueError("malformed E7 prepared trace row")
        timestamp = parse_timestamp(row["timestamp"])
        pv = parse_energy(row["pv_kwh"], "PV")
        demand = parse_energy(row["demand_kwh"], "demand")
        try:
            net = float(row["net_kwh"])
        except (TypeError, ValueError) as error:
            raise ValueError("malformed E7 prepared trace net_kwh") from error
        if not math.isfinite(net) or not math.isclose(
            net, pv - demand, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("E7 prepared trace net_kwh mismatch")
        timestamps.append(timestamp)
    if any(right - left != INTERVAL for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("E7 prepared trace timestamps are not 15 minutes apart")
    selection = provenance.get("selection")
    expected_selection = {
        "policy": SELECTION_POLICY,
        "source_rows_required": len(rows) + 1,
        "prepared_interval_rows": len(rows),
        "warmup_rows": WARMUP_SAMPLES,
        "measured_rows": len(rows) - WARMUP_SAMPLES,
        "source_timestamp_start": (timestamps[0] - INTERVAL).isoformat(),
        "source_timestamp_end": timestamps[-1].isoformat(),
        "prepared_interval_timestamp_start": timestamps[0].isoformat(),
        "prepared_interval_timestamp_end": timestamps[-1].isoformat(),
    }
    if selection != expected_selection:
        raise ValueError("E7 trace selection provenance mismatch")
    return len(rows)


def load_evidence(evidence_path: Path) -> list[dict[str, str]]:
    try:
        content = evidence_path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 evidence has duplicate CSV header names")
        rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 evidence is missing or malformed") from error
    if fields != FINAL_FIELDS:
        raise ValueError("E7 evidence schema mismatch")
    return rows


def load_raw_evidence(evidence_path: Path) -> list[dict[str, str]]:
    try:
        content = evidence_path.read_bytes()
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if len(fields) != len(set(fields)):
            raise ValueError("E7 raw evidence has duplicate CSV header names")
        rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("E7 raw evidence is missing or malformed") from error
    if fields != RAW_FIELDS:
        raise ValueError("E7 raw evidence schema mismatch")
    return rows


def evidence_fields(evidence_path: Path) -> tuple[str, ...]:
    try:
        with evidence_path.open(newline="", encoding="utf-8") as source:
            fields = tuple(csv.reader(source).__next__())
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration) as error:
        raise ValueError("E7 evidence is missing or malformed") from error
    if len(fields) != len(set(fields)):
        raise ValueError("E7 evidence has duplicate CSV header names")
    return fields


def _identity_field(name: str, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return f"{name}:{len(encoded)}:".encode("ascii") + encoded + b"\n"


def _expected_trade(trace_sha: str, row: dict[str, str], trace_row_idx: int):
    try:
        net = Decimal(row["net_kwh"])
    except (InvalidOperation, TypeError, KeyError) as error:
        raise ValueError("malformed E7 trace net_kwh") from error
    if not net.is_finite():
        raise ValueError("non-finite E7 trace net_kwh")
    if net > 0:
        direction, amount, payer, payee = (
            "prosumer_sale", row["net_kwh"],
            "external-market-grid-counterparty", "residential3",
        )
    elif net < 0:
        direction, amount, payer, payee = (
            "prosumer_purchase", row["net_kwh"].removeprefix("-"),
            "residential3", "external-market-grid-counterparty",
        )
    else:
        direction, amount, payer, payee = "balanced_no_transfer", "0", "none", "none"
    values = (
        ("schema_version", "e7-v1"), ("trace_sha256", trace_sha),
        ("run_id", "1"), ("trace_row_idx", str(trace_row_idx)),
        ("timestamp", row["timestamp"]), ("direction", direction),
        ("amount_kwh", amount), ("payer", payer), ("payee", payee),
    )
    preimage = b"".join(_identity_field(name, value) for name, value in values)
    return hashlib.sha256(preimage).hexdigest(), direction, amount, payer, payee


def _expected_logical_settlement(
    trace_sha: str,
    row: dict[str, str],
    trace_row_idx: int,
    phase: str,
    sample_idx: int,
    trade_id: str,
    direction: str,
    amount: str,
    payer: str,
    payee: str,
) -> bytes:
    values = (
        ("schema_version", "e7-v1"), ("trace_sha256", trace_sha),
        ("run_id", "1"), ("phase", phase),
        ("trace_row_idx", str(trace_row_idx)), ("sample_idx", str(sample_idx)),
        ("timestamp", row["timestamp"]), ("pv_kwh", row["pv_kwh"]),
        ("demand_kwh", row["demand_kwh"]), ("net_kwh", row["net_kwh"]),
        ("direction", direction), ("amount_kwh", amount), ("payer", payer),
        ("payee", payee), ("logical_trade_id", trade_id),
    )
    return b"".join(_identity_field(name, value) for name, value in values)


def _trace_identity_rows(trace_path: Path):
    content = trace_path.read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
    fields = tuple(reader.fieldnames or ())
    if fields != TRACE_FIELDS or len(fields) != len(set(fields)):
        raise ValueError("E7 prepared trace schema mismatch")
    rows = list(reader)
    return sha256(content), rows


def validate_raw_rows(rows: list[dict[str, str]], trace_path: Path) -> list[dict[str, object]]:
    trace_sha, trace_rows = _trace_identity_rows(trace_path)
    if len(trace_rows) != MINIMUM_TRACE_ROWS:
        raise ValueError("E7 raw finalization requires exactly 1100 trace rows")
    normalized = []
    seen = set()
    populations = {name: set() for name in CONFIG_NAMES}
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in RAW_FIELDS):
            raise ValueError("malformed E7 raw evidence row")
        config = row["config"]
        get_configuration(config)
        try:
            trace_idx = int(row["trace_row_idx"])
            phase_idx = int(row["phase_idx"])
            sample_idx = int(row["sample_idx"])
            started = int(row["start_monotonic_ns"])
            ended = int(row["end_monotonic_ns"])
            latency = float(row["latency_ms"])
        except (TypeError, ValueError) as error:
            raise ValueError("malformed E7 raw evidence numeric field") from error
        if not 0 <= trace_idx < len(trace_rows):
            raise ValueError("E7 raw trace_row_idx is out of range")
        expected_phase = "warmup" if trace_idx < WARMUP_SAMPLES else "measured"
        expected_idx = trace_idx if expected_phase == "warmup" else trace_idx - WARMUP_SAMPLES
        expected_sample = -1 if expected_phase == "warmup" else expected_idx
        if row["run_id"] != "1" or row["phase"] != expected_phase or phase_idx != expected_idx or sample_idx != expected_sample:
            raise ValueError("E7 raw phase/run/sample mapping mismatch")
        trace_row = trace_rows[trace_idx]
        row_bytes = (
            ",".join(trace_row[field] for field in TRACE_FIELDS) + "\n"
        ).encode("utf-8")
        trade_id, direction, amount, payer, payee = _expected_trade(trace_sha, trace_row, trace_idx)
        if (
            row["trace_timestamp"] != trace_row["timestamp"]
            or row["trace_row_sha256"] != sha256(row_bytes)
            or row["logical_trade_id"] != trade_id
            or row["direction"] != direction
            or row["amount_kwh"] != amount
            or row["payer"] != payer
            or row["payee"] != payee
        ):
            raise ValueError("E7 raw logical trade identity mismatch")
        if row["scientific"] != "true" or row["success"] != "true" or row["error"]:
            raise ValueError("E7 raw evidence is not a successful scientific sample")
        if ended <= started or not math.isfinite(latency) or latency <= 0:
            raise ValueError("E7 raw timing is invalid")
        if not math.isclose(latency, (ended - started) / 1_000_000, rel_tol=0, abs_tol=5e-10):
            raise ValueError("E7 raw latency disagrees with monotonic markers")
        key = (config, row["run_id"], trace_idx)
        if key in seen:
            raise ValueError("duplicate E7 raw sample")
        seen.add(key)
        populations[config].add(trace_idx)
        if config.endswith("_l2"):
            if row["htlc_id"] != "e7-v1:" + trade_id or row["authorization_verified"] != "true" or row["receiver_verified"] != "true":
                raise ValueError("E7 L2 verification evidence mismatch")
        else:
            if (row["committed"] != "true" or row["verified"] != "true"
                    or not row["transaction_id"] or row["validation_status"] != "VALID"
                    or row["validation_code"] != "0"
                    or not SHA256_PATTERN.fullmatch(row["payload_sha256"])):
                raise ValueError("E7 Fabric commit evidence mismatch")
        if config == "sets_reimpl":
            common = _expected_logical_settlement(
                trace_sha, trace_row, trace_idx, expected_phase, expected_sample,
                trade_id, direction, amount, payer, payee,
            )
            if (
                row["sets_ecdsa_algorithm"] != SETS_ECDSA_ALGORITHM
                or row["sets_signed_preimage_sha256"] != sha256(common)
                or not SHA256_PATTERN.fullmatch(row["sets_public_key_sha256"])
                or not SHA256_PATTERN.fullmatch(row["sets_signature_sha256"])
                or row["sets_signature_verified"] != "true"
                or row["sets_controller_eligible"] != "true"
                or row["sets_controller_balance_sufficient"] != "true"
            ):
                raise ValueError("E7 SETS ECDSA/controller evidence mismatch")
        if expected_phase == "measured":
            normalized.append({
                "config": config, "run_id": row["run_id"],
                "sample_idx": sample_idx, "latency_ms": row["latency_ms"],
            })
    expected = set(range(len(trace_rows)))
    if any(populations[name] != expected for name in CONFIG_NAMES):
        raise ValueError("E7 raw trace populations differ or are incomplete")
    return validate_rows(normalized, len(trace_rows))


def validate_rows(rows: list[dict[str, str]], trace_row_count: int) -> list[dict[str, object]]:
    if not rows:
        raise ValueError("E7 evidence is empty")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    populations: dict[str, set[tuple[str, int]]] = {
        name: set() for name in CONFIG_NAMES
    }
    counts: dict[str, int] = {name: 0 for name in CONFIG_NAMES}
    for row in rows:
        if None in row or any(field not in row or row[field] is None for field in SETTLEMENT_FIELDS):
            raise ValueError("malformed E7 evidence row")
        config = row["config"]
        get_configuration(config)
        run_id = row["run_id"].strip()
        if not run_id:
            raise ValueError("E7 run_id must be non-empty")
        try:
            sample_idx = int(row["sample_idx"])
            latency_ms = Decimal(str(row["latency_ms"]))
        except (TypeError, ValueError, InvalidOperation) as error:
            raise ValueError("E7 sample_idx/latency_ms is malformed") from error
        measured_trace_rows = trace_row_count - WARMUP_SAMPLES
        if sample_idx < 0 or sample_idx >= measured_trace_rows:
            raise ValueError("E7 sample_idx is outside the measured trace population")
        if not latency_ms.is_finite() or latency_ms <= 0:
            raise ValueError("E7 latency_ms must be finite and positive")
        key = (config, run_id, sample_idx)
        if key in seen:
            raise ValueError("duplicate E7 (config, run_id, sample_idx)")
        seen.add(key)
        populations[config].add((run_id, sample_idx))
        counts[config] += 1
        normalized_row = {
                "config": config,
                "run_id": run_id,
                "sample_idx": sample_idx,
                "latency_ms": str(row["latency_ms"]),
            }
        if "amortized_setup_ms" in row:
            try:
                setup = Decimal(str(row["amortized_setup_ms"]))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ValueError("E7 amortized_setup_ms is malformed") from error
            if not setup.is_finite() or setup < 0:
                raise ValueError("E7 amortized_setup_ms must be finite and nonnegative")
            normalized_row["amortized_setup_ms"] = str(row["amortized_setup_ms"])
        normalized.append(normalized_row)
    if any(count < MINIMUM_MEASURED_SAMPLES for count in counts.values()):
        raise ValueError("E7 requires at least 1000 measured samples per configuration")
    first_population = populations[CONFIG_NAMES[0]]
    if any(populations[name] != first_population for name in CONFIG_NAMES[1:]):
        raise ValueError(
            "E7 measured (run_id, sample_idx) populations differ across configurations"
        )
    return normalized


def _run_sort_key(run_id: str) -> tuple[int, object]:
    return (0, int(run_id)) if run_id.isdigit() else (1, run_id)


def render_final(rows: list[dict[str, object]]) -> bytes:
    config_order = {name: index for index, name in enumerate(CONFIG_NAMES)}
    ordered = sorted(
        rows,
        key=lambda row: (
            config_order[str(row["config"])],
            _run_sort_key(str(row["run_id"])),
            int(row["sample_idx"]),
        ),
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FINAL_FIELDS)
    for row in ordered:
        writer.writerow(
            (
                row["config"],
                row["run_id"],
                row["sample_idx"],
                row["latency_ms"],
                row["amortized_setup_ms"],
            )
        )
    return output.getvalue().encode("utf-8")


def _expected_frozen_inventory() -> dict[tuple[str, str, str], set[str]]:
    inventory: dict[tuple[str, str, str], set[str]] = {}
    for config, filenames in SETTLEMENT_ARCHIVE_FILES.items():
        inventory[("settlement", config, "1")] = set(filenames)
    for config, filenames in FUNDING_ARCHIVE_FILES.items():
        inventory[("funding", config, "1")] = set(filenames)
    return inventory


def _validate_safe_relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid frozen E7 {label}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe frozen E7 {label}")
    return path


def _validate_archive_tree(root: Path, expected_files: set[str]) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("frozen E7 evidence root is missing or unsafe")
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"frozen E7 evidence contains a symlink: {relative}")
        if path.is_file():
            actual_files.add(relative)
        elif not path.is_dir():
            raise ValueError(f"frozen E7 evidence contains a special file: {relative}")
    if actual_files != expected_files | {"evidence_manifest.json"}:
        raise ValueError("frozen E7 evidence has missing or unmanifested files")


def _reject_archive_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError("frozen E7 evidence root must not be a symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(
                f"frozen E7 evidence contains a symlink: {path.relative_to(root)}"
            )


def _load_frozen_manifest(root: Path) -> tuple[dict, list[dict]]:
    manifest_path = root / "evidence_manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("frozen E7 evidence manifest must not be a symlink")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("frozen E7 evidence manifest is missing or malformed") from error
    expected_top = {
        "schema": FROZEN_EVIDENCE_SCHEMA,
        "experiment": "E7",
        "settlement_measurement_commit": SETTLEMENT_MEASUREMENT_COMMIT,
        "funding_measurement_commit": FUNDING_MEASUREMENT_COMMIT,
        "settlement_condition_count": 4,
        "funding_condition_count": 2,
        "source_file_count": FROZEN_SOURCE_FILE_COUNT,
        "configs": list(CONFIG_NAMES),
        "funding_configs": ["classical_l2", "layer_aware_l2"],
        "run_id": "1",
        "warmup_rows_per_settlement_config": 100,
        "measured_rows_per_settlement_config": 1000,
        "warmup_rows_total": 400,
        "measured_rows_total": 4000,
        "final_csv": {
            "path": "data/e7_endtoend.csv",
            "sha256": FINAL_CSV_SHA256,
            "data_rows": 4000,
            "schema": list(FINAL_FIELDS),
        },
        "trace": {
            "path": "env/e7/trace/opsd_household_15min_e7.csv",
            "sha256": EXPECTED_TRACE_SHA256,
            "provenance_path": "env/e7/trace/provenance.json",
            "provenance_sha256": EXPECTED_TRACE_PROVENANCE_SHA256,
        },
        "funding": {
            config: {
                "duration_ns": FUNDING_DURATIONS_NS[config],
                "transaction_id": FUNDING_TRANSACTION_IDS[config],
            }
            for config in ("classical_l2", "layer_aware_l2")
        },
        "compression": {
            "format": "gzip",
            "level": 9,
            "deterministic_header": True,
            "command": "LC_ALL=C gzip -9 -n",
            "original_sha256_semantics": "SHA-256 of decompressed/original scientific evidence bytes",
            "tracked_sha256_semantics": "SHA-256 of tracked gzip container bytes",
        },
    }
    required_keys = set(expected_top) | {"entries"}
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise ValueError("frozen E7 evidence manifest key set mismatch")
    for key, value in expected_top.items():
        if manifest.get(key) != value:
            raise ValueError(f"frozen E7 evidence manifest {key} mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != FROZEN_SOURCE_FILE_COUNT:
        raise ValueError("frozen E7 evidence manifest must contain exactly 28 entries")
    return manifest, entries


@contextmanager
def extracted_frozen_evidence(root: Path):
    """Validate and reconstruct the two original E7 evidence trees temporarily."""
    _reject_archive_symlinks(root)
    root = root.absolute()
    _, entries = _load_frozen_manifest(root)
    expected_inventory = _expected_frozen_inventory()
    observed_inventory = {key: set() for key in expected_inventory}
    logical_paths: set[str] = set()
    tracked_paths: set[str] = set()
    expected_tracked: set[str] = set()
    entry_keys = {
        "evidence_group", "config", "run_id", "original_filename",
        "original_bytes", "original_sha256", "tracked_filename",
        "tracked_bytes", "tracked_sha256", "compression",
    }
    ordered_keys: list[tuple[str, str, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="e7-frozen-") as temporary_name:
        temporary = Path(temporary_name)
        settlement_root = temporary / "settlement"
        funding_root = temporary / "funding"
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != entry_keys:
                raise ValueError("frozen E7 evidence entry schema mismatch")
            group, config, run_id = (
                entry.get("evidence_group"), entry.get("config"), entry.get("run_id")
            )
            key = (str(group), str(config), str(run_id))
            if key not in expected_inventory:
                raise ValueError("frozen E7 evidence group/config/run identity mismatch")
            original_path = _validate_safe_relative_path(
                entry.get("original_filename"), "original filename"
            )
            if len(original_path.parts) != 1:
                raise ValueError("frozen E7 original filename must be a basename")
            filename = original_path.name
            if filename not in expected_inventory[key]:
                raise ValueError("frozen E7 evidence filename is unexpected")
            logical = f"{group}/{config}/run_{run_id}/{filename}"
            expected_name = f"{logical}.gz"
            tracked = _validate_safe_relative_path(entry.get("tracked_filename"), "tracked filename")
            if tracked.as_posix() != expected_name:
                raise ValueError("frozen E7 tracked filename/classification mismatch")
            if logical in logical_paths or expected_name in tracked_paths:
                raise ValueError("duplicate frozen E7 logical or tracked path")
            if entry.get("compression") != "gzip":
                raise ValueError("frozen E7 entry compression mismatch")
            original_sha = entry.get("original_sha256")
            tracked_sha = entry.get("tracked_sha256")
            if (not SHA256_PATTERN.fullmatch(str(original_sha))
                    or not SHA256_PATTERN.fullmatch(str(tracked_sha))):
                raise ValueError("frozen E7 evidence digest is invalid")
            tracked_file = root.joinpath(*tracked.parts)
            if tracked_file.is_symlink():
                raise ValueError("frozen E7 tracked evidence must not be a symlink")
            try:
                compressed = tracked_file.read_bytes()
            except OSError as error:
                raise ValueError(f"missing frozen E7 tracked file {expected_name}") from error
            if len(compressed) != entry.get("tracked_bytes") or sha256(compressed) != tracked_sha:
                raise ValueError("frozen E7 compressed size/hash mismatch")
            try:
                original = gzip.decompress(compressed)
            except (OSError, EOFError) as error:
                raise ValueError("frozen E7 gzip payload is invalid or truncated") from error
            if len(original) != entry.get("original_bytes") or sha256(original) != original_sha:
                raise ValueError("frozen E7 original size/hash mismatch")
            destination = temporary / logical
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original)
            logical_paths.add(logical)
            tracked_paths.add(expected_name)
            expected_tracked.add(expected_name)
            observed_inventory[key].add(filename)
            ordered_keys.append((*key, filename))
        if ordered_keys != sorted(ordered_keys):
            raise ValueError("frozen E7 evidence entries are not deterministically ordered")
        if observed_inventory != expected_inventory:
            raise ValueError("frozen E7 evidence inventory is incomplete")
        _validate_archive_tree(root, expected_tracked)
        yield settlement_root, funding_root


def finalize_frozen(
    frozen_root: Path,
    trace_path: Path,
    provenance_path: Path,
    output_path: Path,
    project_root: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.frozen-", suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(descriptor)
    candidate = Path(candidate_name)
    try:
        with extracted_frozen_evidence(frozen_root) as (settlement_root, funding_root):
            finalize(
                settlement_root, funding_root, trace_path, provenance_path,
                candidate, project_root,
            )
        if sha256(candidate.read_bytes()) != FINAL_CSV_SHA256:
            raise ValueError("frozen E7 final CSV SHA-256 mismatch")
        os.replace(candidate, output_path)
    finally:
        candidate.unlink(missing_ok=True)


def finalize(
    evidence_root: Path,
    funding_root: Path,
    trace_path: Path,
    provenance_path: Path,
    output_path: Path,
    project_root: Path,
) -> None:
    validate_trace(trace_path, provenance_path)
    rows, _ = load_scientific_runs(
        evidence_root, trace_path, provenance_path, project_root
    )
    normalized = validate_raw_rows(rows, trace_path)
    durations = load_funding_runs(funding_root, project_root)
    zero = "0.000000000"
    # nanoseconds / 1e6 converts to milliseconds; dividing that one-time
    # latency by exactly 1000 measured settlements is therefore ns / 1e9.
    amortized = {
        config: format(Decimal(duration) / Decimal(1_000_000_000), ".9f")
        for config, duration in durations.items()
    }
    for row in normalized:
        row["amortized_setup_ms"] = amortized.get(str(row["config"]), zero)
    if len(normalized) != 4000:
        raise ValueError("E7 final output requires exactly 4000 measured rows")
    content = render_final(normalized)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--evidence-root", type=Path)
    sources.add_argument("--frozen-evidence-root", type=Path)
    parser.add_argument("--funding-evidence-root", type=Path)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--trace-provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.frozen_evidence_root is not None:
        if arguments.funding_evidence_root is not None:
            parser.error("--funding-evidence-root is not used with --frozen-evidence-root")
        finalize_frozen(
            arguments.frozen_evidence_root, arguments.trace,
            arguments.trace_provenance, arguments.output, arguments.project_root,
        )
    else:
        if arguments.funding_evidence_root is None:
            parser.error("plain evidence requires --funding-evidence-root")
        finalize(
            arguments.evidence_root,
            arguments.funding_evidence_root,
            arguments.trace,
            arguments.trace_provenance,
            arguments.output,
            arguments.project_root,
        )


if __name__ == "__main__":
    main()
