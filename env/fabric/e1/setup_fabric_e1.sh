#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

cd "$SCRIPT_DIR"

FABRIC_SAMPLES="${FABRIC_SAMPLES:-$HOME/projects/fabric-samples}"
FABRIC_BIN="$FABRIC_SAMPLES/bin"
FABRIC_CONFIG="$FABRIC_SAMPLES/config"

export PATH="$FABRIC_BIN:$PATH"

CHANNEL_NAME="energychannel"
CHAINCODE_NAME="simplekv"
CHAINCODE_VERSION="1.0"
CHAINCODE_SEQUENCE="1"
CHAINCODE_LABEL="simplekv_1"

echo "[E1] Fabric setup starting..."
echo "[E1] Fabric binaries: $FABRIC_BIN"
echo "[E1] Channel: $CHANNEL_NAME"
echo "[E1] Chaincode: $CHAINCODE_NAME"

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $1"
        exit 1
    fi
}

require_file() {
    if [[ ! -f "$1" ]]; then
        echo "ERROR: Required file not found: $1"
        exit 1
    fi
}

echo "[E1] Running preflight checks..."

require_cmd docker
require_cmd "$FABRIC_BIN/cryptogen"
require_cmd "$FABRIC_BIN/configtxgen"
require_cmd "$FABRIC_BIN/configtxlator"
require_cmd "$FABRIC_BIN/peer"

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: Docker Compose v2 is required."
    exit 1
fi

require_file "$SCRIPT_DIR/crypto-config.yaml"
require_file "$SCRIPT_DIR/configtx.yaml"
require_file "$SCRIPT_DIR/docker-compose.yaml"
require_file "$PROJECT_ROOT/src/e1/chaincode/chaincode.go"

FABRIC_VERSION="$("$FABRIC_BIN/peer" version | awk '/Version:/ {print $2; exit}')"
FABRIC_VERSION="${FABRIC_VERSION#v}"

if [[ "$FABRIC_VERSION" != "2.5.16" ]]; then
    echo "ERROR: Expected Hyperledger Fabric 2.5.16, found: $FABRIC_VERSION"
    exit 1
fi

echo "[E1] Hyperledger Fabric version: $FABRIC_VERSION"

for host in \
    orderer.example.com \
    peer0.org1.example.com \
    peer1.org1.example.com \
    peer0.org2.example.com \
    peer1.org2.example.com
do
    if ! getent hosts "$host" >/dev/null 2>&1; then
        echo "ERROR: Required hostname does not resolve: $host"
        echo "Add the following entries to /etc/hosts:"
        echo "127.0.0.1 orderer.example.com"
        echo "127.0.0.1 peer0.org1.example.com peer1.org1.example.com peer0.org2.example.com peer1.org2.example.com"
        exit 1
    fi
done

echo "[E1] Preflight checks passed."

echo "[E1] Removing previous E1 containers and generated artifacts..."

docker compose -f "$SCRIPT_DIR/docker-compose.yaml" down --remove-orphans || true

rm -rf \
    "$SCRIPT_DIR/crypto-config" \
    "$SCRIPT_DIR/channel-artifacts"

rm -f \
    "$SCRIPT_DIR/"*anchors.tx \
    "$SCRIPT_DIR/config_block.pb" \
    "$SCRIPT_DIR/config_block.json" \
    "$SCRIPT_DIR/simplekv.tar.gz"

mkdir -p "$SCRIPT_DIR/channel-artifacts"

echo "[E1] Previous generated state removed."

echo "[E1] Generating cryptographic material..."

"$FABRIC_BIN/cryptogen" generate \
    --config="$SCRIPT_DIR/crypto-config.yaml" \
    --output="$SCRIPT_DIR/crypto-config"

echo "[E1] Generating orderer genesis block..."

FABRIC_CFG_PATH="$SCRIPT_DIR" "$FABRIC_BIN/configtxgen" \
    -profile E1OrdererGenesis \
    -channelID system-channel \
    -outputBlock "$SCRIPT_DIR/channel-artifacts/genesis.block"

echo "[E1] Generating channel creation transaction..."

FABRIC_CFG_PATH="$SCRIPT_DIR" "$FABRIC_BIN/configtxgen" \
    -profile E1Channel \
    -channelID "$CHANNEL_NAME" \
    -outputCreateChannelTx "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.tx"

echo "[E1] Generating anchor peer updates..."

