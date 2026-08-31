#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <start_block> <end_block>"
    exit 1
fi

START_BLOCK="$1"
END_BLOCK="$2"

if ! [[ "$START_BLOCK" =~ ^[0-9]+$ && "$END_BLOCK" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Block numbers must be non-negative integers."
    exit 1
fi

if (( START_BLOCK > END_BLOCK )); then
    echo "ERROR: start_block must be <= end_block."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PEER_CONTAINER="peer0.org1.example.com"
CHANNEL="energychannel"

ORDERER_CA="$SCRIPT_DIR/crypto-config/ordererOrganizations/example.com/orderers/orderer.example.com/tls/ca.crt"
CONTAINER_CA="/tmp/e1-orderer-ca.crt"

if [[ ! -f "$ORDERER_CA" ]]; then
    echo "ERROR: Orderer TLS CA not found:"
    echo "  $ORDERER_CA"
    exit 1
fi

docker cp "$ORDERER_CA" \
    "$PEER_CONTAINER:$CONTAINER_CA" >/dev/null

total_bytes=0
count=0

for (( block=START_BLOCK; block<=END_BLOCK; block++ )); do
    block_file="/tmp/e1-block-${block}.pb"

    docker exec "$PEER_CONTAINER" \
        peer channel fetch "$block" "$block_file" \
        -c "$CHANNEL" \
        -o orderer.example.com:7050 \
        --tls \
        --cafile "$CONTAINER_CA" \
        >/dev/null 2>&1

    size="$(
        docker exec "$PEER_CONTAINER" \
            stat -c '%s' "$block_file"
    )"

    printf "block=%d bytes=%d\n" "$block" "$size"

    total_bytes=$((total_bytes + size))
    count=$((count + 1))

    docker exec "$PEER_CONTAINER" rm -f "$block_file"
done

average_bytes="$(awk -v total="$total_bytes" -v n="$count" 'BEGIN { printf "%.2f", total / n }')"

echo "blocks=$count"
echo "total_bytes=$total_bytes"
echo "average_block_bytes=$average_bytes"

docker exec "$PEER_CONTAINER" rm -f "$CONTAINER_CA"
