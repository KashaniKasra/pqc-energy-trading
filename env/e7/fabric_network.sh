#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
E1_DIR="$PROJECT_ROOT/env/fabric/e1"
OVERRIDE="$SCRIPT_DIR/docker-compose.fabric-cpu.yaml"
DOCKER_SHIM_DIR="$SCRIPT_DIR/docker-shim"

usage() { echo "usage: $0 start|stop l1_only|sets_reimpl" >&2; exit 2; }
[[ $# -eq 2 ]] || usage
ACTION="$1"
CONFIG="$2"
case "$CONFIG" in
  l1_only) E1_MODE="ml-dsa-65" ;;
  sets_reimpl) E1_MODE="ecdsa" ;;
  *) usage ;;
esac

case "$ACTION" in
  start)
    # setup_fabric_e1.sh tears down the previous containers and regenerates all
    # ledger/channel material, giving each on-chain E7 config a fresh ledger.
    # It never writes raw/e1; E7 evidence is supplied separately to e7bench.
    REAL_DOCKER="$(command -v docker)"
    if [[ -z "$REAL_DOCKER" || ! -x "$DOCKER_SHIM_DIR/docker" || ! -f "$OVERRIDE" ]]; then
      echo "ERROR: E7 Docker CPU-isolation prerequisites are missing." >&2
      exit 1
    fi
    E7_REAL_DOCKER="$REAL_DOCKER" \
    E7_DOCKER_COMPOSE_OVERRIDE="$OVERRIDE" \
    PATH="$DOCKER_SHIM_DIR:$PATH" \
    E1_CONFIG="$E1_MODE" \
      "$E1_DIR/setup_fabric_e1.sh"
    ;;
  stop)
    docker compose -f "$E1_DIR/docker-compose.yaml" -f "$OVERRIDE" down --remove-orphans
    ;;
  *) usage ;;
esac
