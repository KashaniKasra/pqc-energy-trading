#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 <config> <run_label> <benchmark_label>"
    echo "Example: $0 ecdsa blockutil-300 blockutil-300-tps"
    exit 1
fi

CONFIG="$1"
RUN_LABEL="$2"
BENCHMARK_LABEL="$3"

case "$CONFIG" in
    ecdsa|ml-dsa-44|ml-dsa-65|sphincs)
        ;;
    *)
        echo "ERROR: Unsupported E1 configuration: $CONFIG"
        exit 1
        ;;
esac

for label_value in "$RUN_LABEL" "$BENCHMARK_LABEL"; do
    if [[ ! "$label_value" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
        echo "ERROR: Run and benchmark labels must contain only lowercase letters, digits, dot, underscore, or hyphen."
        exit 1
    fi
done

for command_name in awk docker jq openssl realpath sha256sum stat; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $command_name"
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RAW_DIR="$PROJECT_ROOT/raw/e1"
FABRIC_SAMPLES="${FABRIC_SAMPLES:-$HOME/projects/fabric-samples}"
CONFIGTXLATOR="$FABRIC_SAMPLES/bin/configtxlator"

PEER_CONTAINER="peer0.org1.example.com"
CHANNEL="energychannel"
RUN_NAMESPACE="${RUN_LABEL}_${CONFIG}"
HEIGHTS_FILE="$RAW_DIR/${RUN_NAMESPACE}_block_heights.csv"
RAW_BLOCKS_FILE="$RAW_DIR/${RUN_NAMESPACE}_blocks_${BENCHMARK_LABEL}.csv"
SUMMARY_FILE="$RAW_DIR/${RUN_NAMESPACE}_blocks_${BENCHMARK_LABEL}_summary.csv"

if [[ ! -x "$CONFIGTXLATOR" ]]; then
    echo "ERROR: configtxlator not found or not executable: $CONFIGTXLATOR"
    exit 1
fi

if [[ ! -f "$HEIGHTS_FILE" ]]; then
    echo "ERROR: Height markers not found for run namespace: $HEIGHTS_FILE"
    exit 1
fi

for output_file in "$RAW_BLOCKS_FILE" "$SUMMARY_FILE"; do
    if [[ -e "$output_file" ]]; then
        echo "ERROR: Refusing to overwrite existing measurement output: $output_file"
        exit 1
    fi
done

if [[ "$(docker inspect -f '{{.State.Running}}' "$PEER_CONTAINER" 2>/dev/null || true)" != "true" ]]; then
    echo "ERROR: Required peer container is not running: $PEER_CONTAINER"
    exit 1
fi

START_BLOCK="$(awk -F, -v marker="before_${BENCHMARK_LABEL}" '$1 == marker {print $2}' "$HEIGHTS_FILE")"
AFTER_BLOCK="$(awk -F, -v marker="after_${BENCHMARK_LABEL}" '$1 == marker {print $2}' "$HEIGHTS_FILE")"

if ! [[ "$START_BLOCK" =~ ^[0-9]+$ && "$AFTER_BLOCK" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Could not resolve unique numeric before/after markers for benchmark label: $BENCHMARK_LABEL"
    exit 1
fi

if (( START_BLOCK == 0 )); then
    echo "ERROR: Genesis block 0 must never be included."
    exit 1
fi

if (( AFTER_BLOCK <= START_BLOCK )); then
    echo "ERROR: Invalid height interval: before=$START_BLOCK after=$AFTER_BLOCK"
    exit 1
fi

END_BLOCK=$((AFTER_BLOCK - 1))

PEER_CERT="$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/msp/signcerts/peer0.org1.example.com-cert.pem"
if [[ ! -f "$PEER_CERT" ]]; then
    echo "ERROR: Current peer identity certificate not found: $PEER_CERT"
    exit 1
fi

CERT_TEXT="$(openssl x509 -in "$PEER_CERT" -noout -text)"
case "$CONFIG" in
    ecdsa)
        if grep -q '1.3.6.1.3.9999.1' <<< "$CERT_TEXT"; then
            echo "ERROR: Current network identity is PQ-extended but the requested config is ecdsa."
            exit 1
        fi
        ;;
    ml-dsa-44)
        EXPECTED_ALGORITHM="ML-DSA-44"
        ;;
    ml-dsa-65)
        EXPECTED_ALGORITHM="ML-DSA-65"
        ;;
    sphincs)
        EXPECTED_ALGORITHM="SPHINCS+-SHA2-128s-simple"
        ;;
esac

