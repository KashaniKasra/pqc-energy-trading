#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

CPUSET="2-15"
TARGET_FREQ_KHZ=3201000

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 ecdsa"
    exit 1
fi

CONFIG="$1"

if [[ "$CONFIG" != "ecdsa" ]]; then
    echo "ERROR: PQ Fabric identity configurations are not implemented yet."
    echo "Currently supported E1 configuration: ecdsa"
    exit 1
fi

cd "$SCRIPT_DIR"

echo "[E1] Configuration: $CONFIG"
echo "[E1] Fixed CPU set: $CPUSET"

echo "[E1] Verifying CPU measurement state..."

PSTATE="$(cat /sys/devices/system/cpu/amd_pstate/status)"
GOVERNOR="$(cat /sys/devices/system/cpu/cpu2/cpufreq/scaling_governor)"
BOOST="$(cat /sys/devices/system/cpu/cpufreq/boost)"

if [[ "$PSTATE" != "passive" ]]; then
    echo "ERROR: amd_pstate must be passive. Found: $PSTATE"
    echo "Run: $SCRIPT_DIR/setup_cpu_e1.sh"
    exit 1
fi

if [[ "$GOVERNOR" != "performance" ]]; then
    echo "ERROR: CPU governor must be performance. Found: $GOVERNOR"
    echo "Run: $SCRIPT_DIR/setup_cpu_e1.sh"
    exit 1
fi

if [[ "$BOOST" != "0" ]]; then
    echo "ERROR: CPU boost must be disabled. Found: $BOOST"
    echo "Run: $SCRIPT_DIR/setup_cpu_e1.sh"
    exit 1
fi

for cpu in {2..15}; do
    MIN_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_min_freq)"
    MAX_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_max_freq)"

    if [[ "$MIN_FREQ" != "$TARGET_FREQ_KHZ" || "$MAX_FREQ" != "$TARGET_FREQ_KHZ" ]]; then
        echo "ERROR: CPU $cpu frequency is not locked to ${TARGET_FREQ_KHZ} kHz."
        echo "  scaling_min_freq=$MIN_FREQ"
        echo "  scaling_max_freq=$MAX_FREQ"
        echo "Run: $SCRIPT_DIR/setup_cpu_e1.sh"
        exit 1
    fi
done

echo "[E1] CPU state verified."

echo "[E1] Frequency lock verified: ${TARGET_FREQ_KHZ} kHz on CPUs ${CPUSET}."

RAW_DIR="$PROJECT_ROOT/raw/e1"
mkdir -p "$RAW_DIR"

HEIGHTS_FILE="$RAW_DIR/${CONFIG}_block_heights.csv"

if compgen -G "$RAW_DIR/${CONFIG}_*.csv" > /dev/null; then
    echo "ERROR: Existing raw CSV files found for configuration: $CONFIG"
    echo "Refusing to overwrite or mix measurement runs."
    echo "Remove or archive them before running again."
    exit 1
fi

if [[ -e "$HEIGHTS_FILE" ]]; then
    echo "ERROR: Existing block-height marker file found:"
    echo "  $HEIGHTS_FILE"
    echo "Remove or archive it before running again."
    exit 1
fi

LOG_FILE="$RAW_DIR/${CONFIG}_caliper_run.log"

echo "[E1] Starting Caliper benchmark..."
echo "[E1] Log: $LOG_FILE"

E1_CONFIG="$CONFIG" \
taskset -c "$CPUSET" \
npx caliper launch manager \
    --caliper-workspace . \
    --caliper-benchconfig benchmark.yaml \
    --caliper-networkconfig network.yaml \
    2>&1 | tee "$LOG_FILE"

echo "[E1] Benchmark finished."

echo "[E1] Raw timing files:"
ls -lh "$RAW_DIR"/"${CONFIG}"_*.csv

echo "[E1] Block-height markers:"
cat "$HEIGHTS_FILE"

echo "[E1] Run completed successfully for configuration: $CONFIG"
