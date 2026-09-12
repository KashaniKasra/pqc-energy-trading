#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/run_policy.sh"

grep -Fq 'validate_e1_run_policy' "$SCRIPT_DIR/run_e1.sh"
grep -Fq 'ensure_e1_namespace_available' "$SCRIPT_DIR/run_e1.sh"

if FABRIC_PQ_VERIFY_TRACE=1 "$SCRIPT_DIR/run_e1.sh" ecdsa >/dev/null 2>&1; then
    echo "ERROR: Caliper runner accepted FABRIC_PQ_VERIFY_TRACE=1"
    exit 1
fi

(
    cd "$SCRIPT_DIR"
    python3 <<'PY'
from pathlib import Path
import yaml

expected = {
    "benchmark.yaml": [
        ("warmup", 20, 50), ("50-tps", 120, 50), ("200-tps", 120, 200),
    ],
    "benchmark_blockutil.yaml": [
        ("warmup", 20, 50), ("blockutil-300-tps", 60, 300),
    ],
    "benchmark_tx_evidence_50.yaml": [
        ("warmup", 20, 50), ("50-tps", 120, 50),
    ],
    "benchmark_ecdsa_sustained_228.yaml": [
        ("warmup", 20, 50), ("sustained-228-tps", 60, 228),
    ],
    "benchmark_ecdsa_sustained_229.yaml": [
        ("warmup", 20, 50), ("sustained-229-tps", 60, 229),
    ],
    "benchmark_ml_dsa_sustained_200.yaml": [
        ("warmup", 20, 50), ("sustained-200-tps", 60, 200),
    ],
    "benchmark_ml_dsa_sustained_356.yaml": [
        ("warmup", 20, 50), ("sustained-356-tps", 60, 356),
    ],
    "benchmark_ml_dsa_sustained_357.yaml": [
        ("warmup", 20, 50), ("sustained-357-tps", 60, 357),
    ],
    "benchmark_ml_dsa_65_sustained_344.yaml": [
        ("warmup", 20, 50), ("sustained-344-tps", 60, 344),
    ],
    "benchmark_ml_dsa_65_sustained_345.yaml": [
        ("warmup", 20, 50), ("sustained-345-tps", 60, 345),
    ],
    "benchmark_sphincs_boundary_22.yaml": [
        ("warmup", 20, 1), ("sphincs-22-tps", 60, 22),
    ],
    "benchmark_sphincs_boundary_23.yaml": [
        ("warmup", 20, 1), ("sphincs-23-tps", 60, 23),
    ],
    "benchmark_sphincs_blockutil_54.yaml": [
        ("warmup", 20, 1), ("blockutil-54-tps", 60, 54),
    ],
}

for filename, wanted in expected.items():
    parsed = yaml.safe_load(Path(filename).read_text(encoding="utf-8"))
    observed = [
        (
            item["label"],
            item["txDuration"],
            item["rateControl"]["opts"]["tps"],
        )
        for item in parsed["test"]["rounds"]
    ]
    assert observed == wanted, f"{filename}: scientific rates changed"
    for item in parsed["test"]["rounds"]:
        assert item["rateControl"]["type"] == "fixed-rate"
        assert item["workload"]["module"] == "workload/set.js"
        assert item["workload"]["arguments"]["contractId"] == "simplekv"
        assert item["workload"]["arguments"]["roundLabel"] == item["label"]

sweep = yaml.safe_load(
    Path("benchmark_sphincs_sweep.yaml").read_text(encoding="utf-8")
)
assert [
    (item["label"], item["txDuration"], item["rateControl"]["opts"]["tps"])
    for item in sweep["test"]["rounds"]
] == [
    ("warmup", 20, 1),
    ("sphincs-1-tps", 60, 1),
    ("sphincs-2-tps", 60, 2),
    ("sphincs-5-tps", 60, 5),
    ("sphincs-10-tps", 60, 10),
    ("sphincs-20-tps", 60, 20),
]
PY
)

expect_rejected() {
    if "$@" >/dev/null 2>&1; then
        echo "ERROR: Expected policy rejection: $*"
        exit 1
    fi
}

validate_e1_run_policy ecdsa benchmark.yaml fixed-profile ""
validate_e1_run_policy sphincs benchmark.yaml fixed-profile ""
validate_e1_run_policy sphincs benchmark_sphincs_sweep.yaml sweep sphincs-lowrate-v1
validate_e1_run_policy sphincs benchmark_sphincs_boundary_22.yaml sweep sphincs-boundary-22-v1
validate_e1_run_policy sphincs benchmark_sphincs_boundary_23.yaml sweep sphincs-boundary-23-v1
for config in ecdsa ml-dsa-44 ml-dsa-65; do
    validate_e1_run_policy "$config" benchmark_tx_evidence_50.yaml evidence tx-evidence-50-v1
done
validate_e1_run_policy ecdsa benchmark_blockutil.yaml diagnostic blockutil-300
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_228.yaml sustainability sustained-228-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_229.yaml sustainability sustained-229-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_200.yaml sustainability sustained-200-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_356.yaml sustainability sustained-356-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_357.yaml sustainability sustained-357-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_344.yaml sustainability sustained-344-v2
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_345.yaml sustainability sustained-345-v1
validate_e1_run_policy sphincs benchmark_sphincs_blockutil_54.yaml diagnostic blockutil-54-v1

for profile in benchmark_ecdsa_sustained_228.yaml benchmark_ecdsa_sustained_229.yaml; do
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" sustainability bad
    expect_rejected validate_e1_run_policy ecdsa "$profile" diagnostic bad
    expect_rejected validate_e1_run_policy ecdsa "$profile" sustainability ""
done
for profile in benchmark_ml_dsa_sustained_356.yaml benchmark_ml_dsa_sustained_357.yaml; do
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" sustainability bad
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" diagnostic bad
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" sustainability ""
done
for profile in benchmark_ml_dsa_65_sustained_344.yaml benchmark_ml_dsa_65_sustained_345.yaml; do
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" sustainability bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" diagnostic bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" sustainability ""
done
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_200.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_sphincs_blockutil_54.yaml diagnostic bad
expect_rejected validate_e1_run_policy sphincs benchmark_sphincs_blockutil_54.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa unreviewed.yaml diagnostic bad

TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
ensure_e1_namespace_available "$TEST_DIR" available_ecdsa
touch "$TEST_DIR/collision_ecdsa_caliper_run.log"
expect_rejected ensure_e1_namespace_available "$TEST_DIR" collision_ecdsa

echo "run policy tests passed"