if [[ "$CONFIG" != "ecdsa" ]]; then
    if ! grep -q '1.3.6.1.3.9999.1' <<< "$CERT_TEXT" || ! grep -Fq "$EXPECTED_ALGORITHM" <<< "$CERT_TEXT"; then
        echo "ERROR: Current peer identity does not match requested config $CONFIG ($EXPECTED_ALGORITHM)."
        exit 1
    fi
fi

ORDERER_CA="$SCRIPT_DIR/crypto-config/ordererOrganizations/example.com/orderers/orderer.example.com/tls/ca.crt"
if [[ ! -f "$ORDERER_CA" ]]; then
    echo "ERROR: Orderer TLS CA not found: $ORDERER_CA"
    exit 1
fi

HOST_TEMP_DIR="$(mktemp -d)"
CONTAINER_PREFIX="/tmp/e1-block-measure-${RUN_NAMESPACE}-$$"
CONTAINER_CA="${CONTAINER_PREFIX}-orderer-ca.crt"

cleanup() {
    docker exec "$PEER_CONTAINER" sh -c "rm -f ${CONTAINER_PREFIX}-*.pb ${CONTAINER_CA}" >/dev/null 2>&1 || true
    rm -rf "$HOST_TEMP_DIR"
}
trap cleanup EXIT

docker cp "$ORDERER_CA" "$PEER_CONTAINER:$CONTAINER_CA" >/dev/null

CONTAINER_CONFIG_BLOCK="${CONTAINER_PREFIX}-config.pb"
HOST_CONFIG_BLOCK="$HOST_TEMP_DIR/config.pb"

docker exec "$PEER_CONTAINER" \
    peer channel fetch config "$CONTAINER_CONFIG_BLOCK" \
    -c "$CHANNEL" \
    -o orderer.example.com:7050 \
    --tls \
    --cafile "$CONTAINER_CA" \
    >/dev/null 2>&1

docker cp "$PEER_CONTAINER:$CONTAINER_CONFIG_BLOCK" "$HOST_CONFIG_BLOCK" >/dev/null

CONFIG_PARAMETERS="$($CONFIGTXLATOR proto_decode \
    --input "$HOST_CONFIG_BLOCK" \
    --type common.Block \
    --output /dev/stdout \
    | jq -r '[
        .header.number,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchTimeout.value.timeout,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.max_message_count,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.preferred_max_bytes,
        .data.data[0].payload.data.config.channel_group.groups.Orderer.values.BatchSize.value.absolute_max_bytes
      ] | @tsv')"

IFS=$'\t' read -r CONFIG_BLOCK_NUMBER BATCH_TIMEOUT MAX_MESSAGE_COUNT PREFERRED_MAX_BYTES ABSOLUTE_MAX_BYTES <<< "$CONFIG_PARAMETERS"
CONFIG_BLOCK_SHA256="$(sha256sum "$HOST_CONFIG_BLOCK" | awk '{print $1}')"

