# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical measurement package for a research testbed. Its governing principle is to measure what the implemented system actually does, retain the raw evidence, and report surprising results rather than tune them toward an analytical model.

`docs/student_spec(1).pdf` is the primary implementation specification. A later direct professor clarification overrides it only for the clarified point; the proposal is background and lower authority.

## Current status

- E0 server primitive measurements are complete: 21 scheme/operation rows, 10,000 retained samples per row, and median/p95/p99 summaries. Meter/SBC measurements and the supplied sanity-gate implementation are still missing.
- E1 is functionally implemented for ECDSA, ML-DSA-44, ML-DSA-65, and SPHINCS+-SHA2-128s-simple using one Fabric topology and one fixed Caliper profile.
- Candidate fixed-profile data exist for ECDSA, ML-DSA-44, and ML-DSA-65. They pass sample-count and zero-failure checks, but their retained logs predate full preflight capture, so they are not silently promoted to final data.
- The identical-profile SPHINCS+ run saturated and failed heavily. Its raw samples, 50-TPS success/error rates, and saturation status are reportable, but the run is excluded from normal latency. The supplemental sweep and integer boundary probes establish 22 TPS as the highest tested sustainable integer rate and 23 TPS as the first nonsustainable integer rate; they do not replace the common-profile result.
- `identity_bytes` now means public-key bytes only. The four peer values are 91 bytes (ECDSA DER SubjectPublicKeyInfo), 1312 (ML-DSA-44), 1952 (ML-DSA-65), and 32 (SPHINCS+) within each configuration. Historical signcert-size evidence remains preserved but is supporting evidence, not `identity_bytes`.
- The ECDSA block-filling probe provides the accepted boundary-inclusive block-utilisation value `0.930951670`. A separate same-ledger 50-TPS evidence run supports exact `tx_bytes_mean=3929.301450` and empirical `endorsements_per_tx=2.000000`; its timeout-driven rho is diagnostic-only. Final latency population remains unresolved, so `data/e1_fabric.csv` has not been created.
- E2 and later experiments are not implemented.

No final figure should be produced from unresolved E1 data.

## Repository layout

```text
data/                 validated or explicitly labelled candidate summaries
docs/                 student specification and proposal
env/caliper/e1/       pinned Caliper environment, workload, timing patch, runners
env/fabric/e1/        Fabric topology, hybrid-PQ patch/build, setup, evidence tools
env/mininet/          E9 communication-network environment (not crypto timing)
figures/              generated figures when their source data are valid
raw/e0/               retained compressed E0 samples and canonical server log
raw/e1/               fixed-profile, failed, diagnostic, and archived E1 evidence
src/e0/               E0 primitive benchmark
src/e1/chaincode/     E1 Set/Get chaincode only
src/e1/evidence/      public-key and serialized-block evidence inspectors
src/e1/pqidentity/    experimental PQ peer-identity generator
src/e1/analyze_timings.py
meta.json             version pins and experiment-specific provenance
```

Generated Fabric crypto material, channel artifacts, chaincode packages, Caliper reports, and `node_modules` are excluded. Raw experiment CSVs and logs are deliberately visible to Git. Classify provenance before staging them; do not delete failed runs. Large raw samples may be gzip-compressed only after classification, with their traceability preserved.

## Exact software pins

The principal pins are:

- Hyperledger Fabric `v2.5.16`, commit `f871cf92a026aba7b12e6f06d71ded3e6e659d71`
- Fabric CA `v1.5.17`
- Fabric samples commit `05edea01d4cf24dd4087bd3750c36e690dc4d6ff`
- liboqs `0.15.0`, commit `97f6b86b1b6d109cfd43cf276ae39c2e776aed80`
- liboqs-go `v0.15.0`, commit `75451133b94a6c4be5f528eef94916ce08475f24`
- Hyperledger Caliper and Fabric connector `0.7.1`
- `@hyperledger/fabric-gateway` `1.7.1`, `@grpc/grpc-js` `1.13.1`
- Node.js `22.23.2`, npm `12.0.2`
- Mininet `2.3.0`

Fabric is built with Go `1.26.4`; this is distinct from the system Go version used by E0 and other tools. Historical experiment versions and current-host observations are separate in `meta.json` and must not be conflated.

## E0: primitive measurements

E0 calls liboqs directly; it does not use oqs-provider, Fabric, Caliper, or Mininet. ECDSA P-256 uses OpenSSL `libcrypto` with `prime256v1`, and SHA-256 hashing is outside the timed ECDSA sign/verify call.

The measured parameter strings are:

- `ML-KEM-768`
- `ML-DSA-44`, `ML-DSA-65`, `ML-DSA-87`
- `Falcon-512`
- `SPHINCS+-SHA2-128s-simple`
- ECDSA `prime256v1`

The SPHINCS+ choice is the small-signature 128s variant, not 128f. Falcon in liboqs 0.15.0 is the round-3 implementation and is not represented as final FIPS 206.

