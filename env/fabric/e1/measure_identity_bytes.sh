#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "Usage: $0 <config> [run_label] [crypto_config_dir]"
    echo "Example (fixed profile): $0 ecdsa"
    echo "Example (diagnostic):    $0 ecdsa blockutil-300"
    echo "Example (offline tree):  $0 ml-dsa-44 identity-only-v1 /tmp/e1-identity/crypto-config"
    exit 1
fi

CONFIG="$1"
RUN_LABEL="${2:-}"
CRYPTO_CONFIG_ARG="${3:-}"

case "$CONFIG" in
    ecdsa|ml-dsa-44|ml-dsa-65|sphincs)
        ;;
    *)
        echo "ERROR: Unsupported E1 configuration: $CONFIG"
        exit 1
        ;;
esac

if [[ -n "$RUN_LABEL" && ! "$RUN_LABEL" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "ERROR: Run label must contain only lowercase letters, digits, dot, underscore, or hyphen."
    exit 1
fi

for command_name in find openssl realpath sha256sum stat; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $command_name"
        exit 1
    fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -n "$CRYPTO_CONFIG_ARG" ]]; then
    if [[ ! -d "$CRYPTO_CONFIG_ARG" ]]; then
        echo "ERROR: Crypto-config directory not found: $CRYPTO_CONFIG_ARG"
        exit 1
    fi
    CRYPTO_CONFIG_DIR="$(realpath "$CRYPTO_CONFIG_ARG")"
else
    CRYPTO_CONFIG_DIR="$SCRIPT_DIR/crypto-config"
fi
PEER_ORGS="$CRYPTO_CONFIG_DIR/peerOrganizations"
RAW_DIR="$PROJECT_ROOT/raw/e1"

if [[ -n "$RUN_LABEL" ]]; then
    RUN_NAMESPACE="${RUN_LABEL}_${CONFIG}"
else
    RUN_NAMESPACE="$CONFIG"
fi

OUTPUT_FILE="$RAW_DIR/${RUN_NAMESPACE}_identity_bytes.csv"

if [[ -e "$OUTPUT_FILE" ]]; then
    echo "ERROR: Refusing to overwrite existing identity measurement: $OUTPUT_FILE"
    exit 1
fi

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

case "$CONFIG" in
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

mkdir -p "$RAW_DIR"
TEMP_OUTPUT="$(mktemp "$RAW_DIR/.${RUN_NAMESPACE}_identity_bytes.XXXXXX")"
cleanup() {
    rm -f "$TEMP_OUTPUT"
}
trap cleanup EXIT

printf '%s\n' 'config,run_label,peer,identity_type,identity_path,bytes,sha256' > "$TEMP_OUTPUT"

for file in "${FILES[@]}"; do
    cert_text="$(openssl x509 -in "$file" -noout -text)"

    if [[ "$CONFIG" == "ecdsa" ]]; then
        if grep -q '1.3.6.1.3.9999.1' <<< "$cert_text"; then
            echo "ERROR: Identity contains the experimental PQ extension but config is ecdsa: $file"
            exit 1
        fi
    elif ! grep -q '1.3.6.1.3.9999.1' <<< "$cert_text" || ! grep -Fq "$EXPECTED_ALGORITHM" <<< "$cert_text"; then
        echo "ERROR: Identity does not match config $CONFIG ($EXPECTED_ALGORITHM): $file"
        exit 1
    fi

    peer="$(basename "$(dirname "$(dirname "$(dirname "$file")")")")"
    if [[ "$CRYPTO_CONFIG_DIR" == "$SCRIPT_DIR/crypto-config" ]]; then
        relative_path="$(realpath --relative-to="$PROJECT_ROOT" "$file")"
    else
        relative_path="generated_crypto_config/$(realpath --relative-to="$CRYPTO_CONFIG_DIR" "$file")"
    fi
    size="$(stat -c '%s' "$file")"
    digest="$(sha256sum "$file" | awk '{print $1}')"

    printf '%s,%s,%s,%s,%s,%d,%s\n' \
        "$CONFIG" \
        "${RUN_LABEL:-canonical}" \
        "$peer" \
        'msp_signcert_pem' \
        "$relative_path" \
        "$size" \
        "$digest" \
        >> "$TEMP_OUTPUT"
done

mv "$TEMP_OUTPUT" "$OUTPUT_FILE"
trap - EXIT

echo "Per-peer identity evidence written without aggregation:"
echo "  run_namespace=$RUN_NAMESPACE"
echo "  output_file=$OUTPUT_FILE"
column -s, -t "$OUTPUT_FILE" 2>/dev/null || cat "$OUTPUT_FILE"
