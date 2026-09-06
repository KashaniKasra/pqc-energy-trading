#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FABRIC_E1_DIR="$PROJECT_ROOT/env/fabric/e1"

CPUSET="2-15"
TARGET_FREQ_KHZ=3201000

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 {ecdsa|ml-dsa-44|ml-dsa-65|sphincs}"
    exit 1
fi

CONFIG="$1"

case "$CONFIG" in
    ecdsa|ml-dsa-44|ml-dsa-65|sphincs)
        ;;
    *)
        echo "ERROR: Unsupported E1 configuration: $CONFIG"
        echo "Expected one of: ecdsa, ml-dsa-44, ml-dsa-65, sphincs"
        exit 1
        ;;
esac

cd "$SCRIPT_DIR"

for command_name in basename date docker git grep jq npx realpath sha256sum taskset tee uname; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $command_name"
        exit 1
    fi
done

RUN_LABEL="${E1_RUN_LABEL:-}"

if [[ -n "$RUN_LABEL" && ! "$RUN_LABEL" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "ERROR: E1_RUN_LABEL must contain only lowercase letters, digits, dot, underscore, or hyphen."
    exit 1
fi

if [[ -n "$RUN_LABEL" ]]; then
    RUN_NAMESPACE="${RUN_LABEL}_${CONFIG}"
    RUN_TYPE="${E1_RUN_TYPE:-diagnostic}"
else
    RUN_NAMESPACE="$CONFIG"
    RUN_TYPE="${E1_RUN_TYPE:-fixed-profile}"
fi

if [[ ! "$RUN_TYPE" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "ERROR: E1_RUN_TYPE must contain only lowercase letters, digits, dot, underscore, or hyphen."
    exit 1
fi

BENCHMARK_CONFIG_INPUT="${E1_BENCHCONFIG:-benchmark.yaml}"
if [[ "$BENCHMARK_CONFIG_INPUT" = /* ]]; then
    BENCHMARK_CONFIG="$BENCHMARK_CONFIG_INPUT"
else
    BENCHMARK_CONFIG="$SCRIPT_DIR/$BENCHMARK_CONFIG_INPUT"
fi

if [[ ! -f "$BENCHMARK_CONFIG" ]]; then
    echo "ERROR: Benchmark configuration not found: $BENCHMARK_CONFIG"
    exit 1
fi

BENCHMARK_CONFIG="$(realpath "$BENCHMARK_CONFIG")"
BENCHMARK_CONFIG_SHA256="$(sha256sum "$BENCHMARK_CONFIG" | awk '{print $1}')"
BENCHMARK_CONFIG_REPO_PATH="$(realpath --relative-to="$PROJECT_ROOT" "$BENCHMARK_CONFIG")"

if [[ -z "$RUN_LABEL" && "$BENCHMARK_CONFIG" != "$(realpath "$SCRIPT_DIR/benchmark.yaml")" ]]; then
    echo "ERROR: A non-default benchmark requires E1_RUN_LABEL to isolate its raw artifacts."
    exit 1
fi

if [[ -z "$RUN_LABEL" && "$RUN_TYPE" != "fixed-profile" ]]; then
    echo "ERROR: An unlabeled run must use run type fixed-profile."
    exit 1
fi

case "$(basename "$BENCHMARK_CONFIG")" in
    benchmark_sphincs_sweep.yaml|benchmark_sphincs_refine_3_4.yaml)
        if [[ "$CONFIG" != "sphincs" || "$RUN_TYPE" != "sweep" || -z "$RUN_LABEL" ]]; then
            echo "ERROR: SPHINCS+ sweep profiles require config=sphincs, E1_RUN_TYPE=sweep, and a nonempty E1_RUN_LABEL."
            exit 1
        fi
        ;;
esac

PEER_GATEWAY_FILE="$SCRIPT_DIR/node_modules/@hyperledger/caliper-fabric/lib/connector-versions/peer-gateway/PeerGateway.js"
if [[ ! -f "$PEER_GATEWAY_FILE" ]] || ! grep -Fq 'start_offset_ms,latency_ms,status,tx_id' "$PEER_GATEWAY_FILE"; then
    echo "ERROR: Caliper's reproducible E1 end-to-end timing patch is not installed."
    echo "Run: $SCRIPT_DIR/setup_caliper_e1.sh"
    exit 1
fi

RAW_DIR="$PROJECT_ROOT/raw/e1"
mkdir -p "$RAW_DIR"

LOG_FILE="$RAW_DIR/${RUN_NAMESPACE}_caliper_run.log"
HEIGHTS_FILE="$RAW_DIR/${RUN_NAMESPACE}_block_heights.csv"

if compgen -G "$RAW_DIR/${RUN_NAMESPACE}_*" > /dev/null; then
    echo "ERROR: Existing raw artifacts found for run namespace: $RUN_NAMESPACE"
    echo "Refusing to overwrite or mix measurement runs:"
    compgen -G "$RAW_DIR/${RUN_NAMESPACE}_*" | sort
    exit 1
fi

PROJECT_GIT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
PROJECT_GIT_STATUS="$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=normal)"
PROJECT_GIT_STATUS_SHA256="$(printf '%s' "$PROJECT_GIT_STATUS" | sha256sum | awk '{print $1}')"
PROJECT_TRACKED_STATUS="$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=no)"
PROJECT_TRACKED_STATUS_SHA256="$(printf '%s' "$PROJECT_TRACKED_STATUS" | sha256sum | awk '{print $1}')"
PROJECT_UNTRACKED_PATHS="$(git -C "$PROJECT_ROOT" ls-files --others --exclude-standard | sort)"
PROJECT_UNTRACKED_PATHS_SHA256="$(printf '%s' "$PROJECT_UNTRACKED_PATHS" | sha256sum | awk '{print $1}')"
if [[ -n "$PROJECT_GIT_STATUS" ]]; then
    PROJECT_GIT_STATE="dirty"
else
    PROJECT_GIT_STATE="clean"
fi
if [[ -n "$PROJECT_TRACKED_STATUS" ]]; then
    PROJECT_TRACKED_STATE="dirty"
else
    PROJECT_TRACKED_STATE="clean"
fi

exec > >(tee "$LOG_FILE") 2>&1

echo "[E1] Run provenance"
echo "[E1] started_at=$(date --iso-8601=seconds)"
echo "[E1] configuration=$CONFIG"
echo "[E1] run_type=$RUN_TYPE"
echo "[E1] run_label=${RUN_LABEL:-canonical}"
echo "[E1] run_namespace=$RUN_NAMESPACE"
echo "[E1] benchmark_config=$BENCHMARK_CONFIG_REPO_PATH"
echo "[E1] benchmark_config_sha256=$BENCHMARK_CONFIG_SHA256"
echo "[E1] project_git_commit=$PROJECT_GIT_COMMIT"
echo "[E1] project_git_state_before_log_creation=$PROJECT_GIT_STATE"
echo "[E1] project_git_status_sha256_before_log_creation=$PROJECT_GIT_STATUS_SHA256"
echo "[E1] project_tracked_state_before_log_creation=$PROJECT_TRACKED_STATE"
echo "[E1] project_tracked_status_sha256_before_log_creation=$PROJECT_TRACKED_STATUS_SHA256"
echo "[E1] project_untracked_paths_sha256_before_log_creation=$PROJECT_UNTRACKED_PATHS_SHA256"
echo "[E1] host_kernel=$(uname -r)"
echo "[E1] docker_server_version=$(docker version --format '{{.Server.Version}}')"
echo "[E1] fixed_cpu_set=$CPUSET"

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
    CPU_GOVERNOR="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_governor)"
    MIN_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_min_freq)"
    MAX_FREQ="$(cat /sys/devices/system/cpu/cpu${cpu}/cpufreq/scaling_max_freq)"

    echo "[E1] cpu${cpu}_state=governor:${CPU_GOVERNOR},min_khz:${MIN_FREQ},max_khz:${MAX_FREQ}"

    if [[ "$CPU_GOVERNOR" != "performance" || "$MIN_FREQ" != "$TARGET_FREQ_KHZ" || "$MAX_FREQ" != "$TARGET_FREQ_KHZ" ]]; then
        echo "ERROR: CPU $cpu is not in the required controlled state."
        echo "  scaling_governor=$CPU_GOVERNOR"
        echo "  scaling_min_freq=$MIN_FREQ"
        echo "  scaling_max_freq=$MAX_FREQ"
        echo "Run: $SCRIPT_DIR/setup_cpu_e1.sh"
        exit 1
    fi
done

echo "[E1] CPU state verified."
echo "[E1] amd_pstate_mode=$PSTATE"
echo "[E1] boost_state=$BOOST"
echo "[E1] Frequency lock verified: ${TARGET_FREQ_KHZ} kHz on CPUs ${CPUSET}."

FABRIC_SETUP="$FABRIC_E1_DIR/setup_fabric_e1.sh"

if [[ ! -x "$FABRIC_SETUP" ]]; then
    echo "ERROR: Fabric setup script not found or not executable:"
    echo "  $FABRIC_SETUP"
    exit 1
fi

FABRIC_SOURCE_DIR="${FABRIC_DIR:-$HOME/projects/fabric}"
if [[ -d "$FABRIC_SOURCE_DIR/.git" ]]; then
    FABRIC_SOURCE_COMMIT="$(git -C "$FABRIC_SOURCE_DIR" rev-parse HEAD)"
    FABRIC_SOURCE_TAG="$(git -C "$FABRIC_SOURCE_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo untagged)"
    if [[ -n "$(git -C "$FABRIC_SOURCE_DIR" status --porcelain --untracked-files=normal)" ]]; then
        FABRIC_SOURCE_STATE="dirty"
    else
        FABRIC_SOURCE_STATE="clean"
    fi
else
    FABRIC_SOURCE_COMMIT="unavailable"
    FABRIC_SOURCE_TAG="unavailable"
    FABRIC_SOURCE_STATE="unavailable"
fi

PEER_IMAGE_ID="$(docker image inspect fabric-peer:2.5.16-pq --format '{{.Id}}')"
ORDERER_IMAGE_ID="$(docker image inspect fabric-orderer:2.5.16-pq --format '{{.Id}}')"

echo "[E1] fabric_source_tag=$FABRIC_SOURCE_TAG"
echo "[E1] fabric_source_commit=$FABRIC_SOURCE_COMMIT"
echo "[E1] fabric_source_state=$FABRIC_SOURCE_STATE"
echo "[E1] fabric_peer_image_id=$PEER_IMAGE_ID"
echo "[E1] fabric_orderer_image_id=$ORDERER_IMAGE_ID"
echo "[E1] configtx_sha256=$(sha256sum "$FABRIC_E1_DIR/configtx.yaml" | awk '{print $1}')"
echo "[E1] crypto_config_sha256=$(sha256sum "$FABRIC_E1_DIR/crypto-config.yaml" | awk '{print $1}')"
echo "[E1] docker_compose_sha256=$(sha256sum "$FABRIC_E1_DIR/docker-compose.yaml" | awk '{print $1}')"
echo "[E1] fabric_setup_sha256=$(sha256sum "$FABRIC_SETUP" | awk '{print $1}')"
echo "[E1] fabric_build_sha256=$(sha256sum "$FABRIC_E1_DIR/build_fabric_pq.sh" | awk '{print $1}')"
echo "[E1] fabric_pq_patch_sha256=$(sha256sum "$FABRIC_E1_DIR/patches/fabric-2.5.16-pq-identities.patch" | awk '{print $1}')"
echo "[E1] chaincode_sha256=$(sha256sum "$PROJECT_ROOT/src/e1/chaincode/chaincode.go" | awk '{print $1}')"
echo "[E1] pq_identity_generator_sha256=$(sha256sum "$PROJECT_ROOT/src/e1/pqidentity/generate.go" | awk '{print $1}')"
echo "[E1] caliper_package_lock_sha256=$(sha256sum "$SCRIPT_DIR/package-lock.json" | awk '{print $1}')"
echo "[E1] caliper_network_sha256=$(sha256sum "$SCRIPT_DIR/network.yaml" | awk '{print $1}')"
echo "[E1] caliper_connection_org1_sha256=$(sha256sum "$SCRIPT_DIR/connection-org1.yaml" | awk '{print $1}')"
echo "[E1] caliper_connection_org2_sha256=$(sha256sum "$SCRIPT_DIR/connection-org2.yaml" | awk '{print $1}')"
echo "[E1] caliper_workload_sha256=$(sha256sum "$SCRIPT_DIR/workload/set.js" | awk '{print $1}')"
echo "[E1] caliper_timing_patch_sha256=$(sha256sum "$SCRIPT_DIR/patches/peer-gateway-e1-timing.patch" | awk '{print $1}')"
echo "[E1] e1_runner_sha256=$(sha256sum "$SCRIPT_DIR/run_e1.sh" | awk '{print $1}')"

echo "[E1] Setting up Fabric network for configuration: $CONFIG"

E1_CONFIG="$CONFIG" "$FABRIC_SETUP"

echo "[E1] Fabric network ready for configuration: $CONFIG"

CONFIGTXLATOR="${FABRIC_SAMPLES:-$HOME/projects/fabric-samples}/bin/configtxlator"
CHANNEL_GENESIS_BLOCK="$FABRIC_E1_DIR/channel-artifacts/energychannel.block"

if [[ ! -x "$CONFIGTXLATOR" || ! -f "$CHANNEL_GENESIS_BLOCK" ]]; then
    echo "ERROR: Cannot inspect the generated effective Fabric block configuration."
    exit 1
fi

BLOCK_PARAMETERS="$($CONFIGTXLATOR proto_decode \
    --input "$CHANNEL_GENESIS_BLOCK" \
    --type common.Block \
    --output /dev/stdout \
    | jq -r '[
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchTimeout.value.timeout,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.max_message_count,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.preferred_max_bytes,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.absolute_max_bytes
      ] | @tsv')"

IFS=$'\t' read -r BATCH_TIMEOUT MAX_MESSAGE_COUNT PREFERRED_MAX_BYTES ABSOLUTE_MAX_BYTES <<< "$BLOCK_PARAMETERS"

if [[ -z "$BATCH_TIMEOUT" || -z "$MAX_MESSAGE_COUNT" || -z "$PREFERRED_MAX_BYTES" || -z "$ABSOLUTE_MAX_BYTES" ]]; then
    echo "ERROR: Generated Fabric block parameters are incomplete."
    exit 1
fi

echo "[E1] effective_BatchTimeout=$BATCH_TIMEOUT"
echo "[E1] effective_MaxMessageCount=$MAX_MESSAGE_COUNT"
echo "[E1] effective_PreferredMaxBytes=$PREFERRED_MAX_BYTES"
echo "[E1] effective_AbsoluteMaxBytes=$ABSOLUTE_MAX_BYTES"

echo "[E1] Starting Caliper benchmark..."
echo "[E1] Log: $LOG_FILE"

E1_CONFIG="$CONFIG" \
E1_RUN_NAMESPACE="$RUN_NAMESPACE" \
taskset -c "$CPUSET" \
npx caliper launch manager \
    --caliper-workspace . \
    --caliper-benchconfig "$BENCHMARK_CONFIG" \
    --caliper-networkconfig network.yaml

echo "[E1] Benchmark finished."
echo "[E1] finished_at=$(date --iso-8601=seconds)"

echo "[E1] Raw timing files:"
find "$RAW_DIR" -maxdepth 1 -type f -name "${RUN_NAMESPACE}_*.csv" -printf '%f\t%s bytes\n' | sort

echo "[E1] Block-height markers:"
cat "$HEIGHTS_FILE"

echo "[E1] Run completed successfully for namespace: $RUN_NAMESPACE"