FABRIC_CFG_PATH="$SCRIPT_DIR" "$FABRIC_BIN/configtxgen" \
    -profile E1Channel \
    -outputAnchorPeersUpdate "$SCRIPT_DIR/Org1MSPanchors.tx" \
    -channelID "$CHANNEL_NAME" \
    -asOrg Org1MSP

FABRIC_CFG_PATH="$SCRIPT_DIR" "$FABRIC_BIN/configtxgen" \
    -profile E1Channel \
    -outputAnchorPeersUpdate "$SCRIPT_DIR/Org2MSPanchors.tx" \
    -channelID "$CHANNEL_NAME" \
    -asOrg Org2MSP

echo "[E1] Fabric cryptographic and channel artifacts generated."

echo "[E1] Starting Fabric network..."

docker compose -f "$SCRIPT_DIR/docker-compose.yaml" up -d

wait_for_container() {
    local container="$1"
    local attempts=60

    for ((i=1; i<=attempts; i++)); do
        if [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" == "true" ]]; then
            echo "[E1] $container is running."
            return 0
        fi

        sleep 1
    done

    echo "ERROR: Container did not become ready: $container"
    docker logs "$container" 2>&1 | tail -50 || true
    exit 1
}

wait_for_container orderer.example.com
wait_for_container peer0.org1.example.com
wait_for_container peer1.org1.example.com
wait_for_container peer0.org2.example.com
wait_for_container peer1.org2.example.com

echo "[E1] Fabric containers are running."

echo "[E1] Waiting for Raft leader..."

wait_for_raft_leader() {
    local attempts=60

    for ((i=1; i<=attempts; i++)); do
        if docker logs orderer.example.com 2>&1 \
            | grep -q "Start accepting requests as Raft leader"; then
            echo "[E1] Raft leader is ready."
            return 0
        fi

        if [[ "$(docker inspect -f '{{.State.Running}}' orderer.example.com 2>/dev/null || true)" != "true" ]]; then
            echo "ERROR: Orderer stopped while waiting for Raft leader."
            docker logs orderer.example.com 2>&1 | tail -100 || true
            exit 1
        fi

        sleep 1
    done

    echo "ERROR: Raft leader was not elected within 60 seconds."
    docker logs orderer.example.com 2>&1 | tail -100 || true
    exit 1
}

wait_for_raft_leader

echo "[E1] Waiting for peers to become ready..."

wait_for_peer() {
    local peer_address="$1"
    local msp_id="$2"
    local msp_path="$3"
    local tls_root_cert="$4"
    local attempts=60

    for ((i=1; i<=attempts; i++)); do
        CORE_PEER_TLS_ENABLED=true \
        CORE_PEER_LOCALMSPID="$msp_id" \
        CORE_PEER_MSPCONFIGPATH="$msp_path" \
        CORE_PEER_ADDRESS="$peer_address" \
        CORE_PEER_TLS_ROOTCERT_FILE="$tls_root_cert" \
        FABRIC_CFG_PATH="$FABRIC_CONFIG" \
        "$FABRIC_BIN/peer" node status >/dev/null 2>&1 && {
            echo "[E1] $peer_address is ready."
            return 0
        }

        sleep 1
    done

    echo "ERROR: Peer did not become ready: $peer_address"
    exit 1
}

wait_for_peer \
    "peer0.org1.example.com:7051" \
    "Org1MSP" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"

wait_for_peer \
    "peer1.org1.example.com:8051" \
    "Org1MSP" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer1.org1.example.com/tls/ca.crt"

wait_for_peer \
    "peer0.org2.example.com:9051" \
    "Org2MSP" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/users/Admin@org2.example.com/msp" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt"

wait_for_peer \
    "peer1.org2.example.com:10051" \
    "Org2MSP" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/users/Admin@org2.example.com/msp" \
    "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer1.org2.example.com/tls/ca.crt"

echo "[E1] All peers are ready."

ORDERER_CA="$SCRIPT_DIR/crypto-config/ordererOrganizations/example.com/orderers/orderer.example.com/tls/ca.crt"

set_org1_peer0() {
    export FABRIC_CFG_PATH="$FABRIC_CONFIG"
    export CORE_PEER_TLS_ENABLED=true
    export CORE_PEER_LOCALMSPID=Org1MSP
    export CORE_PEER_MSPCONFIGPATH="$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp"
    export CORE_PEER_ADDRESS=peer0.org1.example.com:7051
    export CORE_PEER_TLS_ROOTCERT_FILE="$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"
}

set_org1_peer1() {
    export FABRIC_CFG_PATH="$FABRIC_CONFIG"
    export CORE_PEER_TLS_ENABLED=true
    export CORE_PEER_LOCALMSPID=Org1MSP
    export CORE_PEER_MSPCONFIGPATH="$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp"
    export CORE_PEER_ADDRESS=peer1.org1.example.com:8051
    export CORE_PEER_TLS_ROOTCERT_FILE="$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer1.org1.example.com/tls/ca.crt"
}

set_org2_peer0() {
    export FABRIC_CFG_PATH="$FABRIC_CONFIG"
    export CORE_PEER_TLS_ENABLED=true
    export CORE_PEER_LOCALMSPID=Org2MSP
    export CORE_PEER_MSPCONFIGPATH="$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/users/Admin@org2.example.com/msp"
    export CORE_PEER_ADDRESS=peer0.org2.example.com:9051
    export CORE_PEER_TLS_ROOTCERT_FILE="$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt"
}

set_org2_peer1() {
    export FABRIC_CFG_PATH="$FABRIC_CONFIG"
    export CORE_PEER_TLS_ENABLED=true
    export CORE_PEER_LOCALMSPID=Org2MSP
    export CORE_PEER_MSPCONFIGPATH="$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/users/Admin@org2.example.com/msp"
    export CORE_PEER_ADDRESS=peer1.org2.example.com:10051
    export CORE_PEER_TLS_ROOTCERT_FILE="$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer1.org2.example.com/tls/ca.crt"
}

echo "[E1] Creating channel $CHANNEL_NAME..."

set_org1_peer0

"$FABRIC_BIN/peer" channel create \
    -o orderer.example.com:7050 \
    -c "$CHANNEL_NAME" \
    -f "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.tx" \
    --outputBlock "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.block" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Joining peer0.org1..."
set_org1_peer0
"$FABRIC_BIN/peer" channel join \
    -b "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.block"

echo "[E1] Joining peer1.org1..."
set_org1_peer1
"$FABRIC_BIN/peer" channel join \
    -b "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.block"

echo "[E1] Joining peer0.org2..."
set_org2_peer0
"$FABRIC_BIN/peer" channel join \
    -b "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.block"

echo "[E1] Joining peer1.org2..."
set_org2_peer1
"$FABRIC_BIN/peer" channel join \
    -b "$SCRIPT_DIR/channel-artifacts/${CHANNEL_NAME}.block"

echo "[E1] All four peers joined $CHANNEL_NAME."

echo "[E1] Applying Org1 anchor peer update..."

set_org1_peer0

"$FABRIC_BIN/peer" channel update \
    -o orderer.example.com:7050 \
    -c "$CHANNEL_NAME" \
    -f "$SCRIPT_DIR/Org1MSPanchors.tx" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Applying Org2 anchor peer update..."

set_org2_peer0

"$FABRIC_BIN/peer" channel update \
    -o orderer.example.com:7050 \
    -c "$CHANNEL_NAME" \
    -f "$SCRIPT_DIR/Org2MSPanchors.tx" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Anchor peer updates applied."

CHAINCODE_PACKAGE="$SCRIPT_DIR/simplekv.tar.gz"
CHAINCODE_PATH="$PROJECT_ROOT/src/e1/chaincode"

echo "[E1] Packaging chaincode..."

"$FABRIC_BIN/peer" lifecycle chaincode package "$CHAINCODE_PACKAGE" \
    --path "$CHAINCODE_PATH" \
    --lang golang \
    --label "$CHAINCODE_LABEL"

echo "[E1] Installing chaincode on all four peers..."

set_org1_peer0
"$FABRIC_BIN/peer" lifecycle chaincode install "$CHAINCODE_PACKAGE"

set_org1_peer1
"$FABRIC_BIN/peer" lifecycle chaincode install "$CHAINCODE_PACKAGE"

set_org2_peer0
"$FABRIC_BIN/peer" lifecycle chaincode install "$CHAINCODE_PACKAGE"

set_org2_peer1
"$FABRIC_BIN/peer" lifecycle chaincode install "$CHAINCODE_PACKAGE"

PACKAGE_ID="$(
    set_org1_peer0
    "$FABRIC_BIN/peer" lifecycle chaincode queryinstalled \
        | sed -n "s/^Package ID: \(.*\), Label: ${CHAINCODE_LABEL}$/\1/p" \
        | head -n1
)"

