#!/usr/bin/env bash

set -euo pipefail

BENCHMARK_CPU="2,3"
BINARY="./e0bench"

if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: benchmark binary not found or not executable: $BINARY"
    exit 1
fi

benchmarks=(
    "ML-KEM-768 keygen"
    "ML-KEM-768 encaps"
    "ML-KEM-768 decaps"

    "ML-DSA-44 keygen"
    "ML-DSA-44 sign"
    "ML-DSA-44 verify"

    "ML-DSA-65 keygen"
    "ML-DSA-65 sign"
    "ML-DSA-65 verify"

    "ML-DSA-87 keygen"
    "ML-DSA-87 sign"
    "ML-DSA-87 verify"

    "Falcon-512 keygen"
    "Falcon-512 sign"
    "Falcon-512 verify"

    "SPHINCS+-SHA2-128s keygen"
    "SPHINCS+-SHA2-128s sign"
    "SPHINCS+-SHA2-128s verify"

    "ECDSA-P-256 keygen"
    "ECDSA-P-256 sign"
    "ECDSA-P-256 verify"
)

echo "========================================"
echo "E0 server benchmark batch"
echo "CPU: $BENCHMARK_CPU"
echo "GOMAXPROCS: 1"
echo "Data points: ${#benchmarks[@]}"
echo "========================================"

for entry in "${benchmarks[@]}"; do
    read -r scheme operation <<< "$entry"

    echo
    echo "----------------------------------------"
    echo "Running: server | $scheme | $operation"
    echo "Started: $(date --iso-8601=seconds)"
    echo "----------------------------------------"

    GOMAXPROCS=1 taskset -c "$BENCHMARK_CPU" \
        "$BINARY" server "$scheme" "$operation"

    echo "Finished: $(date --iso-8601=seconds)"
    sleep 5
done

echo
echo "========================================"
echo "E0 server batch completed successfully"
echo "Finished: $(date --iso-8601=seconds)"
echo "========================================"