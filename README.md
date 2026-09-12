# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical testbed for measuring cryptographic, blockchain,
and communication-network costs. Its governing rule is to report what the system
actually does and retain the raw samples behind every reported value. Unexpected
results and failed requests are evidence and must not be tuned away.

## Current status

| Experiment | Status |
| --- | --- |
| E0 primitives, server | Server measurement complete; specification-referenced sanity checker unavailable |
| E0 primitives, meter/SBC | Pending hardware measurement |
| E1 Fabric | Complete except final block-utilisation treatment for ML-DSA-44, ML-DSA-65, and SPHINCS+ |
| E2, E5, E7, E8, E9 | Not yet completed |

`data/e1_fabric.csv` is intentionally absent until E1 is complete. The
specification refers to a supplied `make_figures.py` sanity checker, but that
source and its plausible bands are absent; no replacement is fabricated.

## Repository layout

```text
README.md
meta.json
env/       Fabric, Caliper, and network configurations
src/       benchmark, analysis, chaincode, identity, and evidence code
data/      derived and final experiment CSVs
raw/       retained raw measurements
figures/   generated final PDFs
```

Raw E1 evidence remains uncompressed while the pending block methodology is
resolved. It must not be deleted or overwritten. `meta.json` contains the
authoritative machine-readable provenance.

## Pinned environment

- Hyperledger Fabric `v2.5.16`, commit
  `f871cf92a026aba7b12e6f06d71ded3e6e659d71`
- Fabric CA `1.5.17`
- Hyperledger Caliper and Fabric connector `0.7.1`
- liboqs `0.15.0`, commit
  `97f6b86b1b6d109cfd43cf276ae39c2e776aed80`
- liboqs-go `v0.15.0`, commit
  `75451133b94a6c4be5f528eef94916ce08475f24`
- Fabric build Go `1.26.4`

Current trace-capable images are `fabric-peer:2.5.16-pq` at
`sha256:3d71da5ac46b179c981527b69ac832cb7905bd3bc428ee6b2620bc580e3c944e`
and `fabric-orderer:2.5.16-pq` at
`sha256:4ec6f0287e3fda35a874357bfe9c9f2ef8b9fbb427a42457de3b6b1b98290058`.
The Fabric patch SHA-256 is
`5268f9f67656233d7e7b9ac7c449f20f05fc8a8257fb91eb0de63698a72d1293`.

## E0 primitives

E0 uses direct liboqs APIs and OpenSSL/libcrypto for ECDSA P-256; it does not
use oqs-provider. The deterministic signature message is
`SHA-256("pqc-energy-trading-e0-message-v1")`. ML-KEM has no ordinary message
input. ECDSA SHA-256 hashing is outside the timed sign/verify interval.

The canonical server run used 100 discarded warm-ups, 10,000 measured
iterations, `GOMAXPROCS=1`, logical CPUs 2 and 3, amd-pstate passive mode,
performance governor, boost disabled, approximately 3.2 GHz fixed frequency,
and AC power. The canonical output is `data/e0_primitives.csv`; compressed raw
samples and the run log are under `raw/e0/`. Results report median, p95, and p99.

Build and validate without measuring:

```bash
cd src/e0
go test ./...
go build -o e0bench .
```

The controlled human measurement entry point is `src/e0/run_e0_server.sh`.
Do not rerun the canonical server measurement automatically. The meter/SBC run
remains pending and must not be extrapolated from the server.

## E1 Fabric testbed

E1 uses Fabric 2.5 with two organizations, two peers per organization, one
etcdraft/Raft orderer, channel `energychannel`, and a simple key/value
chaincode. The benchmarked operation is `Set(key,value)`; no `Get` latency or
throughput is claimed. The testbed models the communication/blockchain system,
not an electricity market.

Fabric parameters are identical across configurations:

| Parameter | Value |
| --- | ---: |
| `BatchTimeout` | 2 s |
| `MaxMessageCount` | 500 |
| effective `PreferredMaxBytes` | 2,097,152 bytes |
| effective `AbsoluteMaxBytes` | 10,485,760 bytes |
| endorsement policy | Application `ImplicitMeta MAJORITY` |
| empirical endorsements per transaction | 2 |

