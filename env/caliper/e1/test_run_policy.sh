#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/run_policy.sh"

grep -Fq 'validate_e1_run_policy' "$SCRIPT_DIR/run_e1.sh"
grep -Fq 'ensure_e1_namespace_available' "$SCRIPT_DIR/run_e1.sh"

(
    cd "$SCRIPT_DIR"
    node <<'NODE'
const fs = require('fs');
const yaml = require('js-yaml');

const expected = {
    'benchmark_ecdsa_sustained_222.yaml': [['warmup', 20, 50], ['sustained-222-tps', 60, 222]],
    'benchmark_ecdsa_sustained_223.yaml': [['warmup', 20, 50], ['sustained-223-tps', 60, 223]],
    'benchmark_ecdsa_sustained_224.yaml': [['warmup', 20, 50], ['sustained-224-tps', 60, 224]],
    'benchmark_ecdsa_sustained_227.yaml': [['warmup', 20, 50], ['sustained-227-tps', 60, 227]],
    'benchmark_ecdsa_sustained_228.yaml': [['warmup', 20, 50], ['sustained-228-tps', 60, 228]],
    'benchmark_ecdsa_sustained_230.yaml': [['warmup', 20, 50], ['sustained-230-tps', 60, 230]],
    'benchmark_ecdsa_sustained_237.yaml': [['warmup', 20, 50], ['sustained-237-tps', 60, 237]],
    'benchmark_ecdsa_sustained_250.yaml': [['warmup', 20, 50], ['sustained-250-tps', 60, 250]],
    'benchmark_ml_dsa_sustained_200.yaml': [['warmup', 20, 50], ['sustained-200-tps', 60, 200]],
    'benchmark_sphincs_blockutil_54.yaml': [['warmup', 20, 1], ['blockutil-54-tps', 60, 54]],
};

for (const [filename, wanted] of Object.entries(expected)) {
    const parsed = yaml.load(fs.readFileSync(filename, 'utf8'));
    const observed = parsed.test.rounds.map(round => [
        round.label,
        round.txDuration,
        round.rateControl.opts.tps,
    ]);
    if (JSON.stringify(observed) !== JSON.stringify(wanted)) {
        throw new Error(`${filename}: unexpected labels, durations, or rates`);
    }
    for (const round of parsed.test.rounds) {
        if (round.rateControl.type !== 'fixed-rate' ||
            round.workload.module !== 'workload/set.js' ||
            round.workload.arguments.contractId !== 'simplekv' ||
            round.workload.arguments.roundLabel !== round.label) {
            throw new Error(`${filename}: workload semantics changed`);
        }
    }
}
NODE
)

expect_rejected() {
    if "$@" >/dev/null 2>&1; then
        echo "ERROR: Expected policy rejection: $*"
        exit 1
    fi
}

validate_e1_run_policy ecdsa benchmark.yaml fixed-profile ""
validate_e1_run_policy sphincs benchmark_sphincs_sweep.yaml sweep sphincs-lowrate-v1
validate_e1_run_policy ecdsa benchmark_tx_evidence_50.yaml evidence tx-evidence-50-v1
validate_e1_run_policy ml-dsa-44 benchmark_tx_evidence_50.yaml evidence tx-evidence-50-v1
validate_e1_run_policy ml-dsa-65 benchmark_tx_evidence_50.yaml evidence tx-evidence-50-v1
validate_e1_run_policy ecdsa benchmark_blockutil.yaml diagnostic blockutil-300
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_222.yaml sustainability sustained-222-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_223.yaml sustainability sustained-223-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_224.yaml sustainability sustained-224-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_227.yaml sustainability sustained-227-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_228.yaml sustainability sustained-228-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_230.yaml sustainability sustained-230-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_237.yaml sustainability sustained-237-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_250.yaml sustainability sustained-250-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_200.yaml sustainability sustained-200-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_200.yaml sustainability sustained-200-v1
validate_e1_run_policy sphincs benchmark_sphincs_blockutil_54.yaml diagnostic blockutil-54-v1

expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_222.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_222.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_223.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_223.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_224.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_224.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_227.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_227.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_228.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_228.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_230.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_230.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_237.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_237.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_250.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_250.yaml diagnostic bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ml_dsa_sustained_200.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_sphincs_blockutil_54.yaml diagnostic bad
expect_rejected validate_e1_run_policy sphincs benchmark_sphincs_blockutil_54.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_222.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_223.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_224.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_227.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_228.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_230.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_237.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_250.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa unreviewed.yaml diagnostic bad

TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
ensure_e1_namespace_available "$TEST_DIR" available_ecdsa
touch "$TEST_DIR/collision_ecdsa_caliper_run.log"
expect_rejected ensure_e1_namespace_available "$TEST_DIR" collision_ecdsa

echo "run policy tests passed"
