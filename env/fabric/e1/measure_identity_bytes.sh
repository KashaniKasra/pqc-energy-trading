#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PEER_ORGS="$SCRIPT_DIR/crypto-config/peerOrganizations"

mapfile -t FILES < <(
    find "$PEER_ORGS" \
        -path '*/peers/*/msp/signcerts/*' \
        -type f \
        | sort
)

if [[ "${#FILES[@]}" -ne 4 ]]; then
    echo "ERROR: Expected 4 peer identity files, found ${#FILES[@]}."
    exit 1
fi

total=0

for file in "${FILES[@]}"; do
    size="$(stat -c '%s' "$file")"
    echo "$file: $size bytes"
    total=$((total + size))
done

average=$((total / ${#FILES[@]}))

echo "identity_bytes=$average"
