#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

FABRIC_DIR="${FABRIC_DIR:-$HOME/projects/fabric}"
FABRIC_TAG="v2.5.16"
FABRIC_COMMIT="f871cf92a026aba7b12e6f06d71ded3e6e659d71"

LIBOQS_COMMIT="97f6b86b1b6d109cfd43cf276ae39c2e776aed80"

GO_TOOLCHAIN_CACHE="$HOME/go/pkg/mod/cache/download/golang.org/toolchain/@v/v0.0.1-go1.26.4.linux-amd64.zip"
GO_TOOLCHAIN_SHA256="0221cfe82f9c88c717677cb5c1f59f598d177cd7eac18c11f90ae29432d356f9"
GO_TOOLCHAIN_STAGED="$FABRIC_DIR/go1.26.4-toolchain.zip"

PATCH_FILE="$SCRIPT_DIR/patches/fabric-2.5.16-pq-identities.patch"
IMAGE_PROVENANCE_LABEL="org.pqc-energy-trading.fabric-pq"

PEER_IMAGE="fabric-peer:2.5.16-pq"
ORDERER_IMAGE="fabric-orderer:2.5.16-pq"

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: Required command not found: $1"
        exit 1
    fi
}

require_cmd git
require_cmd docker
require_cmd go
require_cmd sha256sum

if [[ ! -f "$PATCH_FILE" ]]; then
    echo "ERROR: Patch file not found: $PATCH_FILE"
    exit 1
fi

PATCH_SHA256="$(sha256sum "$PATCH_FILE" | awk '{print $1}')"

if [[ ! -d "$FABRIC_DIR/.git" ]]; then
    echo "ERROR: Fabric checkout not found: $FABRIC_DIR"
    exit 1
fi

if [[ ! -f "$GO_TOOLCHAIN_CACHE" ]]; then
    echo "ERROR: Go 1.26.4 toolchain cache not found:"
    echo "  $GO_TOOLCHAIN_CACHE"
    exit 1
fi

ACTUAL_GO_TOOLCHAIN_SHA256="$(sha256sum "$GO_TOOLCHAIN_CACHE" | awk '{print $1}')"

if [[ "$ACTUAL_GO_TOOLCHAIN_SHA256" != "$GO_TOOLCHAIN_SHA256" ]]; then
    echo "ERROR: Go toolchain SHA256 mismatch."
    echo "Expected: $GO_TOOLCHAIN_SHA256"
    echo "Actual:   $ACTUAL_GO_TOOLCHAIN_SHA256"
    exit 1
fi

cd "$FABRIC_DIR"

CURRENT_COMMIT="$(git rev-parse HEAD)"

if [[ "$CURRENT_COMMIT" != "$FABRIC_COMMIT" ]]; then
    echo "ERROR: Fabric checkout must be at:"
    echo "  tag:    $FABRIC_TAG"
    echo "  commit: $FABRIC_COMMIT"
    echo "Current commit: $CURRENT_COMMIT"
    exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "ERROR: Fabric checkout is not clean (tracked or untracked files present)."
    echo "Use a clean Fabric $FABRIC_TAG checkout before running this script."
    exit 1
fi

for path in \
    bccsp/sw/pq.go \
    bccsp/sw/pq_test.go \
    bccsp/sw/pqkey.go \
    bccsp/sw/pqkeystore.go \
    bccsp/sw/pqx509.go \
    vendor/github.com/open-quantum-safe/liboqs-go
do
    if [[ -e "$path" ]]; then
        echo "ERROR: Fabric checkout already contains PQ patch files: $path"
        exit 1
    fi
done

PATCH_APPLIED=false

cleanup() {
    rm -f "$GO_TOOLCHAIN_STAGED"

    if [[ "$PATCH_APPLIED" == "true" ]]; then
        echo "[E1] Restoring clean Fabric checkout..."
        git apply -R "$PATCH_FILE"
    fi
}

trap cleanup EXIT

echo "[E1] Applying Fabric PQ identity patch..."

git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"
PATCH_APPLIED=true

echo "[E1] Fabric patch applied."

echo "[E1] Verifying patched Fabric tests..."

go test ./bccsp/sw ./bccsp/signer ./msp

echo "[E1] Building patched peer image..."

echo "[E1] Staging Go 1.26.4 toolchain for Docker build..."

cp "$GO_TOOLCHAIN_CACHE" "$GO_TOOLCHAIN_STAGED"

docker build \
    -f images/peer/Dockerfile \
    -t "$PEER_IMAGE" \
    --label "$IMAGE_PROVENANCE_LABEL.fabric-commit=$FABRIC_COMMIT" \
    --label "$IMAGE_PROVENANCE_LABEL.liboqs-commit=$LIBOQS_COMMIT" \
    --label "$IMAGE_PROVENANCE_LABEL.patch-sha256=$PATCH_SHA256" \
    --build-arg UBUNTU_VER=22.04 \
    --build-arg TARGETOS=linux \
    --build-arg TARGETARCH=amd64 \
    --build-arg FABRIC_VER=2.5.16 \
    --build-arg GO_VER=1.26.4 \
    --build-arg GO_TAGS="" \
    --build-arg LIBOQS_COMMIT="$LIBOQS_COMMIT" \
    .

echo "[E1] Building patched orderer image..."

docker build \
    -f images/orderer/Dockerfile \
    -t "$ORDERER_IMAGE" \
    --label "$IMAGE_PROVENANCE_LABEL.fabric-commit=$FABRIC_COMMIT" \
    --label "$IMAGE_PROVENANCE_LABEL.liboqs-commit=$LIBOQS_COMMIT" \
    --label "$IMAGE_PROVENANCE_LABEL.patch-sha256=$PATCH_SHA256" \
    --build-arg UBUNTU_VER=22.04 \
    --build-arg TARGETOS=linux \
    --build-arg TARGETARCH=amd64 \
    --build-arg FABRIC_VER=2.5.16 \
    --build-arg GO_VER=1.26.4 \
    --build-arg GO_TAGS="" \
    --build-arg LIBOQS_COMMIT="$LIBOQS_COMMIT" \
    .

echo "[E1] Validating images..."

docker run --rm "$PEER_IMAGE" peer version
docker run --rm "$ORDERER_IMAGE" orderer version

for image in "$PEER_IMAGE" "$ORDERER_IMAGE"; do
    if [[ "$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.fabric-commit\" }}")" != "$FABRIC_COMMIT" ]]; then
        echo "ERROR: Fabric commit provenance label mismatch for $image"
        exit 1
    fi
    if [[ "$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.liboqs-commit\" }}")" != "$LIBOQS_COMMIT" ]]; then
        echo "ERROR: liboqs commit provenance label mismatch for $image"
        exit 1
    fi
    if [[ "$(docker image inspect "$image" --format "{{ index .Config.Labels \"$IMAGE_PROVENANCE_LABEL.patch-sha256\" }}")" != "$PATCH_SHA256" ]]; then
        echo "ERROR: Fabric PQ patch provenance label mismatch for $image"
        exit 1
    fi
done

docker run --rm --entrypoint sh "$PEER_IMAGE" -c \
    'ldd /usr/local/bin/peer | grep -E "liboqs|libcrypto"'

docker run --rm --entrypoint sh "$ORDERER_IMAGE" -c \
    'ldd /usr/local/bin/orderer | grep -E "liboqs|libcrypto"'

echo "[E1] Patched Fabric images built successfully."
echo "[E1] Peer image:    $PEER_IMAGE"
echo "[E1] Orderer image: $ORDERER_IMAGE"
echo "[E1] PQ patch SHA-256: $PATCH_SHA256"