Signature schemes use the deterministic 32-byte message:

```text
SHA-256("pqc-energy-trading-e0-message-v1")
```

ML-KEM has no ordinary message input. The canonical server run used 100 discarded warm-up iterations, 10,000 retained iterations per data point, `GOMAXPROCS=1`, logical CPUs 2 and 3 (SMT siblings), approximately 3.2 GHz, amd-pstate passive mode, the performance governor, boost disabled, AC power, and reduced background load.

Outputs:

- summary: `data/e0_primitives.csv`
- raw samples: `raw/e0/e0_server_*.csv.gz`
- retained historical log: `raw/e0/e0_server_run.log`

The historical log prints the old pre-move `raw/` paths. The retained files are now under `raw/e0/`; the evidence was moved, not regenerated. Current code resolves repository-relative output and writes future samples to `raw/e0/`.

Build-only validation (not a measurement):

```bash
cd src/e0
export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
go test ./...
go build -o e0bench .
```

Do not rerun the canonical server benchmark casually: it overwrites same-named E0 sources and requires a controlled human-run environment. Meter/SBC work requires the missing hardware metadata and a separate controlled run.

## E1 topology and hybrid-PQ scope

E1 uses Fabric 2.5.16 with:

- two organizations and two peers per organization;
- one Raft/`etcdraft` orderer (never Solo);
- channel `energychannel`;
- the minimal `simplekv` chaincode and `Set(key,value)` workload only.

Stock Fabric 2.5 does not provide the required PQ peer signing path. The committed patch implements an **experimental hybrid identity mechanism**:

- ordinary ECDSA X.509 membership and CA validation remain;
- a noncritical extension contains the PQ algorithm identifier and public key;
- the ECDSA CA signature binds that extension;
- patched BCCSP import, keystore, signing, and verification use actual liboqs operations;
- PQ SKI is SHA-256 of the raw PQ public key;
- experimental OID `1.3.6.1.3.9999.1` is a testbed identifier, not a standardized assignment.

This is not full PQ PKI, standardized PQ X.509, or native Fabric PQ identity support. The Caliper `User1` client, TLS identities, and orderer identity remain ECDSA. Both peer and orderer **binaries** are patched because the orderer must verify requests signed by PQ peer identities. All configurations, including ECDSA, use the same patched images.

The E1 specification row `SLH-DSA` maps in this implementation to the exact liboqs string `SPHINCS+-SHA2-128s-simple`.

## Rebuild the patched Fabric images

The build uses a clean `~/projects/fabric` checkout at the exact tag/commit above:

```bash
cd env/fabric/e1
./build_fabric_pq.sh
```

The script validates pins, applies the repository patch temporarily, runs the narrow Fabric tests, builds both images, checks versions/dynamic libraries, and reverses the patch on exit.

The committed Fabric patch has SHA-256 `2eeee5582fabd90bb9e675e98bce3daf94a087d2d6848ccfa720f7216e23550f`. The currently verified rebuilt images are:

```text
fabric-peer:2.5.16-pq    sha256:6fe74f12e91ab73a07b13d4a7d2954df8a2ad5a6e00ea2665cfffed7f34f5448
fabric-orderer:2.5.16-pq sha256:b56ec95ae9168725b8c8e1dc4bc4126f30f2b661e3dc99dd72225dc87b1f3d1f
```

Both report Fabric 2.5.16 commit `f871cf9`, were built with Go 1.26.4, and dynamically link to liboqs and libcrypto. These are observed image IDs for the present machine/checkpoint; future rebuilds must record their own IDs.

Current reproducibility caveat: the build expects this cached Go toolchain archive:

```text
~/go/pkg/mod/cache/download/golang.org/toolchain/@v/v0.0.1-go1.26.4.linux-amd64.zip
SHA-256 0221cfe82f9c88c717677cb5c1f59f598d177cd7eac18c11f90ae29432d356f9
```

No untested acquisition URL is claimed. After any patch change, rebuild the images and use the new image IDs recorded by `run_e1.sh`; old IDs must not be attributed to a new run.

## Fabric setup

`setup_fabric_e1.sh` accepts exactly:

```text
ecdsa | ml-dsa-44 | ml-dsa-65 | sphincs
```

For example:

```bash
cd env/fabric/e1
E1_CONFIG=ml-dsa-44 ./setup_fabric_e1.sh
```

It creates fresh generated state, injects real PQ peer identities before channel-artifact generation where applicable, starts the common topology, creates/joins the channel, applies anchor updates, deploys `simplekv`, smoke-tests Set/Get, and checks discovery. All four configurations have passed this functional setup; that does not mean all performance measurements are final.

## Caliper setup and fixed profile

Install the exact locked packages and reproducibly apply the timing patch:

```bash
cd env/caliper/e1
./setup_caliper_e1.sh
```

