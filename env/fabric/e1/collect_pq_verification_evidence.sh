#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FABRIC_SOURCE_DIR="${FABRIC_DIR:-$HOME/projects/fabric}"
FABRIC_COMMIT="f871cf92a026aba7b12e6f06d71ded3e6e659d71"
LIBOQS_COMMIT="97f6b86b1b6d109cfd43cf276ae39c2e776aed80"
IMAGE_PROVENANCE_LABEL="org.pqc-energy-trading.fabric-pq"

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 {ml-dsa-44|ml-dsa-65|sphincs} <run-label>"
    exit 1
fi

CONFIG="$1"
RUN_LABEL="$2"

case "$CONFIG" in
    ml-dsa-44)
        ALGORITHM="ML-DSA-44"
        ;;
    ml-dsa-65)
        ALGORITHM="ML-DSA-65"
        ;;
    sphincs)
        ALGORITHM="SPHINCS+-SHA2-128s-simple"
        ;;
    *)
        echo "ERROR: PQ verification evidence requires ml-dsa-44, ml-dsa-65, or sphincs."
        exit 1
        ;;
esac

if [[ ! "$RUN_LABEL" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "ERROR: run label must contain only lowercase letters, digits, dot, underscore, or hyphen."
    exit 1
fi

for command_name in date docker git grep jq realpath sed sha256sum tee; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $command_name"
        exit 1
    fi
done

if [[ ! -d "$FABRIC_SOURCE_DIR/.git" ]]; then
    echo "ERROR: Fabric source checkout not found: $FABRIC_SOURCE_DIR"
    exit 1
fi

if [[ "$(git -C "$FABRIC_SOURCE_DIR" rev-parse HEAD)" != "$FABRIC_COMMIT" ]] ||
   [[ -n "$(git -C "$FABRIC_SOURCE_DIR" status --porcelain --untracked-files=normal)" ]]; then
    echo "ERROR: Fabric source must be the clean pinned commit $FABRIC_COMMIT."
    exit 1
fi

PATCH_FILE="$SCRIPT_DIR/patches/fabric-2.5.16-pq-identities.patch"
PATCH_SHA256="$(sha256sum "$PATCH_FILE" | awk '{print $1}')"
PEER_IMAGE="fabric-peer:2.5.16-pq"
ORDERER_IMAGE="fabric-orderer:2.5.16-pq"

for image in "$PEER_IMAGE" "$ORDERER_IMAGE"; do
    IMAGE_PATCH_SHA="$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.patch-sha256\" }}")"
    IMAGE_FABRIC_COMMIT="$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.fabric-commit\" }}")"
    IMAGE_LIBOQS_COMMIT="$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.liboqs-commit\" }}")"

    if [[ "$IMAGE_PATCH_SHA" != "$PATCH_SHA256" ||
          "$IMAGE_FABRIC_COMMIT" != "$FABRIC_COMMIT" ||
          "$IMAGE_LIBOQS_COMMIT" != "$LIBOQS_COMMIT" ]]; then
        echo "ERROR: $image was not built from the current pinned PQ patch/source provenance."
        echo "Rebuild both images with: $SCRIPT_DIR/build_fabric_pq.sh"
        exit 1
    fi
done

RUN_NAMESPACE="${RUN_LABEL}_${CONFIG}"
RAW_DIR="$PROJECT_ROOT/raw/e1"
LOG_FILE="$RAW_DIR/${RUN_NAMESPACE}_pq_verification_audit.log"
CSV_FILE="$RAW_DIR/${RUN_NAMESPACE}_pq_verification_audit.csv"

mkdir -p "$RAW_DIR"
if compgen -G "$RAW_DIR/${RUN_NAMESPACE}_*" >/dev/null; then
    echo "ERROR: Existing raw artifacts found for namespace: $RUN_NAMESPACE"
    echo "Refusing to overwrite or mix verification evidence."
    exit 1
fi

PROJECT_GIT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
PROJECT_GIT_STATUS="$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=normal)"
PROJECT_GIT_STATUS_SHA256="$(printf '%s' "$PROJECT_GIT_STATUS" | sha256sum | awk '{print $1}')"
PROJECT_TRACKED_STATUS="$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=no)"
PROJECT_TRACKED_STATUS_SHA256="$(printf '%s' "$PROJECT_TRACKED_STATUS" | sha256sum | awk '{print $1}')"
PROJECT_UNTRACKED_PATHS="$(git -C "$PROJECT_ROOT" ls-files --others --exclude-standard | sort)"
PROJECT_UNTRACKED_PATHS_SHA256="$(printf '%s' "$PROJECT_UNTRACKED_PATHS" | sha256sum | awk '{print $1}')"
if [[ -n "$PROJECT_TRACKED_STATUS" ]]; then
    echo "ERROR: Commit or otherwise resolve tracked project changes before collecting verification evidence."
    exit 1