### Experimental PQ identity scope

The patch implements an experimental hybrid mechanism. Standard ECDSA X.509
membership and CA signatures remain. Noncritical experimental OID
`1.3.6.1.3.9999.1` carries the PQ algorithm and raw public key; PQ SKI is
`SHA-256(raw PQ public key)`. Patched BCCSP uses liboqs for the peer
transaction/endorsement signing and verification path.

This is not standardized PQ X.509, full post-quantum PKI, or native Fabric PQ
support. Caliper client, TLS, and orderer identities remain ECDSA. Retained
one-shot runtime audits prove successful `oqs.Signature.Verify` dispatch for
ML-DSA-44, ML-DSA-65, and `SPHINCS+-SHA2-128s-simple` on the orderer and peers
from both organizations. `TestPQVerifierDispatchRejectsClassicalFallback`
proves that tampered PQ signatures and ECDSA signatures presented to PQ keys
are rejected without silent classical fallback. Trace logging is disabled and
rejected during performance runs.

### Common-profile latency and saturation

The immutable common profile is `env/caliper/e1/benchmark.yaml`, SHA-256
`5ae8aa973467a7828237fbc097c8e3936d5a05196de3cfd2881f550b8afd6e1a`:
20 seconds at 50 TPS as discarded warm-up, then 120 seconds at 50 TPS and
120 seconds at 200 TPS. Each retained clean run started from a fresh height-7
ledger under the controlled CPU state. The professor selected 200 TPS for the
final latency fields; 50 TPS remains supporting evidence.

| Configuration | success/total at 200 TPS | endorse median / p95 / p99 (ms) | commit median / p95 / p99 (ms) |
| --- | ---: | ---: | ---: |
| ECDSA | 24,001/24,001 | 3.631845 / 29.692575 / 51.665168 | 1210.965735 / 2062.837374 / 2174.454991 |
| ML-DSA-44 | 24,000/24,000 | 4.034757 / 21.290463 / 33.333694 | 473.614710 / 905.356157 / 955.036991 |
| ML-DSA-65 | 24,001/24,001 | 4.047892 / 23.881626 / 36.930557 | 445.157309 / 725.150242 / 769.665123 |

SPHINCS+ saturated under the unchanged common profile: 462/6,001 succeeded at
50 TPS and 532/24,001 at 200 TPS. These outcomes are retained as saturation
evidence; they are not valid normal-latency populations.

The schema alias `commit_*_ms` means
`post_endorsement_submit_to_commit_status_ms`: timing begins after endorsement,
immediately before `transaction.submit()`, and ends after `subtx.getStatus()`.
It includes client signing and RPC waiting, orderer submission/acknowledgement,
block-cut or BatchTimeout waiting, validation, ledger commit, and status
delivery. It excludes proposal construction, endorsement, and Caliper
scheduling before connector invocation. It is not pure commit-processing or
primitive-crypto time.

### Identity, transaction size, and sustainable rate

Professor-defined `identity_bytes` is the public-key representation used by
Fabric, excluding certificate and signature bytes. Four peer values are
retained; mean, min, max, and dispersion are validated. All four have zero
dispersion.

`tx_bytes_mean` is the arithmetic mean of exact serialized `common.Envelope`
lengths extracted from fetched blocks, never block bytes divided by transaction
count. Embedded endorsements are counted for every retained transaction.

| Configuration | identity bytes | tx samples | tx_bytes_mean | endorsements/tx | tps_sustained |
| --- | ---: | ---: | ---: | ---: | ---: |
| ECDSA P-256 DER SPKI | 91 | 6,001 | 3,929.301450 | 2 | 228 |
| ML-DSA-44 raw key | 1,312 | 6,001 | 12,288.617897 | 2 | 356 |
| ML-DSA-65 raw key | 1,952 | 6,001 | 15,803.491918 | 2 | 344 |
| SPHINCS+-SHA2-128s-simple raw key | 32 | 1,201 | 19,711.570358 | 2 | 22 |

