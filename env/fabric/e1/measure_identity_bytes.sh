#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo "Usage: $0 <config> [run_label] [crypto_config_dir]"
    echo "Measures the public-key representation used by Fabric; it does not measure the signcert PEM."
    echo "Example (fixed profile): $0 ecdsa public-key-v1"
    echo "Example (diagnostic):    $0 ecdsa blockutil-public-key-v1"
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

for command_name in awk go realpath sha256sum; do
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
EVIDENCE_MODULE="$PROJECT_ROOT/src/e1/evidence"

if [[ -n "$RUN_LABEL" ]]; then
    RUN_NAMESPACE="${RUN_LABEL}_${CONFIG}"
else
    RUN_NAMESPACE="$CONFIG"
fi

OUTPUT_FILE="$RAW_DIR/${RUN_NAMESPACE}_public_key_bytes.csv"

if [[ -e "$OUTPUT_FILE" ]]; then
    echo "ERROR: Refusing to overwrite existing identity measurement: $OUTPUT_FILE"
    exit 1
fi

if [[ ! -d "$PEER_ORGS" ]]; then
    echo "ERROR: Peer organizations directory not found: $PEER_ORGS"
    exit 1
fi

mkdir -p "$RAW_DIR"
TEMP_OUTPUT="$(mktemp "$RAW_DIR/.${RUN_NAMESPACE}_public_key_bytes.XXXXXX")"
HOST_TEMP_DIR="$(mktemp -d)"
cleanup() {
    rm -f "$TEMP_OUTPUT"
    rm -rf "$HOST_TEMP_DIR"
}
trap cleanup EXIT

if [[ "$CRYPTO_CONFIG_DIR" == "$SCRIPT_DIR/crypto-config" ]]; then
    SOURCE_PREFIX="env/fabric/e1/crypto-config"
else
    SOURCE_PREFIX="generated_crypto_config"
fi

(cd "$EVIDENCE_MODULE" && go build -o "$HOST_TEMP_DIR/identityinspect" ./cmd/identityinspect)
"$HOST_TEMP_DIR/identityinspect" \
    --config "$CONFIG" \
    --run-label "${RUN_LABEL:-canonical}" \
    --crypto-config "$CRYPTO_CONFIG_DIR" \
    --source-prefix "$SOURCE_PREFIX" \
    > "$TEMP_OUTPUT"

ROW_COUNT="$(awk 'END {print NR - 1}' "$TEMP_OUTPUT")"
if [[ "$ROW_COUNT" != "4" ]]; then
    echo "ERROR: Expected four public-key evidence rows, found $ROW_COUNT."
    exit 1
fi

read -r KEY_MEAN KEY_MIN KEY_MAX DISPERSION_PERCENT < <(
    awk -F, '
        NR == 2 { min = max = $7 }
        NR > 1 { total += $7; if ($7 < min) min = $7; if ($7 > max) max = $7 }
        END {
            mean = total / (NR - 1)
            dispersion = mean == 0 ? 0 : 100 * (max - min) / mean
            printf "%.6f %d %d %.6f\n", mean, min, max, dispersion
        }
    ' "$TEMP_OUTPUT"
)

if awk -v dispersion="$DISPERSION_PERCENT" 'BEGIN { exit !(dispersion > 3.0) }'; then
    DISPERSION_FLAG="review_required_over_3_percent"
else
    DISPERSION_FLAG="within_3_percent"
fi

mv "$TEMP_OUTPUT" "$OUTPUT_FILE"
rm -rf "$HOST_TEMP_DIR"
trap - EXIT

echo "Per-peer public-key evidence written:"
echo "  run_namespace=$RUN_NAMESPACE"
echo "  output_file=$OUTPUT_FILE"
echo "  identity_bytes_definition=public key bytes only"
echo "  public_key_bytes_mean=$KEY_MEAN"
echo "  public_key_bytes_min=$KEY_MIN"
echo "  public_key_bytes_max=$KEY_MAX"
echo "  public_key_dispersion_percent=$DISPERSION_PERCENT"
echo "  dispersion_flag=$DISPERSION_FLAG"
column -s, -t "$OUTPUT_FILE" 2>/dev/null || cat "$OUTPUT_FILE"
