#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 OUTPUT_ROOT ARTIFACT_ROOT" >&2
  exit 2
fi

output_root=$1
artifact_root=$2

if [[ -e "$output_root" || -e "$artifact_root" ]]; then
  echo "refusing existing output or artifact root" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel)
mkdir -p -- "$output_root" "$artifact_root"
cd "$repo_root/src/e5"

for n_states in 10 100 1000 10000 100000; do
  condition_output="$output_root/n${n_states}"
  condition_artifact="$artifact_root/n${n_states}"
  taskset -c 2,4,6,8,10,12,14 \
    go run ./cmd/e5bench \
      --n-states "$n_states" \
      --warmup 100 \
      --iterations 1000 \
      --output-root "$condition_output" \
      --artifact-root "$condition_artifact" \
      --scientific
done
