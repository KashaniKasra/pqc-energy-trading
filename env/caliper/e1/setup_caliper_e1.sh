#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[E1] Installing pinned Caliper dependencies..."
npm ci

PEER_GATEWAY_FILE="node_modules/@hyperledger/caliper-fabric/lib/connector-versions/peer-gateway/PeerGateway.js"
PATCH_FILE="patches/peer-gateway-e1-timing.patch"

if [[ ! -f "$PEER_GATEWAY_FILE" ]]; then
    echo "ERROR: PeerGateway.js not found:"
    echo "  $PEER_GATEWAY_FILE"
    exit 1
fi

if [[ ! -f "$PATCH_FILE" ]]; then
    echo "ERROR: E1 timing patch not found:"
    echo "  $PATCH_FILE"
    exit 1
fi

echo "[E1] Checking timing patch..."

if patch --dry-run -p0 < "$PATCH_FILE" >/dev/null 2>&1; then
    echo "[E1] Applying timing patch..."
    patch -p0 < "$PATCH_FILE"
else
    if patch --dry-run -R -p0 < "$PATCH_FILE" >/dev/null 2>&1; then
        echo "[E1] Timing patch is already applied."
    else
        echo "ERROR: Timing patch does not apply cleanly."
        echo "Check the pinned @hyperledger/caliper-fabric version."
        exit 1
    fi
fi

echo "[E1] Validating patched connector..."
node -c "$PEER_GATEWAY_FILE"

echo "[E1] Caliper E1 environment is ready."