fi
PEER_IMAGE_ID="$(docker image inspect "$PEER_IMAGE" --format '{{.Id}}')"
ORDERER_IMAGE_ID="$(docker image inspect "$ORDERER_IMAGE" --format '{{.Id}}')"
EXPECTED_PEER_IMAGE_ID="$(jq -r '.e1_benchmark.installed_images_at_metadata_update.peer' "$PROJECT_ROOT/meta.json")"
EXPECTED_ORDERER_IMAGE_ID="$(jq -r '.e1_benchmark.installed_images_at_metadata_update.orderer' "$PROJECT_ROOT/meta.json")"
if [[ "$PEER_IMAGE_ID" != "$EXPECTED_PEER_IMAGE_ID" ||
      "$ORDERER_IMAGE_ID" != "$EXPECTED_ORDERER_IMAGE_ID" ]]; then
    echo "ERROR: Installed image IDs do not match the current meta.json provenance checkpoint."
    echo "Expected peer=$EXPECTED_PEER_IMAGE_ID orderer=$EXPECTED_ORDERER_IMAGE_ID"
    echo "Observed peer=$PEER_IMAGE_ID orderer=$ORDERER_IMAGE_ID"
    exit 1
fi

exec > >(tee "$LOG_FILE") 2>&1

echo "[E1-PQ-VERIFY] started_at=$(date --iso-8601=seconds)"
echo "[E1-PQ-VERIFY] run_namespace=$RUN_NAMESPACE"
echo "[E1-PQ-VERIFY] configuration=$CONFIG"
echo "[E1-PQ-VERIFY] algorithm=$ALGORITHM"
echo "[E1-PQ-VERIFY] purpose=functional_verification_path_audit_not_performance_measurement"
echo "[E1-PQ-VERIFY] controlled_cpu_or_ac_required=false"
echo "[E1-PQ-VERIFY] project_git_commit=$PROJECT_GIT_COMMIT"
echo "[E1-PQ-VERIFY] project_git_status_sha256_before_log_creation=$PROJECT_GIT_STATUS_SHA256"
echo "[E1-PQ-VERIFY] project_tracked_state_before_log_creation=clean"
echo "[E1-PQ-VERIFY] project_tracked_status_sha256_before_log_creation=$PROJECT_TRACKED_STATUS_SHA256"
echo "[E1-PQ-VERIFY] project_untracked_paths_sha256_before_log_creation=$PROJECT_UNTRACKED_PATHS_SHA256"
echo "[E1-PQ-VERIFY] fabric_source_commit=$FABRIC_COMMIT"
echo "[E1-PQ-VERIFY] liboqs_commit=$LIBOQS_COMMIT"
echo "[E1-PQ-VERIFY] fabric_pq_patch_sha256=$PATCH_SHA256"
echo "[E1-PQ-VERIFY] peer_image_id=$PEER_IMAGE_ID"
echo "[E1-PQ-VERIFY] orderer_image_id=$ORDERER_IMAGE_ID"
echo "[E1-PQ-VERIFY] negative_regression_test=TestPQVerifierDispatchRejectsClassicalFallback"

echo "[E1-PQ-VERIFY] Provisioning a fresh matching network for the functional smoke transaction..."
FABRIC_PQ_VERIFY_TRACE=1 E1_CONFIG="$CONFIG" "$SCRIPT_DIR/setup_fabric_e1.sh"

TRACE_FILE="$(mktemp)"
trap 'rm -f "$TRACE_FILE"' EXIT

for container in \
    orderer.example.com \
    peer0.org1.example.com \
    peer1.org1.example.com \
    peer0.org2.example.com \
    peer1.org2.example.com
do
    docker logs --timestamps "$container" 2>&1 \
        | grep 'E1_PQ_VERIFY_TRACE' \
        | sed "s/^/${container}\t/" \
        >> "$TRACE_FILE" || true
done

if ! grep -Fq "algorithm=$ALGORITHM result=success" "$TRACE_FILE"; then
    echo "ERROR: No successful $ALGORITHM liboqs verification trace was observed."
    exit 1
fi

for organization in org1 org2; do
    if ! grep -E "^peer[01]\.${organization}\.example\.com[[:space:]].*algorithm=${ALGORITHM//+/\\+} result=success" "$TRACE_FILE" >/dev/null; then
        echo "ERROR: No successful $ALGORITHM verification trace was observed on a peer in $organization."
        exit 1
    fi
done

printf '%s\n' \
    'config,algorithm,container,implementation,function,result,trace_line_sha256' \
    > "$CSV_FILE"

while IFS=$'\t' read -r container trace_line; do
    result="$(sed -n 's/.* result=\([^ ]*\).*/\1/p' <<< "$trace_line")"
    trace_sha256="$(printf '%s' "$trace_line" | sha256sum | awk '{print $1}')"
    printf '%s,%s,%s,liboqs,oqs.Signature.Verify,%s,%s\n' \
        "$CONFIG" "$ALGORITHM" "$container" "$result" "$trace_sha256" \
        >> "$CSV_FILE"
done < "$TRACE_FILE"

echo "[E1-PQ-VERIFY] Retained one-shot runtime traces:"
cat "$TRACE_FILE"
echo "[E1-PQ-VERIFY] evidence_csv=$CSV_FILE"
echo "[E1-PQ-VERIFY] evidence_csv_sha256=$(sha256sum "$CSV_FILE" | awk '{print $1}')"
echo "[E1-PQ-VERIFY] completed_at=$(date --iso-8601=seconds)"
echo "[E1-PQ-VERIFY] Network remains running so the evidence can be inspected before deliberate teardown."