`benchmark.yaml` is the identical fixed profile for all four configurations:

- warm-up: 20 seconds at 50 TPS (raw timing not written);
- 120 seconds at 50 TPS;
- 120 seconds at 200 TPS;
- one worker and the same `workload/set.js` module.

The connector records endorsement around `proposal.endorse()` and commit from submission through transaction status/commit completion. It also records each future request's monotonic start offset, end-to-end latency, outcome, and transaction ID. End-to-end timing begins immediately before endorsement and ends at successful commit status or the caught failure. `process.hrtime.bigint()` is used.

The chaincode definition does not pass a custom endorsement-policy flag, so Fabric applies `/Channel/Application/Endorsement`. In `configtx.yaml` this is `ImplicitMeta MAJORITY Endorsement`; with Org1 and Org2, whose subpolicies each require one peer, the configured minimum is two endorsements per transaction (one from each organization). A read-only decode of the generated channel block confirmed MAJORITY plus one-peer Org1 and Org2 subpolicies. Future block evidence also records the actual endorsement count in every serialized transaction.

Validate the committed policy path and its source hashes with `./src/e1/analyze_timings.py --endorsement-policy`.

Before a human-controlled final or diagnostic run, connect AC power, close browsers/heavy applications, remove unrelated load, and prepare/verify:

```bash
cd env/caliper/e1
sudo ./setup_cpu_e1.sh
```

Required state: amd-pstate `passive`, boost `0`, governor `performance`, and min/max `3201000` kHz on CPUs 2–15. Fabric containers and Caliper use CPU set 2–15. CPU state may reset after logout/reboot.

A normal fixed-profile command is:

```bash
cd env/caliper/e1
./run_e1.sh ecdsa
```

With no label, the historical namespace and default profile are unchanged (`ecdsa_*`, `ml-dsa-44_*`, etc.). The runner refuses to overwrite any artifact in that namespace. It now retains configuration, run type/label, benchmark path/hash, time, project commit/pre-log worktree state, host/kernel state, per-CPU state, Fabric source state, image IDs, relevant Fabric/Caliper source hashes, setup output, and decoded effective block parameters in the run log. Tracked source state is recorded separately from untracked paths, so the intentionally untracked `raw/e1/` evidence does not obscure whether tracked code matches the recorded commit.

## Isolated block-utilisation probe

`benchmark_blockutil.yaml` is separate from the fixed profile. Its retained 300 TPS ECDSA run was a block-filling probe only. It produced 15,787 successes and 2,214 failures with Gateway concurrency-limit errors, so it is not valid latency or `tps_sustained` evidence.

The isolated command is:

```bash
cd env/caliper/e1
E1_BENCHCONFIG=benchmark_blockutil.yaml \
E1_RUN_LABEL=blockutil-300 \
E1_RUN_TYPE=diagnostic \
./run_e1.sh ecdsa
```

This produces only `blockutil-300_ecdsa_*` artifacts, so it cannot collide with canonical `ecdsa_*` heights, timings, or logs. Diagnostic timing filenames carry the same namespace and cannot masquerade as fixed-profile latency samples.

While that exact network is still running, retain and classify its blocks with:

```bash
cd env/fabric/e1
./measure_block_bytes.sh ecdsa blockutil-300 blockutil-300-tps
```

The tool derives the range from that run's before/after heights, rejects block 0, decodes each block, records every block's byte size/header types/classification, explicitly excludes config/other blocks, and writes raw and summary CSVs. It decodes effective `PreferredMaxBytes` from the current Fabric config block; it does not hard-code the denominator in the script.

For future collections it also writes a per-transaction CSV. The byte value is exactly `len(Block.Data.Data[i])`, i.e. the serialized `common.Envelope` stored in the block—not `block_bytes / transaction_count`. Each row retains the envelope SHA-256, transaction ID, validation code, and actual endorsement count. `tx_bytes_mean` is computed over the serialized endorser-transaction envelopes in accepted ordinary blocks.

The historical ECDSA block evidence predates this envelope extraction, and no serialized historical blocks were retained, so its `tx_bytes_mean` could not be reconstructed. The replacement `benchmark_tx_evidence_50.yaml` run used exactly the warm-up plus 50-TPS prefix of the unchanged common profile and omitted only the unnecessary 200-TPS round. The 120-second population retained 6,001 transactions, preserved the common workload's transaction payload sequence, and avoided making transaction-size evidence depend on the unresolved final latency-population choice. This profile is restricted to evidence-labelled ECDSA, ML-DSA-44, and ML-DSA-65 runs; SPHINCS+ is excluded because it already has exact-envelope evidence. Evidence runs do not replace historical latency candidates.

The same-ledger extraction under namespace `tx-evidence-50-v1_ecdsa` found 6,001 exact serialized envelopes with `tx_bytes_mean=3929.301450`. All 6,001 transactions contain exactly two embedded endorsements, giving mean/min/max `2.000000/2/2`. Validation metadata is unavailable for all rows because orderer-sourced blocks lack the peer-added transaction filter; unavailable is not invalid and does not affect either measurement.

