# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical measurement package for a research testbed. Its governing principle is to measure what the implemented system actually does, retain the raw evidence, and report surprising results rather than tune them toward an analytical model.

`docs/student_spec(1).pdf` is the primary implementation specification. A later direct professor clarification overrides it only for the clarified point; the proposal is background and lower authority.

## Current status

- E0 server primitive measurements are complete: 21 scheme/operation rows, 10,000 retained samples per row, and median/p95/p99 summaries. Meter/SBC measurements and the supplied sanity-gate implementation are still missing.
- E1 is functionally implemented for ECDSA, ML-DSA-44, ML-DSA-65, and SPHINCS+-SHA2-128s-simple using one Fabric topology and one fixed Caliper profile.
- Candidate fixed-profile data exist for ECDSA, ML-DSA-44, and ML-DSA-65. They pass sample-count and zero-failure checks, but their retained logs predate full preflight capture, so they are not silently promoted to final data.
- The identical-profile SPHINCS+ run saturated and failed heavily. Its raw samples and log are the current reportable result for that configuration, but they are excluded from the normal latency summary. No lower-rate replacement run is currently planned.
- Raw per-peer identity sizes are retained for all four E1 configurations without aggregation. The ECDSA block-filling probe provides a boundary-inclusive working block-utilisation value of `0.930951670`; professor review of MaxMessageCount closure and the terminal partial block remains pending. Identity aggregation and sustained TPS remain unresolved, so `data/e1_fabric.csv` has not been created.
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

The connector records endorsement around `proposal.endorse()` and commit from submission through transaction status/commit completion. `process.hrtime.bigint()` is used.

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

With no label, the historical namespace and default profile are unchanged (`ecdsa_*`, `ml-dsa-44_*`, etc.). The runner refuses to overwrite any artifact in that namespace. It now retains configuration, run type/label, benchmark path/hash, time, project commit/pre-log worktree state, host/kernel state, per-CPU state, Fabric source state, image IDs, relevant Fabric/Caliper source hashes, setup output, and decoded effective block parameters in the run log.

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

The tool derives the range from that run's before/after heights, rejects block 0, decodes each block, records every block's byte size/header types/classification, explicitly excludes config/other blocks, and writes both raw and summary CSVs. It decodes effective `PreferredMaxBytes` from the current Fabric config block; it does not hard-code the denominator in the script.

Professor-defined calculation:

```text
block_utilisation = mean(accepted ordinary-transaction block bytes)
                    / effective PreferredMaxBytes
```

The generated config decodes `2 MB` to 2,097,152 bytes. `AbsoluteMaxBytes` (10,485,760 bytes) is not the denominator. Genesis and config blocks are excluded.

For the retained ECDSA probe, blocks 17–47 each contain exactly `MaxMessageCount=500` ordinary transactions. The steady-state active block-cutting constraint was therefore MaxMessageCount, not `BatchTimeout`. Terminal block 48 contains 386 transactions. Because the professor explicitly excluded genesis/config blocks but did not explicitly exclude terminal partial ordinary blocks, the current working calculation includes all 32 ordinary blocks:

```text
block_bytes_mean = 1952347.156250
block_utilisation = 1952347.156250 / 2097152 = 0.930951670
```

The 31 full blocks alone have mean 1,966,319.451613 bytes and rho 0.937614179. That value is retained only as a diagnostic comparison and does not replace the boundary-inclusive working result. Professor review is still required on whether MaxMessageCount closure satisfies the intended volume-filled condition and whether terminal residual ordinary blocks should be excluded.

## Identity evidence

Measure all four peer signcert files for the currently generated configuration:

```bash
cd env/fabric/e1
./measure_identity_bytes.sh ecdsa
```

For a labelled run, add the label as the second argument. The output retains config, peer, identity type/path, bytes, and SHA-256 per peer. It intentionally does not compute one aggregate: the method for mapping four peer values into the single `identity_bytes` E1 field still requires professor clarification.

Per-peer raw evidence now exists for all configurations:

| Configuration | peer0.org1 | peer1.org1 | peer0.org2 | peer1.org2 | Raw source |
|---|---:|---:|---:|---:|---|
| ECDSA | 810 | 806 | 810 | 806 | `raw/e1/blockutil-300_ecdsa_identity_bytes.csv` |
| ML-DSA-44 | 2638 | 2642 | 2642 | 2642 | `raw/e1/identity-only-v1_ml-dsa-44_identity_bytes.csv` |
| ML-DSA-65 | 3508 | 3508 | 3508 | 3508 | `raw/e1/identity-only-v1_ml-dsa-65_identity_bytes.csv` |
| SLH-DSA (`SPHINCS+-SHA2-128s-simple`) | 916 | 916 | 916 | 916 | `raw/e1/identity-only-v1_sphincs_identity_bytes.csv` |