A 60-second offered rate is sustainable only when all gates pass:

- `tx_success_rate >= 0.99`;
- `success_count / 60 >= 0.95 * configured offered TPS`;
- at least five successful samples occur in both request-start windows
  `[0,12000)` ms and `[48000,60000)` ms;
- ending-window p95 is at most twice beginning-window p95;
- percentiles use linear interpolation at rank `p*(n-1)`.

Final adjacent PASS/FAIL pairs are 228/229 for ECDSA, 356/357 for ML-DSA-44,
344/345 for ML-DSA-65, and 22/23 for SPHINCS+. The professor-required SPHINCS+
1, 2, 5, 10, and 20 TPS sweep is retained separately; its 60-second low-rate
rounds intentionally contain fewer than 1,000 requests.

### Block utilisation

```text
block_utilisation = mean(actual fetched ordinary-transaction block bytes)
                    / 2,097,152
```

Genesis, config, and other nonordinary blocks are excluded; terminal ordinary
blocks are retained. The professor requires blocks to fill by traffic volume
rather than close prematurely because of `BatchTimeout`. Treatment of mixed
populations containing both volume-filled and periodic `BatchTimeout` residual
blocks remains pending professor clarification.

ECDSA final evidence is `blockutil-300_ecdsa`, blocks 17-48: 32 ordinary
blocks, `block_bytes_mean=1952347.156250`, and
`block_utilisation=0.930951670`. Blocks 17-47 reached 500 transactions; block
48 is the retained terminal 386-transaction block.

ML-DSA-44, ML-DSA-65, and SPHINCS+ block treatment remains pending professor
clarification. Their retained candidate/diagnostic block evidence must not be
promoted or deleted in the meantime.

## Reproduction and deterministic analysis

Install Caliper and its retained timing patch:

```bash
cd env/caliper/e1
./setup_caliper_e1.sh
```

Rebuild the patched Fabric images only when required:

```bash
cd env/fabric/e1
./build_fabric_pq.sh
```

Before controlled E1 timing runs, use AC power, close heavy applications,
ensure low load and no unrelated Docker containers, then run
`sudo env/caliper/e1/setup_cpu_e1.sh`. Required state is CPUs 2-15,
amd-pstate passive, performance governor, boost off, and min=max=3,201,000 kHz.
The runner verifies and records this state, provisions a fresh matching network,
checks the scientific profile/configuration allowlist, and refuses namespace
collisions.

Run an immutable profile with a unique label:

```bash
cd env/caliper/e1
E1_BENCHCONFIG=<allowlisted-profile.yaml> \
E1_RUN_LABEL=<unique-label> \
E1_RUN_TYPE=<fixed-profile|sustainability|sweep|evidence|diagnostic> \
./run_e1.sh <ecdsa|ml-dsa-44|ml-dsa-65|sphincs>
```

For transaction/block evidence, run
`env/fabric/e1/measure_block_bytes.sh` against that same live ledger before
teardown or setup of another configuration.

Deterministic checks and regeneration:

```bash
python3 -m json.tool meta.json >/dev/null
python3 -m unittest src/e1/test_analyze_timings.py
bash env/caliper/e1/test_run_policy.sh
./src/e1/analyze_timings.py --identity-public-keys
./src/e1/analyze_timings.py --endorsement-policy
./src/e1/analyze_timings.py --pq-verification-audits
./src/e1/analyze_timings.py --clean-latency-professor-review
./src/e1/analyze_timings.py --working-e1
```

`--working-e1` is stdout-only and leaves unresolved block fields empty; it
cannot be mistaken for `data/e1_fabric.csv`. Raw CSV/log evidence will be
gzip-compressed only after the E1 KEEP set and pending block methodology are
frozen, with provenance hashes and readers updated together.

Falcon-512 in liboqs 0.15.0 is the round-3 implementation and must not be
described as final FIPS 206.