Its 50-TPS blocks are BatchTimeout-driven: the 120-second interval contains 60 ordinary blocks, 59 of which hold 100–102 transactions, plus a 36-transaction terminal residual. No block approaches `MaxMessageCount=500`. Therefore `block_bytes_mean=394327.216667` and rho `0.188029869` are diagnostic-only and do not replace the accepted ECDSA block-filling result. The independently regenerated evidence summary is `data/e1_ecdsa_transaction_evidence.csv`.

The matching ML-DSA-44 evidence run retained 6,001 exact serialized envelopes with `tx_bytes_mean=12288.617897`. Every transaction contains exactly two embedded endorsements, giving mean/min/max `2.000000/2/2`. Its 60 blocks over 120 seconds are also BatchTimeout-driven: 59 nonterminal blocks contain 101–102 transactions and the terminal residual contains 34. Therefore its `block_bytes_mean=1230394.033333` and rho `0.586697594` are diagnostic-only, while its transaction-size and endorsement results are eligible for final E1 use. Validation metadata is unavailable—not invalid—for all 6,001 orderer-sourced envelopes. The independently regenerated summary is `data/e1_ml_dsa_44_transaction_evidence.csv`.

The matching ML-DSA-65 evidence run retained 6,001 exact serialized envelopes with `tx_bytes_mean=15803.491918`. Every transaction contains exactly two embedded endorsements, giving mean/min/max `2.000000/2/2`. Its 60 blocks over 120 seconds are BatchTimeout-driven: 59 nonterminal blocks contain 100–102 transactions and the terminal residual contains 44. Therefore its `block_bytes_mean=1581940.150000` and rho `0.754327846` are diagnostic-only, while its transaction-size and endorsement results are eligible for final E1 use. Validation metadata is unavailable—not invalid—for all 6,001 orderer-sourced envelopes. The run recorded a dirty tracked state limited to README, metadata, and post-processing analyzer changes; all measurement-relevant sources matched recorded commit `82dde3a`. The independently regenerated summary is `data/e1_ml_dsa_65_transaction_evidence.csv`.

After collection, independently reconcile the per-transaction CSV and its summary with:

```bash
./src/e1/analyze_timings.py --block-transactions raw/e1/<run>_blocks_<round>_summary.csv
```

Professor-defined calculation:

```text
block_utilisation = mean(accepted ordinary-transaction block bytes)
                    / effective PreferredMaxBytes
```

The generated config decodes `2 MB` to 2,097,152 bytes. `AbsoluteMaxBytes` (10,485,760 bytes) is not the denominator. Genesis and config blocks are excluded.

For the retained ECDSA probe, blocks 17–47 each contain exactly `MaxMessageCount=500` ordinary transactions. This is the documented empirical block-filling result: the steady-state active block-cutting constraint was MaxMessageCount, not `BatchTimeout`. Terminal block 48 contains 386 transactions. Following the professor's latest clarification, genesis/config blocks are excluded and terminal ordinary partial blocks are retained, so the accepted population contains all 32 ordinary blocks:

```text
block_bytes_mean = 1952347.156250
block_utilisation = 1952347.156250 / 2097152 = 0.930951670
```

The 31 full blocks alone have mean 1,966,319.451613 bytes and rho 0.937614179. The difference is `0.006662509` in rho, or about **0.666251 percentage points**. That value is retained only as a diagnostic comparison and does not replace the accepted boundary-inclusive result. MaxMessageCount-driven closure and inclusion of the terminal ordinary block are no longer professor-decision blockers.

## Public-key identity evidence

`identity_bytes` is the byte length of the public-key representation used by Fabric. It excludes the MSP signcert PEM and every signature. The measurement tool extracts all four peers:

```bash
cd env/fabric/e1
./measure_identity_bytes.sh ecdsa public-key-v1
```

For ECDSA, the measured representation is the DER X.509 SubjectPublicKeyInfo returned by Fabric's `ecdsaPublicKey.Bytes()` (`x509.MarshalPKIXPublicKey`), which is 91 bytes for each retained P-256 peer key. For PQ configurations, it is the raw liboqs public key returned by patched `pqPublicKey.Bytes()` and carried in experimental extension `1.3.6.1.3.9999.1`. The tool records each peer and key hash, then reports mean/min/max and flags dispersion above 3%; the flag never discards data.

Supported public-key sizes are:

| Configuration | Representation | Per-peer bytes | Mean | Min–max |
|---|---|---:|---:|---:|
| ECDSA | DER SubjectPublicKeyInfo | 91, 91, 91, 91 | 91 | 91–91 |
| ML-DSA-44 | raw liboqs public key | 1312, 1312, 1312, 1312 | 1312 | 1312–1312 |
| ML-DSA-65 | raw liboqs public key | 1952, 1952, 1952, 1952 | 1952 | 1952–1952 |
| SLH-DSA (`SPHINCS+-SHA2-128s-simple`) | raw liboqs public key | 32, 32, 32, 32 | 32 | 32–32 |