if ! [[ "$CONFIG_BLOCK_NUMBER" =~ ^[0-9]+$ && "$MAX_MESSAGE_COUNT" =~ ^[0-9]+$ && "$PREFERRED_MAX_BYTES" =~ ^[0-9]+$ && "$ABSOLUTE_MAX_BYTES" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Failed to derive effective Fabric block parameters from the current channel config block."
    exit 1
fi

if (( CONFIG_BLOCK_NUMBER >= START_BLOCK )); then
    echo "ERROR: The latest config block ($CONFIG_BLOCK_NUMBER) overlaps or follows the measured interval start ($START_BLOCK)."
    echo "A single effective block-capacity configuration cannot be attributed to this interval."
    exit 1
fi

if (( PREFERRED_MAX_BYTES == 0 )); then
    echo "ERROR: Effective PreferredMaxBytes must be greater than zero."
    exit 1
fi

printf '%s\n' \
    'config,run_label,benchmark_label,block_number,block_bytes,transaction_count,header_types,classification,accepted_for_mean' \
    > "$RAW_BLOCKS_FILE"

ordinary_count=0
excluded_count=0
ordinary_total_bytes=0
ordinary_total_transactions=0

for (( block=START_BLOCK; block<=END_BLOCK; block++ )); do
    container_block="${CONTAINER_PREFIX}-${block}.pb"
    host_block="$HOST_TEMP_DIR/block-${block}.pb"

    docker exec "$PEER_CONTAINER" \
        peer channel fetch "$block" "$container_block" \
        -c "$CHANNEL" \
        -o orderer.example.com:7050 \
        --tls \
        --cafile "$CONTAINER_CA" \
        >/dev/null 2>&1

    docker cp "$PEER_CONTAINER:$container_block" "$host_block" >/dev/null

    block_bytes="$(stat -c '%s' "$host_block")"
    block_metadata="$($CONFIGTXLATOR proto_decode \
        --input "$host_block" \
        --type common.Block \
        --output /dev/stdout \
        | jq -r '[
            (.data.data | length),
            ([.data.data[].payload.header.channel_header.type] | map(tostring) | join(";")),
            (if ([.data.data[].payload.header.channel_header.type] | any(. == 1 or . == 2)) then "config"
             elif ((.data.data | length) > 0 and ([.data.data[].payload.header.channel_header.type] | all(. == 3))) then "ordinary_transaction"
             else "other"
             end)
          ] | @tsv')"

    IFS=$'\t' read -r transaction_count header_types classification <<< "$block_metadata"

    if [[ "$classification" == "ordinary_transaction" ]]; then
        accepted_for_mean="true"
        ordinary_count=$((ordinary_count + 1))
        ordinary_total_bytes=$((ordinary_total_bytes + block_bytes))
        ordinary_total_transactions=$((ordinary_total_transactions + transaction_count))
    else
        accepted_for_mean="false"
        excluded_count=$((excluded_count + 1))
    fi

    printf '%s,%s,%s,%d,%d,%d,%s,%s,%s\n' \
        "$CONFIG" \
        "$RUN_LABEL" \
        "$BENCHMARK_LABEL" \
        "$block" \
        "$block_bytes" \
        "$transaction_count" \
        "$header_types" \
        "$classification" \
        "$accepted_for_mean" \
        >> "$RAW_BLOCKS_FILE"

    docker exec "$PEER_CONTAINER" rm -f "$container_block" >/dev/null
done

if (( ordinary_count == 0 )); then
    echo "ERROR: No ordinary transaction blocks were found in the measured interval."
    exit 1
fi

BLOCK_BYTES_MEAN="$(awk -v total="$ordinary_total_bytes" -v n="$ordinary_count" 'BEGIN { printf "%.6f", total / n }')"
TRANSACTIONS_MEAN="$(awk -v total="$ordinary_total_transactions" -v n="$ordinary_count" 'BEGIN { printf "%.6f", total / n }')"
BLOCK_UTILISATION="$(awk -v mean="$BLOCK_BYTES_MEAN" -v preferred="$PREFERRED_MAX_BYTES" 'BEGIN { printf "%.9f", mean / preferred }')"
RAW_BLOCKS_SHA256="$(sha256sum "$RAW_BLOCKS_FILE" | awk '{print $1}')"

printf '%s\n' \
    'config,run_label,benchmark_label,start_block,end_block,ordinary_block_count,excluded_block_count,block_bytes_mean,transactions_per_block_mean,effective_preferred_max_bytes,block_utilisation,BatchTimeout,MaxMessageCount,AbsoluteMaxBytes,effective_config_block_number,effective_config_block_sha256,raw_blocks_file,raw_blocks_sha256,measurement_status' \
    > "$SUMMARY_FILE"

printf '%s,%s,%s,%d,%d,%d,%d,%s,%s,%d,%s,%s,%d,%d,%d,%s,%s,%s,%s\n' \
    "$CONFIG" \
    "$RUN_LABEL" \
    "$BENCHMARK_LABEL" \
    "$START_BLOCK" \
    "$END_BLOCK" \
    "$ordinary_count" \
    "$excluded_count" \
    "$BLOCK_BYTES_MEAN" \
    "$TRANSACTIONS_MEAN" \
    "$PREFERRED_MAX_BYTES" \
    "$BLOCK_UTILISATION" \
    "$BATCH_TIMEOUT" \
    "$MAX_MESSAGE_COUNT" \
    "$ABSOLUTE_MAX_BYTES" \
    "$CONFIG_BLOCK_NUMBER" \
    "$CONFIG_BLOCK_SHA256" \
    "$(realpath --relative-to="$PROJECT_ROOT" "$RAW_BLOCKS_FILE")" \
    "$RAW_BLOCKS_SHA256" \
    'diagnostic_unreviewed' \
    >> "$SUMMARY_FILE"

echo "Block measurement completed."
echo "  run_namespace=$RUN_NAMESPACE"
echo "  measured_interval=${START_BLOCK}-${END_BLOCK}"
echo "  ordinary_blocks=$ordinary_count"
echo "  excluded_blocks=$excluded_count"
echo "  block_bytes_mean=$BLOCK_BYTES_MEAN"
echo "  effective_PreferredMaxBytes=$PREFERRED_MAX_BYTES"
echo "  block_utilisation=$BLOCK_UTILISATION"
echo "  raw_blocks_file=$RAW_BLOCKS_FILE"
echo "  summary_file=$SUMMARY_FILE"
echo "  measurement_status=diagnostic_unreviewed"
