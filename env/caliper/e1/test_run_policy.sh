#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/run_policy.sh"

grep -Fq 'validate_e1_run_policy' "$SCRIPT_DIR/run_e1.sh"
grep -Fq 'ensure_e1_namespace_available' "$SCRIPT_DIR/run_e1.sh"

if FABRIC_PQ_VERIFY_TRACE=1 "$SCRIPT_DIR/run_e1.sh" ecdsa >/dev/null 2>&1; then
    echo "ERROR: Caliper runner accepted FABRIC_PQ_VERIFY_TRACE=1"
    exit 1
fi

(
    cd "$SCRIPT_DIR"
    node <<'NODE'
const fs = require('fs');
const assert = require('node:assert/strict');
const yaml = require('js-yaml');

const expected = {
    'benchmark_ecdsa_sustained_222.yaml': [['warmup', 20, 50], ['sustained-222-tps', 60, 222]],
    'benchmark_ecdsa_sustained_223.yaml': [['warmup', 20, 50], ['sustained-223-tps', 60, 223]],
    'benchmark_ecdsa_sustained_224.yaml': [['warmup', 20, 50], ['sustained-224-tps', 60, 224]],
    'benchmark_ecdsa_sustained_227.yaml': [['warmup', 20, 50], ['sustained-227-tps', 60, 227]],
    'benchmark_ecdsa_sustained_228.yaml': [['warmup', 20, 50], ['sustained-228-tps', 60, 228]],
    'benchmark_ecdsa_sustained_229.yaml': [['warmup', 20, 50], ['sustained-229-tps', 60, 229]],
    'benchmark_ecdsa_sustained_230.yaml': [['warmup', 20, 50], ['sustained-230-tps', 60, 230]],
    'benchmark_ecdsa_sustained_237.yaml': [['warmup', 20, 50], ['sustained-237-tps', 60, 237]],
    'benchmark_ecdsa_sustained_250.yaml': [['warmup', 20, 50], ['sustained-250-tps', 60, 250]],
    'benchmark_ml_dsa_sustained_200.yaml': [['warmup', 20, 50], ['sustained-200-tps', 60, 200]],
    'benchmark_ml_dsa_sustained_300.yaml': [['warmup', 20, 50], ['sustained-300-tps', 60, 300]],
    'benchmark_ml_dsa_sustained_350.yaml': [['warmup', 20, 50], ['sustained-350-tps', 60, 350]],
    'benchmark_ml_dsa_sustained_356.yaml': [['warmup', 20, 50], ['sustained-356-tps', 60, 356]],
    'benchmark_ml_dsa_sustained_357.yaml': [['warmup', 20, 50], ['sustained-357-tps', 60, 357]],
    'benchmark_ml_dsa_sustained_358.yaml': [['warmup', 20, 50], ['sustained-358-tps', 60, 358]],
    'benchmark_ml_dsa_sustained_359.yaml': [['warmup', 20, 50], ['sustained-359-tps', 60, 359]],
    'benchmark_ml_dsa_sustained_360.yaml': [['warmup', 20, 50], ['sustained-360-tps', 60, 360]],
    'benchmark_ml_dsa_sustained_361.yaml': [['warmup', 20, 50], ['sustained-361-tps', 60, 361]],
    'benchmark_ml_dsa_sustained_362.yaml': [['warmup', 20, 50], ['sustained-362-tps', 60, 362]],
    'benchmark_ml_dsa_sustained_368.yaml': [['warmup', 20, 50], ['sustained-368-tps', 60, 368]],
    'benchmark_ml_dsa_sustained_375.yaml': [['warmup', 20, 50], ['sustained-375-tps', 60, 375]],
    'benchmark_ml_dsa_sustained_381.yaml': [['warmup', 20, 50], ['sustained-381-tps', 60, 381]],
    'benchmark_ml_dsa_sustained_387.yaml': [['warmup', 20, 50], ['sustained-387-tps', 60, 387]],
    'benchmark_ml_dsa_sustained_393.yaml': [['warmup', 20, 50], ['sustained-393-tps', 60, 393]],
    'benchmark_ml_dsa_sustained_400.yaml': [['warmup', 20, 50], ['sustained-400-tps', 60, 400]],
    'benchmark_ml_dsa_65_sustained_250.yaml': [['warmup', 20, 50], ['sustained-250-tps', 60, 250]],
    'benchmark_ml_dsa_65_sustained_300.yaml': [['warmup', 20, 50], ['sustained-300-tps', 60, 300]],
    'benchmark_ml_dsa_65_sustained_350.yaml': [['warmup', 20, 50], ['sustained-350-tps', 60, 350]],
    'benchmark_ml_dsa_65_sustained_400.yaml': [['warmup', 20, 50], ['sustained-400-tps', 60, 400]],
    'benchmark_ml_dsa_65_sustained_450.yaml': [['warmup', 20, 50], ['sustained-450-tps', 60, 450]],
    'benchmark_ml_dsa_65_sustained_306.yaml': [['warmup', 20, 50], ['sustained-306-tps', 60, 306]],
    'benchmark_ml_dsa_65_sustained_312.yaml': [['warmup', 20, 50], ['sustained-312-tps', 60, 312]],
    'benchmark_ml_dsa_65_sustained_318.yaml': [['warmup', 20, 50], ['sustained-318-tps', 60, 318]],
    'benchmark_ml_dsa_65_sustained_325.yaml': [['warmup', 20, 50], ['sustained-325-tps', 60, 325]],
    'benchmark_ml_dsa_65_sustained_331.yaml': [['warmup', 20, 50], ['sustained-331-tps', 60, 331]],
    'benchmark_ml_dsa_65_sustained_337.yaml': [['warmup', 20, 50], ['sustained-337-tps', 60, 337]],
    'benchmark_ml_dsa_65_sustained_343.yaml': [['warmup', 20, 50], ['sustained-343-tps', 60, 343]],
    'benchmark_ml_dsa_65_sustained_344.yaml': [['warmup', 20, 50], ['sustained-344-tps', 60, 344]],
    'benchmark_ml_dsa_65_sustained_345.yaml': [['warmup', 20, 50], ['sustained-345-tps', 60, 345]],
    'benchmark_ml_dsa_65_sustained_346.yaml': [['warmup', 20, 50], ['sustained-346-tps', 60, 346]],
    'benchmark_ml_dsa_65_sustained_347.yaml': [['warmup', 20, 50], ['sustained-347-tps', 60, 347]],
    'benchmark_ml_dsa_65_sustained_348.yaml': [['warmup', 20, 50], ['sustained-348-tps', 60, 348]],
    'benchmark_ml_dsa_65_sustained_349.yaml': [['warmup', 20, 50], ['sustained-349-tps', 60, 349]],
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

const midpointBase = yaml.load(fs.readFileSync('benchmark_ml_dsa_sustained_350.yaml', 'utf8'));
for (const tps of [356, 357, 358, 359, 360, 361, 362, 368, 375, 381, 387, 393]) {
    const wanted = structuredClone(midpointBase);
    wanted.test.name = `E1 ML-DSA Sustainable-Rate Probe at ${tps} TPS`;
    wanted.test.rounds[1].label = `sustained-${tps}-tps`;
    wanted.test.rounds[1].description = `Offer ${tps} TPS for 60 seconds`;
    wanted.test.rounds[1].rateControl.opts.tps = tps;
    wanted.test.rounds[1].workload.arguments.roundLabel = `sustained-${tps}-tps`;
    const observed = yaml.load(
        fs.readFileSync(`benchmark_ml_dsa_sustained_${tps}.yaml`, 'utf8')
    );
    assert.deepStrictEqual(
        observed,
        wanted,
        `benchmark_ml_dsa_sustained_${tps}.yaml changed beyond the five authorized fields`
    );
}

const mlDsa65Base = yaml.load(fs.readFileSync('benchmark_ml_dsa_sustained_200.yaml', 'utf8'));
for (const tps of [250, 300, 350, 400, 450]) {
    const wanted = structuredClone(mlDsa65Base);
    wanted.test.name = `E1 ML-DSA-65 Sustainable-Rate Probe at ${tps} TPS`;
    wanted.test.rounds[1].label = `sustained-${tps}-tps`;
    wanted.test.rounds[1].description = `Offer ${tps} TPS for 60 seconds`;
    wanted.test.rounds[1].rateControl.opts.tps = tps;
    wanted.test.rounds[1].workload.arguments.roundLabel = `sustained-${tps}-tps`;
    const observed = yaml.load(
        fs.readFileSync(`benchmark_ml_dsa_65_sustained_${tps}.yaml`, 'utf8')
    );
    assert.deepStrictEqual(
        observed,
        wanted,
        `benchmark_ml_dsa_65_sustained_${tps}.yaml changed beyond the five authorized fields`
    );
}

for (const tps of [306, 312, 318, 325, 331, 337, 343, 344, 345, 346, 347, 348, 349]) {
    const wanted = structuredClone(mlDsa65Base);
    wanted.test.name = `E1 ML-DSA-65 Sustainable-Rate Probe at ${tps} TPS`;
    wanted.test.rounds[1].label = `sustained-${tps}-tps`;
    wanted.test.rounds[1].description = `Offer ${tps} TPS for 60 seconds`;
    wanted.test.rounds[1].rateControl.opts.tps = tps;
    wanted.test.rounds[1].workload.arguments.roundLabel = `sustained-${tps}-tps`;
    const observed = yaml.load(
        fs.readFileSync(`benchmark_ml_dsa_65_sustained_${tps}.yaml`, 'utf8')
    );
    assert.deepStrictEqual(
        observed,
        wanted,
        `benchmark_ml_dsa_65_sustained_${tps}.yaml changed beyond the five authorized fields`
    );
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
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_229.yaml sustainability sustained-229-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_230.yaml sustainability sustained-230-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_237.yaml sustainability sustained-237-v1
validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_250.yaml sustainability sustained-250-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_200.yaml sustainability sustained-200-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_200.yaml sustainability sustained-200-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_300.yaml sustainability sustained-300-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_350.yaml sustainability sustained-350-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_356.yaml sustainability sustained-356-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_357.yaml sustainability sustained-357-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_358.yaml sustainability sustained-358-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_359.yaml sustainability sustained-359-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_360.yaml sustainability sustained-360-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_361.yaml sustainability sustained-361-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_362.yaml sustainability sustained-362-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_368.yaml sustainability sustained-368-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_375.yaml sustainability sustained-375-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_381.yaml sustainability sustained-381-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_387.yaml sustainability sustained-387-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_393.yaml sustainability sustained-393-v1
validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_400.yaml sustainability sustained-400-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_250.yaml sustainability sustained-250-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_300.yaml sustainability sustained-300-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_350.yaml sustainability sustained-350-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_400.yaml sustainability sustained-400-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_450.yaml sustainability sustained-450-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_306.yaml sustainability sustained-306-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_312.yaml sustainability sustained-312-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_318.yaml sustainability sustained-318-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_325.yaml sustainability sustained-325-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_331.yaml sustainability sustained-331-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_337.yaml sustainability sustained-337-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_343.yaml sustainability sustained-343-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_344.yaml sustainability sustained-344-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_345.yaml sustainability sustained-345-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_346.yaml sustainability sustained-346-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_347.yaml sustainability sustained-347-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_348.yaml sustainability sustained-348-v1
validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_65_sustained_349.yaml sustainability sustained-349-v1
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
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_229.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_229.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_230.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_230.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_237.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_237.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ecdsa_sustained_250.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_250.yaml diagnostic bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ml_dsa_sustained_200.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_300.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_300.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_300.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_350.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_350.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_350.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_356.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_356.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_356.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_357.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_357.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_357.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_358.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_358.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_358.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_359.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_359.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_359.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_360.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_360.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_360.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_361.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_361.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_361.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_362.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_362.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_362.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_368.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_368.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_368.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_375.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_375.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_375.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_381.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_381.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_381.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_387.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_387.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_387.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_393.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_393.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_393.yaml sustainability ""
expect_rejected validate_e1_run_policy ml-dsa-65 benchmark_ml_dsa_sustained_400.yaml sustainability bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_400.yaml diagnostic bad
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_ml_dsa_sustained_400.yaml sustainability ""
for tps in 250 300 350 400 450; do
    profile="benchmark_ml_dsa_65_sustained_${tps}.yaml"
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" sustainability bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" diagnostic bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" sustainability ""
done
for tps in 306 312 318 325 331 337 343 344 345 346 347 348 349; do
    profile="benchmark_ml_dsa_65_sustained_${tps}.yaml"
    expect_rejected validate_e1_run_policy ml-dsa-44 "$profile" sustainability bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" diagnostic bad
    expect_rejected validate_e1_run_policy ml-dsa-65 "$profile" sustainability ""
done
expect_rejected validate_e1_run_policy ml-dsa-44 benchmark_sphincs_blockutil_54.yaml diagnostic bad
expect_rejected validate_e1_run_policy sphincs benchmark_sphincs_blockutil_54.yaml sustainability bad
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_222.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_223.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_224.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_227.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_228.yaml sustainability ""
expect_rejected validate_e1_run_policy ecdsa benchmark_ecdsa_sustained_229.yaml sustainability ""
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