The earlier files named `*_identity_bytes.csv` contain MSP signcert PEM sizes (ECDSA 806–810, ML-DSA-44 2638–2642, ML-DSA-65 3508, SPHINCS+ 916). They remain scientifically useful supporting evidence and must not be deleted, but they are explicitly ineligible for the final `identity_bytes` field. The PQ generation logs contain four actual `public_key_bytes` observations per algorithm. Regenerate the validated public-key summary with `./src/e1/analyze_timings.py --identity-public-keys`.

## Existing E1 data and analysis

`src/e1/analyze_timings.py` validates the three zero-failure candidate configurations, requires at least 1,000 samples per timing file, checks the Caliper result tables, calculates median/p95/p99 using the same `p*(n-1)` interpolation as E0, and records source hashes:

```bash
./src/e1/analyze_timings.py
```

The retained generated result is `data/e1_timing_candidates.csv`. Its rows are explicitly `candidate_historical_preflight_not_captured`, not final E1 claims. SPHINCS+ is intentionally rejected from this normal-latency dataset.

The observed SPHINCS+ fixed-profile run recorded 700 successes/301 failures in warm-up, 891/5,110 at 50 TPS, and 46/23,955 at 200 TPS. At the common 50 TPS point, `tx_success_rate=0.148475254`, `tx_error_rate=0.851524746`, and status is `saturation_observed_gateway_concurrency_limit`. The log contains `exceeding concurrency limit (500)` errors, followed by retained Gossip/membership symptoms and endorsement-set errors. Caliper's displayed throughput is not successful committed throughput when failures dominate. A plausible interpretation is that long operations accumulated outstanding requests until the limit was reached and peer responsiveness degraded; this causal chain is an inference, not a proven mechanism. Do not raise the Gateway limit, change the common fixed profile, delete this run, or fabricate a latency row.

Regenerate common-profile success/error rates and saturation labels with:

```bash
./src/e1/analyze_timings.py --fixed-outcomes
```

## SPHINCS+ low-rate sweep and sustainability rule

`benchmark_sphincs_sweep.yaml` is a supplemental profile created because SPHINCS+ saturated at the unchanged common 50 TPS point. It has a discarded 20-second 1 TPS warm-up followed by 60 seconds each at 1, 2, 5, 10, and 20 TPS. It does not replace or modify `benchmark.yaml`.

The later professor instruction fixes these rounds at 60 seconds. Consequently the lowest rates intentionally contain fewer than 1,000 requests; this topic-specific clarification supersedes the earlier general sample-count rule for this sweep and must be disclosed with its results.

The human-controlled initial sweep command is:

```bash
cd env/caliper/e1
E1_BENCHCONFIG=benchmark_sphincs_sweep.yaml \
E1_RUN_LABEL=sphincs-lowrate-v1 \
E1_RUN_TYPE=sweep \
./run_e1.sh sphincs
```

A tested rate is sustainable only if all three deterministic checks pass:

- `tx_success_rate = success / (success + fail) >= 0.99`;
- successful throughput, defined as `success / 60 seconds`, is at least `0.95 * offered_rate`;
- successful end-to-end latency is stable: p95 for requests starting in `[48000,60000)` ms is no more than 2.0 times p95 for requests starting in `[0,12000)` ms.

These are the first and last 20% of the configured 60-second round, selected by monotonic request-start offset. Each window must contain at least five successful samples. Percentiles use linear interpolation at rank `p*(n-1)`. The final SPHINCS+ `tps_sustained` is the highest tested low-rate point passing all checks; zero failures are not required. The evaluator validates the run log, benchmark hash, CPU/Fabric/image provenance, height-marker sequence, all endorsement/commit/e2e file schemas and counts, and every source hash before reporting results:

```bash
./src/e1/analyze_timings.py --sustainability sphincs-lowrate-v1_sphincs
```

The retained `sphincs-lowrate-v1_sphincs` sweep completed with zero failures at every tested rate. All 1/2/5/10/20 TPS points pass the preregistered success-rate, successful-throughput, and latency-stability gates, so that initial sweep established 20 TPS as sustainable. Exact statistics and hashes are retained in `data/e1_sphincs_sustainability.csv`. The earlier 3/4-TPS refinement profile is unnecessary because both rates lie below an already-passing 20 TPS point. Subsequent boundary probes are documented below.

Same-ledger evidence for the 20 TPS round contains 1,201 serialized endorser-transaction envelopes in 30 ordinary blocks. The blocks are BatchTimeout-driven: 30 blocks over about 60 seconds matches `BatchTimeout=2s`; the 29 nonterminal blocks contain 35–46 transactions (mean 41.206897), close to the 40 expected from 20 TPS for two seconds; no block approaches `MaxMessageCount=500` or `PreferredMaxBytes`. Consequently its `block_bytes_mean=790308.966667` and rho `0.376848682` are retained as diagnostic evidence and are ineligible for final SPHINCS+ block utilisation.

