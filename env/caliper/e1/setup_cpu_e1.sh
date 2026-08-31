#!/usr/bin/env bash
set -euo pipefail

CPUSET="2-15"
TARGET_FREQ_KHZ=3201000

echo "[E1] Configuring CPU measurement state..."

if [[ ! -f /sys/devices/system/cpu/amd_pstate/status ]]; then
    echo "ERROR: amd_pstate status file not found."
    exit 1
fi

if [[ ! -f /sys/devices/system/cpu/cpufreq/boost ]]; then
    echo "ERROR: CPU boost control not found."
    exit 1
fi

echo passive | sudo tee /sys/devices/system/cpu/amd_pstate/status >/dev/null

for governor_file in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$governor_file" >/dev/null
done

echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost >/dev/null

echo "[E1] Locking experiment CPUs to ${TARGET_FREQ_KHZ} kHz..."

for cpu in {2..15}; do
    CPUFREQ_DIR="/sys/devices/system/cpu/cpu${cpu}/cpufreq"

    if [[ ! -d "$CPUFREQ_DIR" ]]; then
        echo "ERROR: CPU frequency directory not found for CPU $cpu"
        exit 1
    fi

    echo "$TARGET_FREQ_KHZ" | sudo tee "$CPUFREQ_DIR/scaling_max_freq" >/dev/null
    echo "$TARGET_FREQ_KHZ" | sudo tee "$CPUFREQ_DIR/scaling_min_freq" >/dev/null
done

PSTATE="$(cat /sys/devices/system/cpu/amd_pstate/status)"
GOVERNOR="$(cat /sys/devices/system/cpu/cpu2/cpufreq/scaling_governor)"
BOOST="$(cat /sys/devices/system/cpu/cpufreq/boost)"

for cpu in {2..15}; do
    MIN_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_min_freq)"
    MAX_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_max_freq)"

    if [[ "$MIN_FREQ" != "$TARGET_FREQ_KHZ" || "$MAX_FREQ" != "$TARGET_FREQ_KHZ" ]]; then
        echo "ERROR: CPU $cpu frequency is not locked to ${TARGET_FREQ_KHZ} kHz."
        echo "  scaling_min_freq=$MIN_FREQ"
        echo "  scaling_max_freq=$MAX_FREQ"
        exit 1
    fi
done

if [[ "$PSTATE" != "passive" ]]; then
    echo "ERROR: amd_pstate is not passive: $PSTATE"
    exit 1
fi

if [[ "$GOVERNOR" != "performance" ]]; then
    echo "ERROR: CPU governor is not performance: $GOVERNOR"
    exit 1
fi

if [[ "$BOOST" != "0" ]]; then
    echo "ERROR: CPU boost is not disabled: $BOOST"
    exit 1
fi

echo "[E1] CPU state verified:"
echo "  fixed CPU set: $CPUSET"
echo "  amd_pstate:    $PSTATE"
echo "  governor:      $GOVERNOR"
echo "  boost:         $BOOST"
echo "  frequency:     ${TARGET_FREQ_KHZ} kHz"