The three PQ sets were generated using the same `cryptogen` and PQ-identity generator path in isolated temporary crypto trees; no Fabric network or workload was started. Their generation logs retain the project commit, tool/source hashes, exact algorithm, public-key length, and measured certificate lengths. These per-peer values are scientifically supported but are not collapsed into the final single field.

## Existing E1 data and analysis

`src/e1/analyze_timings.py` validates the three zero-failure candidate configurations, requires at least 1,000 samples per timing file, checks the Caliper result tables, calculates median/p95/p99 using the same `p*(n-1)` interpolation as E0, and records source hashes:

```bash
./src/e1/analyze_timings.py
```

The retained generated result is `data/e1_timing_candidates.csv`. Its rows are explicitly `candidate_historical_preflight_not_captured`, not final E1 claims. SPHINCS+ is intentionally rejected from this normal-latency dataset.

The observed SPHINCS+ fixed-profile run recorded 700 successes/301 failures in warm-up, 891/5,110 at 50 TPS, and 46/23,955 at 200 TPS. The log contains `exceeding concurrency limit (500)` Gateway errors, followed by retained Gossip/membership symptoms and endorsement-set errors. Caliper's displayed throughput is not successful committed throughput when failures dominate. A plausible interpretation is that long operations accumulated outstanding requests until the limit was reached and peer responsiveness degraded; this causal chain is an inference, not a proven mechanism. The current decision is to report this saturation result without a lower-rate rerun. Do not raise the Gateway limit, change the common fixed profile, delete this run, or fabricate a latency row.

The final E1 schema, once all decisions and measurements are valid, is:

```text
config,identity_bytes,endorse_median_ms,endorse_p95_ms,commit_median_ms,tps_sustained,block_bytes_mean,block_utilisation
```

P99 endorsement and commit statistics remain required in traceable supporting data even though they are not fields in this main CSV.

An explicitly incomplete working-schema view can be regenerated with:

```bash
./src/e1/analyze_timings.py --working-e1
```

It verifies all four per-peer identity evidence files and their provenance hashes, then verifies the ECDSA block summary against the retained per-block source and `meta.json`. It emits the exact four-row schema to stdout, populates only the supported boundary-inclusive ECDSA block fields, and leaves unresolved cells empty. It intentionally refuses `--output`, preventing the working view from being mistaken for the final deliverable.

## Pending scientific decisions

The following must not be guessed and should be surfaced at the professor meeting:

1. whether the currently reported SPHINCS+ fixed-profile saturation result should later be supplemented by a lower-rate latency run;
2. whether MaxMessageCount-driven blocks at about 93.76% of PreferredMaxBytes satisfy the intended filled-by-volume condition;
3. whether terminal partial ordinary blocks such as block 48 should be excluded, despite the explicit instruction naming only genesis/config blocks;
4. how four per-peer identity sizes map to one `identity_bytes` value;
5. the operational threshold for “highest rate at which throughput remains linear/sustainable”;
6. which fixed-profile round or combined sample population supplies the single endorsement/commit fields in the E1 CSV;
7. the plausible bands/source for the `make_figures.py` E0 sanity checker referenced by the student specification but absent from the supplied repository.

The earlier ECDSA 222/224 TPS probe remains diagnostic evidence only and is not a final `tps_sustained` value.

## Later experiments

The student specification defines the dependency order after E1:

- E2: eight channel-state transitions for classical, uniform ML-DSA, and layer-aware configurations; penalty bytes must come from a real serialized force-close-and-punish execution.
- E5: watchtower storage and scan CPU over at least four decades, measured rather than extrapolated.
- E9: communication-network sensitivity in Mininet at RTT 5/20/50/100 ms and 1–5 hops, with ping verification before each run; E0 crypto and E9 network measurements remain separate.
- E7: at least 1,000 raw end-to-end samples per configuration using a documented/licensed 15-minute PV/demand trace. `sets_reimpl` must always be identified as a reimplementation.
- E8: constrained-hardware energy with separately measured idle/load power and a documented instrument.
- E6: optional key-aggregation ablation.

No proposal-era energy-market or power-flow logic should be introduced unless the student specification requires it. Mininet represents the communication network only.