The same evidence independently supports `tx_bytes_mean=19711.570358`, calculated from each exact serialized `common.Envelope`, and empirical `endorsements_per_tx=2.000000` with min=max=2 across all 1,201 transactions. Validation codes are unavailable because `peer channel fetch` obtained the orderer's block copy without peer-added `TRANSACTIONS_FILTER` metadata. The original raw summary grouped these unavailable codes with invalid transactions; it remains unchanged, while `data/e1_sphincs_transaction_evidence.csv` records the corrected interpretation as valid=0, invalid=0, unavailable=1201. This limitation does not affect envelope sizes or embedded endorsement counts.

The 35 TPS midpoint probe was not sustainable: 1,294/2,101 succeeded, `tx_success_rate=0.615897192`, successful throughput was 21.566667 TPS (`0.616190` of offered), and successful e2e median/p95/p99 were 17072.196082/32763.050961/36082.802465 ms. The success-rate and throughput gates failed. The end/beginning p95 ratio was 1.286502, so the latency-stability gate alone passed. Exact source hashes are retained in `data/e1_sphincs_boundary_35.csv`.

The 28 TPS probe also was not sustainable: 1,433/1,681 succeeded, `tx_success_rate=0.852468769`, successful throughput was 23.883333 TPS (`0.852976` of offered), and successful e2e median/p95/p99 were 12452.633946/32294.910555/34388.419210 ms. Its success-rate and throughput gates failed, and its end/beginning p95 ratio of 2.660599 also failed the latency-stability gate. Exact source hashes are retained in `data/e1_sphincs_boundary_28.csv`.

The 24 TPS probe is not sustainable despite completing all 1,441 requests successfully. Its successful throughput was 24.016667 TPS (`1.000694` of offered), so the success-rate and throughput gates passed. However, successful e2e median/p95/p99 were 7191.702027/13479.916965/15162.890049 ms, and the ending/beginning p95 ratio was 2.861839, which fails the preregistered latency-stability gate. Exact source hashes are retained in `data/e1_sphincs_boundary_24.csv`.

The 22 TPS probe is sustainable: all 1,321 requests succeeded, successful throughput was 22.016667 TPS (`1.000758` of offered), and successful e2e median/p95/p99 were 3444.793506/5941.405725/6501.009460 ms. Its success-rate and throughput gates passed, and its ending/beginning p95 ratio of 1.876994 passed the latency-stability gate. Exact source hashes are retained in `data/e1_sphincs_boundary_22.csv`.

The 23 TPS probe is not sustainable despite completing all 1,381 requests successfully. Its successful throughput was 23.016667 TPS (`1.000725` of offered), so the success-rate and throughput gates passed. Successful e2e median/p95/p99 were 5252.631986/9124.870637/10627.962666 ms, but its ending/beginning p95 ratio of 2.368567 failed the latency-stability gate. Exact source hashes are retained in `data/e1_sphincs_boundary_23.csv`.

The integer boundary search is complete: 22 TPS is the highest tested sustainable integer rate and 23 TPS is the first tested nonsustainable integer rate. Therefore the working final SPHINCS+ `tps_sustained` is 22 TPS; no additional integer boundary probe is required.

## Remaining E1 rate profiles

The immutable `benchmark_ecdsa_sustained_222.yaml` profile has now been run as `sustained-222-v1_ecdsa`. All 13,321 requests succeeded; successful throughput was 222.016667 TPS, the beginning/end p95 values were 2137.139872/2119.418924 ms, and every preregistered gate passed. The byte-for-byte reproducible result is retained in `data/e1_ecdsa_sustainability_222.csv` (SHA-256 `153f64978b858010835e8e55efeff2ed9cbf5a61386df19d5482ed35de24855d`). This establishes 222 TPS as the highest tested sustainable ECDSA rate, not yet the final boundary.

The subsequent `sustained-223-v1_ecdsa` probe also passed every gate: 13,379/13,381 requests succeeded, successful throughput was 222.983333 TPS, and the beginning/end p95 values were 2147.656240/2119.759641 ms. Its reproducible result is retained in `data/e1_ecdsa_sustainability_223.csv` (SHA-256 `d9fa90ed05baf9d311b6fe0c97fbcc39d03bdcbe3da69068faa311d3c4d0939c`).

The `sustained-224-v1_ecdsa` probe passed as well: 13,422/13,441 requests succeeded, successful throughput was 223.700000 TPS, and the beginning/end p95 values were 2148.214579/2120.795105 ms. Its reproducible result is retained in `data/e1_ecdsa_sustainability_224.csv` (SHA-256 `ab70188ddfd7657807ec180fed059f2e14feedc4dea6be699627a442067d9176`). Thus 224 TPS remains the highest tested sustainable ECDSA rate.