if [[ -z "$PACKAGE_ID" ]]; then
    echo "ERROR: Could not determine chaincode package ID."
    exit 1
fi

echo "[E1] Chaincode package ID: $PACKAGE_ID"

echo "[E1] Approving chaincode for Org1..."

set_org1_peer0

"$FABRIC_BIN/peer" lifecycle chaincode approveformyorg \
    -o orderer.example.com:7050 \
    --channelID "$CHANNEL_NAME" \
    --name "$CHAINCODE_NAME" \
    --version "$CHAINCODE_VERSION" \
    --package-id "$PACKAGE_ID" \
    --sequence "$CHAINCODE_SEQUENCE" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Approving chaincode for Org2..."

set_org2_peer0

"$FABRIC_BIN/peer" lifecycle chaincode approveformyorg \
    -o orderer.example.com:7050 \
    --channelID "$CHANNEL_NAME" \
    --name "$CHAINCODE_NAME" \
    --version "$CHAINCODE_VERSION" \
    --package-id "$PACKAGE_ID" \
    --sequence "$CHAINCODE_SEQUENCE" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Checking chaincode commit readiness..."

set_org1_peer0

"$FABRIC_BIN/peer" lifecycle chaincode checkcommitreadiness \
    --channelID "$CHANNEL_NAME" \
    --name "$CHAINCODE_NAME" \
    --version "$CHAINCODE_VERSION" \
    --sequence "$CHAINCODE_SEQUENCE" \
    --output json

