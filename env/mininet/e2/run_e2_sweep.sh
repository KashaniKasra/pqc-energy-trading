#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: sudo taskset -c <cpus> $0 <new-output-directory>" >&2
  exit 2
fi
if [[ ${EUID} -ne 0 ]]; then
  echo "E2 scientific sweep requires root for Mininet" >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
output_root=$1
if [[ -e ${output_root} ]]; then
  echo "refusing existing E2 output path: ${output_root}" >&2
  exit 1
fi

configs=(classical uniform_mldsa layer_aware)
transitions=(channel_setup commitment_update htlc_add htlc_settle funding coop_close force_close penalty)
for config in "${configs[@]}"; do
  for transition in "${transitions[@]}"; do
    python3 "${repo_root}/env/mininet/e2/run_e2.py" \
      --config "${config}" \
      --transition "${transition}" \
      --warmup 100 \
      --iterations 1000 \
      --ping-samples 20 \
      --output-root "${output_root}" \
      --scientific
  done
done