The adaptive-bracketing `sustained-250-v1_ecdsa` probe failed: 14,063/15,001 requests succeeded, giving `tx_success_rate=0.937470835` and successful throughput 234.383333 TPS (`0.937533` of offered). Its success-rate and throughput gates failed while its latency-stability gate passed. The reproducible result is retained in `data/e1_ecdsa_sustainability_250.csv` (SHA-256 `7c4cb94ff8744188dba3dd2b1da96c76985f41ba51cb7c1c1958eb2f1d0490e3`).

The midpoint `sustained-237-v1_ecdsa` probe also failed: 13,750/14,221 requests succeeded, so `tx_success_rate=0.966879966` failed its gate even though successful throughput was 229.166667 TPS (`0.966948` of offered) and latency remained stable. Its reproducible result is retained in `data/e1_ecdsa_sustainability_237.csv` (SHA-256 `918c6c3ec5062e457b239768068a54ccc6145f92ec0d91cfdb031e607e61382e`).

The next midpoint `sustained-230-v1_ecdsa` probe failed as well: 13,621/13,801 requests succeeded, so `tx_success_rate=0.986957467` failed its gate even though successful throughput was 227.016667 TPS (`0.987029` of offered) and latency remained stable. Its reproducible result is retained in `data/e1_ecdsa_sustainability_230.csv` (SHA-256 `05050cb10b3dd83774bbdc67de5ab12089e810c0c9913b28c9aaf368676267f5`).

The `sustained-227-v1_ecdsa` midpoint passed: 13,509/13,621 requests succeeded, giving `tx_success_rate=0.991777403` and successful throughput 225.150000 TPS (`0.991850` of offered), while latency remained stable. Its reproducible result is retained in `data/e1_ecdsa_sustainability_227.csv` (SHA-256 `19fdc0ef20a2d6e8bad308755c4a3abac8b22ebe399798c3995c6927dcd3ee2a`).

The `sustained-228-v1_ecdsa` probe also passed: 13,564/13,681 requests succeeded, giving `tx_success_rate=0.991447994` and successful throughput 226.066667 TPS (`0.991520` of offered), while latency remained stable. Its reproducible result is retained in `data/e1_ecdsa_sustainability_228.csv` (SHA-256 `29edc6c3e47c7f00043f06fd21d734e43e84ce0d45919a3e49ba419e1b5864c6`).

The final integer probe `sustained-229-v1_ecdsa` failed: 13,561/13,741 requests succeeded, so `tx_success_rate=0.986900517` failed its gate even though successful throughput was 226.016667 TPS (`0.986972` of offered) and latency remained stable. Its reproducible result is retained in `data/e1_ecdsa_sustainability_229.csv` (SHA-256 `970c0b9d7d6b8d2ea1e1169ca6e8bbbdd30444690d0fd4bb1b53e3e5ceca9631`). The ECDSA integer boundary is complete: 228 TPS is the highest sustainable integer rate, 229 TPS is the first failing integer rate, and final ECDSA `tps_sustained=228`.

`benchmark_ml_dsa_sustained_200.yaml` is shared, without parameter substitution, by separate ML-DSA-44 and ML-DSA-65 networks; it contains the same warm-up and one 60-second round at 200 TPS. Those measured rounds use exactly the sustainability gates and request-start windows defined above. The shared ML-DSA profile does not merge configurations: `run_e1.sh` provisions the selected matching network, and its profile policy accepts only ML-DSA-44 or ML-DSA-65 with `E1_RUN_TYPE=sustainability` and a collision-safe label.

The `sustained-200-v1_ml-dsa-44` run passed every sustainability gate with 12,001/12,001 successes, successful throughput 200.016667 TPS, and beginning/end p95 values 927.987266/918.311832 ms. Its reproducible result is retained in `data/e1_ml_dsa_44_sustainability_200.csv` (SHA-256 `b2460d661ddf1d9905735c046bd7fe76b1722e6f7c3042c3d45b94e12e74bbcc`). This establishes 200 TPS as sustainable but does not close the ML-DSA-44 upper boundary.

Same-ledger evidence covers all 12,001 transactions in ordinary blocks 17–106, but the 90-block population is mixed. Sixty blocks contain 170 transactions and occupy 2,090,411–2,092,264 bytes; they were cut by `PreferredMaxBytes` because their remaining 4,888–6,741 bytes cannot fit another observed 12,281–12,299-byte envelope. The other 30 blocks are periodic residuals: 29 nonterminal blocks contain 60–64 transactions and the terminal block contains 33. The exact repeating pattern is two 170-transaction blocks followed by one residual block, yielding 90 blocks over about 60 seconds and identifying the residuals as `BatchTimeout=2s` cuts. `MaxMessageCount=500` is never approached. Therefore the boundary-inclusive `block_bytes_mean=1640832.944444` and rho `0.782410118` are diagnostic-only. The 60 size-driven blocks alone have mean 2,091,602.116667 bytes and rho 0.997353609, retained only as a diagnostic comparison because no approved methodology permits dropping periodic ordinary timeout blocks.