echo "[E1] Committing chaincode definition..."

"$FABRIC_BIN/peer" lifecycle chaincode commit \
    -o orderer.example.com:7050 \
    --channelID "$CHANNEL_NAME" \
    --name "$CHAINCODE_NAME" \
    --version "$CHAINCODE_VERSION" \
    --sequence "$CHAINCODE_SEQUENCE" \
    --peerAddresses peer0.org1.example.com:7051 \
    --tlsRootCertFiles "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt" \
    --peerAddresses peer0.org2.example.com:9051 \
    --tlsRootCertFiles "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt" \
    --tls \
    --cafile "$ORDERER_CA"

echo "[E1] Chaincode committed."

echo "[E1] Running chaincode smoke test..."

set_org1_peer0

"$FABRIC_BIN/peer" chaincode invoke \
    -o orderer.example.com:7050 \
    -C "$CHANNEL_NAME" \
    -n "$CHAINCODE_NAME" \
    --peerAddresses peer0.org1.example.com:7051 \
    --tlsRootCertFiles "$SCRIPT_DIR/crypto-config/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt" \
    --peerAddresses peer0.org2.example.com:9051 \
    --tlsRootCertFiles "$SCRIPT_DIR/crypto-config/peerOrganizations/org2.example.com/peers/peer0.org2.example.com/tls/ca.crt" \
    --tls \
    --cafile "$ORDERER_CA" \
    -c '{"Args":["Set","setup_test_key","setup_test_value"]}'

sleep 2

QUERY_RESULT="$(
    "$FABRIC_BIN/peer" chaincode query \
        -C "$CHANNEL_NAME" \
        -n "$CHAINCODE_NAME" \
        -c '{"Args":["Get","setup_test_key"]}'
)"

if [[ "$QUERY_RESULT" != "setup_test_value" ]]; then
    echo "ERROR: Chaincode smoke test failed."
    echo "Expected: setup_test_value"
    echo "Received: $QUERY_RESULT"
    exit 1
fi

echo "[E1] Chaincode smoke test passed."

set_org1_peer0

echo "[E1] Verifying cross-organization discovery..."

DISCOVER_OUTPUT="$(
    "$FABRIC_BIN/discover" peers \
        --peerTLSCA "$CORE_PEER_TLS_ROOTCERT_FILE" \
        --userKey "$CORE_PEER_MSPCONFIGPATH/keystore/priv_sk" \
        --userCert "$CORE_PEER_MSPCONFIGPATH/signcerts/Admin@org1.example.com-cert.pem" \
        --MSP Org1MSP \
        --server peer0.org1.example.com:7051 \
        --channel "$CHANNEL_NAME"
)"

for endpoint in \
    "peer0.org1.example.com:7051" \
    "peer1.org1.example.com:8051" \
    "peer0.org2.example.com:9051" \
    "peer1.org2.example.com:10051"
do
    if ! grep -q "\"Endpoint\": \"$endpoint\"" <<< "$DISCOVER_OUTPUT"; then
        echo "ERROR: Discovery did not return $endpoint"
        exit 1
    fi
done

echo "[E1] Discovery check passed: all four peers visible."
echo "[E1] Fabric E1 setup completed successfully."