`benchmark_sphincs_blockutil_54.yaml` is a separate block-filling diagnostic with a discarded 20-second 1-TPS warm-up and one 60-second 54-TPS round. It is not a latency or sustained-TPS profile and cannot change the accepted SPHINCS+ `tps_sustained=22`. Its only purpose is to determine empirically whether the unchanged Fabric parameters can produce a clean traffic-volume-driven block population. If it produces timeout-driven or degraded blocks, no higher rate or parameter change is automatic.

For allowlisted sustainability runs, `analyze_timings.py --sustainability <run_namespace>` applies the same mathematics used for SPHINCS+. Its output retains total/success/failure counts, success/error rates, successful throughput and offered-rate ratio, successful E2E median/p95/p99, beginning/ending window counts and p95 values, the stability ratio, each gate result, overall sustainability, and paths plus SHA-256 hashes for every validated source. Caliper displayed throughput remains provenance only and is never used for the throughput gate.

The separate common-profile latency populations requested for professor review are retained in `data/e1_latency_professor_review.csv`. Regenerate it with `./src/e1/analyze_timings.py --latency-professor-review --output data/e1_latency_professor_review.csv --replace`. The table keeps 50-TPS and 200-TPS samples separate. Its ECDSA, ML-DSA-44, and ML-DSA-65 rows are validated zero-failure latency candidates, with the caveat that their historical logs predate full standardized CPU-preflight capture in-log. Its SPHINCS+-SHA2-128s-simple rows retain common-profile success/error and saturation evidence, but all latency fields are intentionally empty. The professor has not yet selected which rate supplies the single final latency fields.

The final E1 schema, once all decisions and measurements are valid, is:

```text
config,identity_bytes,endorse_median_ms,endorse_p95_ms,commit_median_ms,tps_sustained,tx_success_rate,tx_error_rate,tx_bytes_mean,endorsements_per_tx,block_bytes_mean,block_utilisation
```

P99 endorsement and commit statistics remain required in traceable supporting data even though they are not fields in this main CSV.

An explicitly incomplete working-schema view can be regenerated with:

```bash
./src/e1/analyze_timings.py --working-e1
```

It validates the historical signcert evidence without treating it as `identity_bytes`, validates the public-key-only sizes, verifies common-profile success/error outcomes and the configured endorsement policy, and verifies the ECDSA block and all four exact-envelope summaries against retained raw sources and `meta.json`. It emits the exact four-row schema to stdout. Supported public-key means, common-50-TPS success/error rates, the completed ECDSA and SPHINCS+ sustained rates, all four empirical transaction-size and endorsement results, and the accepted boundary-inclusive ECDSA block fields are populated. Unmeasured ML-DSA sustained/block fields and unresolved latency fields remain empty. It intentionally refuses `--output`, preventing the working view from being mistaken for the final deliverable.

## Remaining E1 final-output decision

The only intentionally unresolved E1 final-output methodology item is which fixed-profile round or combined 50-TPS/200-TPS sample population supplies the single endorsement/commit fields in `e1_fabric.csv`. Both round-specific results remain separate for professor review. The added `tx_success_rate`, `tx_error_rate`, `tx_bytes_mean`, and `endorsements_per_tx` columns are part of the final E1 schema.

Separately, the plausible bands/source for the `make_figures.py` E0 sanity checker referenced by the student specification are absent from the supplied repository; this is a later repository-level implementation gap, not an E1 output-methodology decision.

The older pre-standardized ECDSA 222/224 TPS probe remains diagnostic evidence only. It is distinct from the controlled `sustained-222-v1_ecdsa` measurement above; the latter passed at 222 TPS and is the starting point for the preregistered integer boundary search.

## Later experiments

The student specification defines the dependency order after E1:

- E2: eight channel-state transitions for classical, uniform ML-DSA, and layer-aware configurations; penalty bytes must come from a real serialized force-close-and-punish execution.
- E5: watchtower storage and scan CPU over at least four decades, measured rather than extrapolated.
- E9: communication-network sensitivity in Mininet at RTT 5/20/50/100 ms and 1–5 hops, with ping verification before each run; E0 crypto and E9 network measurements remain separate.
- E7: at least 1,000 raw end-to-end samples per configuration using a documented/licensed 15-minute PV/demand trace. `sets_reimpl` must always be identified as a reimplementation.
- E8: constrained-hardware energy with separately measured idle/load power and a documented instrument.
- E6: optional key-aggregation ablation.

No proposal-era energy-market or power-flow logic should be introduced unless the student specification requires it. Mininet represents the communication network only.
