# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical testbed for measuring cryptographic, blockchain, and communication-network costs. Reported values are derived from retained measurements rather than tuned to expected trends.

## Status and outputs

- E0 primitives, server: complete.
- E0 primitives, meter/SBC: pending hardware measurement.
- E1 Fabric: complete.
- E2 channel state machine: complete.
- E5 watchtower scalability: complete.
- E9 network sensitivity: complete.
- E7 and E8: not yet completed.

Final completed-experiment CSVs:

- `data/e0_primitives.csv`
- `data/e1_fabric.csv`
- `data/e2_statemachine.csv`
- `data/e5_watchtower.csv`
- `data/e9_network.csv`

The specification references a supplied `make_figures.py` sanity checker, but that source and its plausible bands are not present in the repository; no replacement is fabricated.

## Repository structure

```text
README.md
meta.json
env/       environment, Fabric, Caliper, and network configuration
src/       benchmark, chaincode, analysis, identity, and evidence code
data/      final experiment CSV deliverables
raw/       retained raw measurement evidence
figures/   generated final figures
```

## Environment and algorithm pins

Key software pins:

- Hyperledger Fabric `v2.5.16`, commit `f871cf92a026aba7b12e6f06d71ded3e6e659d71`
- Fabric CA `1.5.17`
- Hyperledger Caliper and Fabric connector `0.7.1`
- liboqs `0.15.0`, commit `97f6b86b1b6d109cfd43cf276ae39c2e776aed80`
- liboqs-go `v0.15.0`, commit `75451133b94a6c4be5f528eef94916ce08475f24`
- Mininet `2.3.0`

Measured/configured schemes include ML-KEM-768, ML-DSA-44/65/87, Falcon-512, `SPHINCS+-SHA2-128s-simple`, and ECDSA P-256.

`SLH-DSA` in the final E1 schema is implemented as liboqs `SPHINCS+-SHA2-128s-simple`.

Falcon-512 and Falcon-padded-512 in liboqs 0.15.0 are round-3 implementations and must not be described as final FIPS 206.

## E0 primitive benchmark

E0 uses direct liboqs APIs and OpenSSL/libcrypto for ECDSA P-256; it does not use oqs-provider. The deterministic signature message is `SHA-256("pqc-energy-trading-e0-message-v1")`. ECDSA SHA-256 hashing is outside the timed sign/verify region.

The canonical server run used:

- 100 discarded warm-up iterations
- 10,000 measured iterations
- `GOMAXPROCS=1`
- logical CPUs 2 and 3
- amd-pstate passive mode
- performance governor
- boost disabled
- approximately 3.2 GHz fixed frequency
- AC power

Results report median, p95, and p99. The canonical server output is `data/e0_primitives.csv`; retained raw samples and the run log are under `raw/e0/`.

The controlled server measurement entry point is `src/e0/run_e0_server.sh`. The meter/SBC measurement remains pending and must not be extrapolated from the server result.

## E1 Fabric benchmark

### Testbed and cryptographic scope

E1 uses Hyperledger Fabric 2.5 with two organizations, two peers per organization, one etcdraft/Raft orderer, channel `energychannel`, and a simple key/value chaincode. The benchmarked operation is `Set(key,value)`.

Common Fabric parameters:

- `BatchTimeout = 2s`
- `MaxMessageCount = 500`
- effective `PreferredMaxBytes = 2,097,152` bytes
- effective `AbsoluteMaxBytes = 10,485,760` bytes
- Application endorsement policy: `ImplicitMeta MAJORITY`
- empirical endorsements per retained transaction: 2

The PQ implementation is experimental and hybrid: standard ECDSA X.509 membership and CA signatures remain, while patched peer transaction/endorsement signing and verification uses liboqs-backed PQ keys for the tested PQ configurations. It is not standardized post-quantum X.509, a full post-quantum PKI, or native Fabric PQ identity support. Caliper client, TLS, and orderer identities remain ECDSA.

Retained runtime verification evidence confirms PQ verification dispatch for ML-DSA-44, ML-DSA-65, and `SPHINCS+-SHA2-128s-simple`, with no silent ECDSA fallback. Trace logging is disabled during performance measurements.

### Metric definitions and methodology

The common latency profile is `env/caliper/e1/benchmark.yaml`: 20 seconds at 50 TPS as discarded warm-up, followed by 120 seconds at 50 TPS and 120 seconds at 200 TPS. The professor-selected final latency population is the 200-TPS round; the 50-TPS round is supporting evidence.

`commit_*_ms` represents post-endorsement submit-to-commit-status latency: timing starts immediately before `transaction.submit()` and ends after `subtx.getStatus()`. It includes client signing/RPC waiting, orderer submission and acknowledgement, block-cut or timeout waiting, validation, ledger commit, and status delivery. It excludes proposal construction, endorsement, and Caliper scheduling before connector invocation.

`identity_bytes` is the public-key representation used by Fabric, excluding certificate and signature bytes.

`tx_bytes_mean` is the arithmetic mean of exact serialized `common.Envelope` lengths extracted from fetched blocks. It is not estimated from block size divided by transaction count.

A 60-second offered rate is classified as sustainable only if all of the following hold:

- transaction success rate is at least 0.99
- successful throughput is at least 95% of configured offered TPS
- at least five successful samples occur in both request-start windows `[0,12000)` ms and `[48000,60000)` ms
- ending-window p95 is at most twice beginning-window p95
- percentiles use linear interpolation at rank `p*(n-1)`

Final adjacent PASS/FAIL boundaries are retained in raw evidence for each configuration.

Block utilisation is:

```text
block_utilisation =
    mean(actual fetched qualifying ordinary-transaction block bytes)
    / 2,097,152
```

Genesis, configuration, and other nonordinary blocks are excluded. Ordinary blocks established as `BatchTimeout` cuts are excluded; volume-filled ordinary blocks are retained.

For evidence collected with the final block inspector, exact block-cutter message size is `len(common.Envelope.Payload) + len(common.Envelope.Signature)`. The retained ML-DSA block datasets predate that field and use the previously validated dataset-specific five-byte framing reconstruction.

### Required caveats

`SPHINCS+-SHA2-128s-simple` saturated under the unchanged common latency profile, so the final `SLH-DSA` latency fields at 200 TPS are intentionally blank rather than reported as normal-latency measurements. Its success and error rates remain reported.

The final SLH-DSA block-utilisation estimate pools two qualifying size-filled blocks from two independent, otherwise identical 54-TPS runs. Therefore its retained population is `n=2` and must be interpreted as a small-population estimate. A supporting 60-TPS diagnostic produced no qualifying retained block and does not contribute to the final pooled statistic.

## E2 channel state machine

E2 uses one deterministic binary envelope for all configurations, in network byte order, with no varints. Logical channel ID `c1` is serialized as `SHA-256("c1")` (identifier canonicalization only). Unused 32-byte identifiers are zero; unused state references are `uint64` maximum. Raw signatures and public keys use unsigned 16-bit big-endian length prefixes.

| Field | Encoding | Bytes |
|---|---|---:|
| format version | `uint8` | 1 |
| transition type | `uint8` enum | 1 |
| channel ID | SHA-256 logical ID | 32 |
| state number | `uint64` big-endian | 8 |
| balance A | `uint64` big-endian | 8 |
| balance B | `uint64` big-endian | 8 |
| HTLC ID | SHA-256 logical ID or zero sentinel | 32 |
| referenced state | `uint64` big-endian | 8 |
| revoked state | `uint64` big-endian | 8 |
| superseded-by state | `uint64` big-endian | 8 |
| signature count | `uint16` big-endian | 2 |
| signature slot 1 | `uint16` length + raw bytes | 2 + signature bytes |
| signature slot 2 | `uint16` length + raw bytes | 2 + signature bytes, or 2 when unused |
| public-key count | `uint16` big-endian | 2 |
| public-key slot 1 | `uint16` length + raw bytes | 2 + public-key bytes |
| public-key slot 2 | `uint16` length + raw bytes | 2 + public-key bytes, or 2 when unused |

`classical` uses one 64-byte Ed25519 signature and one 32-byte public key as the compact representation of the MuSig2-aggregated size case; this does not assert that Ed25519 itself is MuSig2. `uniform_mldsa` uses ML-DSA-65 (3,309-byte signatures, 1,952-byte public keys) without aggregation. For `layer_aware`, channel setup, commitment updates, and HTLC add/settle use enabled liboqs `Falcon-padded-512` (fixed 666-byte signatures, 897-byte public keys); funding, cooperative close, force close, and penalty use ML-DSA-65.

The crypto section always carries framing for exactly two signature slots and two public-key slots. An unused second slot has a zero length and no payload; it is canonical framing, not a dummy cryptographic object. Actual counts remain one or two and must match contiguous occupied slots.

Scientific E2 authorization uses real Ed25519 and pinned liboqs `0.15.0` (`97f6b86b1b6d109cfd43cf276ae39c2e776aed80`) ML-DSA-65/Falcon-padded-512 keys. Participant keys are generated once per run context and reused; private keys are never serialized. The signing preimage is the canonical 114-byte semantic body; verified signatures and corresponding public keys are then inserted into the canonical transaction envelope. Every bundle is verified before serialization.

For a serialized transaction, `T0 = total bytes - signature payload bytes - public-key payload bytes`; both count fields and all four fixed-width element-length prefixes remain in `T0`. Derived from the emitted field table, `T0` is globally constant at 126 bytes. Final `message_bytes` is obtained only as the length of the actual serialized transaction.

E2 RTT uses two Mininet hosts joined by one direct 20 ms RTT `TCLink`. A prepares and signs the canonical transaction before timing, starts a monotonic timer immediately before sending the serialized bytes over an already-established `TCP_NODELAY` connection, and stops only after receiving B's success ACK. B sends that ACK only after verifying every included signature over the canonical semantic preimage. Thus RTT includes transmission, receive/framing, real verification at B, and ACK return, while excluding key generation, signing, transaction construction, serialization, and TCP setup. Transport framing and ACK bytes do not contribute to `message_bytes`. The classical verification remains one Ed25519 verification for the representative aggregated-size case; it is not a MuSig2 execution. Each scientific condition used 100 discarded warm-up iterations and 1,000 measured iterations after a retained 20-sample ping passed the strict deviation `<10%` gate. The penalty transaction is produced by the real state-machine path that revokes an old commitment, force-closes that stale state, and authorizes the counterparty punishment. Final raw evidence is under `raw/e2/`; `data/e2_statemachine.csv` is complete.

## E5 watchtower scalability

E5 measures only `layer_aware`, using the unchanged 5,387-byte canonical E2 penalty transaction from the real stale-state punishment path. The sweep contains 10, 100, 1,000, 10,000, and 100,000 states. Each physically generated record is an 8-byte big-endian state ID followed by the unchanged penalty blob; `total_bytes` is the `os.Stat` size after streaming generation, flush, fsync, and close.

The registered scan is a deterministic worst-case search for the final state. It examines identifiers sequentially, skips nonmatching fixed payloads by mmap offset, and returns the matching blob. Files are prefaulted before 100 discarded warm-ups and 1,000 measured scans. `scan_cpu_ms` is the median Linux `CLOCK_PROCESS_CPUTIME_ID` process CPU time; p95 and p99 remain in condition manifests. Large synthetic artifacts were fully validated during collection but are not archived; their hashes, measured sizes, and validation receipts remain in `raw/e5/`.

## E9 network sensitivity

E9 is network-only. In its linear Mininet path, `hops` is the number of payment-channel links, not switches, and `rtt_ms` is the logical configured RTT of each link. To compensate for systematic host/Mininet/Linux processing overhead, the final testbed calibration subtracts 0.75 ms per channel before configuring `TCLink`: the injected one-way delay is `(rtt_ms - 0.75)/2`. Thus logical RTTs 5, 20, 50, and 100 ms use one-way delays 2.125, 9.625, 24.625, and 49.625 ms respectively. The compensation is a host/testbed calibration, not part of the logical RTT; expected full-path RTT remains `hops*rtt_ms`.

Before each condition, 20 official full-path ping samples were retained. Their project-defined median had to deviate by strictly less than 10% from `hops*rtt_ms`; exactly 10% fails. All 60 final conditions—three configurations, four RTTs, and five hop counts—passed this gate.

The HTLC executor moves deterministic inert, already-prepared buffers whose authoritative serialized sizes come from E2: 222 bytes (`classical`), 10,648 bytes (`uniform_mldsa`), and 3,252 bytes (`layer_aware`). These buffers model network size and are not independently valid cryptographic transactions. E9 performs no signing, verification, key generation, or serialization inside the timed measurement. Each neighboring direction uses a persistent `TCP_NODELAY` connection established before timing. Its one-byte message type plus four-byte big-endian payload length is E9 transport framing, not part of E2 transaction serialization.

One completion sample starts immediately before sender A writes `HTLC_ADD`, relays that complete message across the whole path to the receiver, relays `HTLC_SETTLE` back, and stops immediately after A receives the complete returned frame. TCP setup is outside timing and samples are not pipelined. Every final condition used 100 discarded warm-ups and 1,000 retained measurements with zero completion failures. Median, p95, and audit-only p99 use linear interpolation at rank `p*(n-1)`; the professor CSV intentionally includes only median and p95. The final 60-condition output is `data/e9_network.csv`, and canonical frozen evidence is under `raw/e9/`.

## Reproduction and validation

Install Caliper and the retained timing patch:

```bash
cd env/caliper/e1
./setup_caliper_e1.sh
```

Rebuild the patched Fabric images only when required:

```bash
cd env/fabric/e1
./build_fabric_pq.sh
```

Controlled E1 performance measurements require AC power, low unrelated load, no unrelated Docker containers, CPUs 2-15, amd-pstate passive mode, performance governor, boost disabled, and min=max frequency of 3,201,000 kHz. `env/caliper/e1/setup_cpu_e1.sh` configures the required CPU state.

Run an allowlisted profile with a unique namespace through `env/caliper/e1/run_e1.sh`. Transaction/block evidence must be extracted from the same live ledger before teardown or switching configuration.

Deterministic validation:

```bash
python3 -m json.tool meta.json >/dev/null
python3 -m unittest -v src/e1/test_analyze_timings.py
bash env/caliper/e1/test_run_policy.sh
./src/e1/analyze_timings.py --final-e1 --output /tmp/e1_fabric.regenerated.csv
cmp -- data/e1_fabric.csv /tmp/e1_fabric.regenerated.csv
sha256sum data/e0_primitives.csv data/e1_fabric.csv
```

E2 requires the pinned liboqs/liboqs-go versions above, Go 1.22.2, Mininet 2.3.0, and `tc`. Its scientific run used AMD `amd-pstate-epp`, performance governor and EPP, boost disabled, AC power, min=max frequency of 3,200,000 kHz, and process affinity `2,4,6,8,10,12,14`. Confirm these controls before running; the scientific runner records and validates them and requires a clean Git worktree.

Point `LIBOQS_GO_PC_DIR` to the directory containing `liboqs-go.pc` from the pinned liboqs-go v0.15.0 checkout or installation, then verify dependency discovery:

```bash
export LIBOQS_GO_PC_DIR=/path/to/liboqs-go/.config
export PKG_CONFIG_PATH="$LIBOQS_GO_PC_DIR:/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
pkg-config --modversion liboqs-go
pkg-config --cflags --libs liboqs-go
mn --version
```

Run E2 tests:

```bash
(cd src/e2 && go test ./...)
python3 -m unittest -v src/e2/test_rtt_runner.py
```

Run one scientific condition to a new directory outside the worktree:

```bash
sudo env PATH="$PATH" PYTHONPATH="$PWD" \
  PKG_CONFIG_PATH="$LIBOQS_GO_PC_DIR:/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  taskset -c 2,4,6,8,10,12,14 \
  python3 env/mininet/e2/run_e2.py \
    --config classical --transition penalty \
    --warmup 100 --iterations 1000 --ping-samples 20 \
    --output-root /tmp/e2_classical_penalty --scientific
```

Run the deterministic 24-condition sweep; the wrapper refuses an existing output directory and delegates every condition to the same runner:

```bash
sudo env PATH="$PATH" PYTHONPATH="$PWD" \
  PKG_CONFIG_PATH="$LIBOQS_GO_PC_DIR:/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  taskset -c 2,4,6,8,10,12,14 \
  env/mininet/e2/run_e2_sweep.sh /tmp/e2_full
```

Validate retained E2 evidence and regenerate the final CSV:

```bash
python3 src/e2/finalize_e2.py --evidence-root raw/e2 \
  --output /tmp/e2_statemachine.regenerated.csv
cmp -- data/e2_statemachine.csv /tmp/e2_statemachine.regenerated.csv
sha256sum data/e2_statemachine.csv
```

E5 reuses the pinned E2/liboqs environment and the same controlled CPU affinity and power policy. Run its tests:

```bash
(cd src/e5 && go test ./... && go vet ./...)
python3 -m unittest -v src/e5/test_finalize_e5.py
```

Run one scientific condition to new external directories, or run the complete five-point sweep:

```bash
(cd src/e5 && taskset -c 2,4,6,8,10,12,14 go run ./cmd/e5bench \
  --n-states 1000 --warmup 100 --iterations 1000 \
  --output-root /tmp/e5_n1000_evidence \
  --artifact-root /tmp/e5_n1000_artifact --scientific)

env/e5/run_e5_sweep.sh /tmp/e5_full /tmp/e5_artifacts
```

While generated artifacts exist, request the optional deep audit. Canonical frozen regeneration needs only `raw/e5/`:

```bash
python3 src/e5/finalize_e5.py --evidence-root /tmp/e5_full \
  --artifact-root /tmp/e5_artifacts --output /tmp/e5_watchtower.deep.csv
python3 src/e5/finalize_e5.py --evidence-root raw/e5 \
  --output /tmp/e5_watchtower.regenerated.csv
cmp -- data/e5_watchtower.csv /tmp/e5_watchtower.regenerated.csv
sha256sum data/e5_watchtower.csv
```

E9 tests and frozen finalization do not require the scientific CPU policy. A new scientific measurement does: AC power, `amd-pstate-epp`, performance governor and EPP, boost disabled, min=max frequency of 3,200,000 kHz, and process affinity `2,4,6,8,10,12,14`. Validate and regenerate the completed E9 result from retained evidence with:

```bash
python3 -m unittest discover -s src/e9 -p 'test*.py' -v
python3 src/e9/finalize_e9.py --evidence-root raw/e9 \
  --output /tmp/e9_network.regenerated.csv
cmp -- data/e9_network.csv /tmp/e9_network.regenerated.csv
sha256sum data/e9_network.csv raw/e9/evidence_manifest.json
```

## Evidence storage and provenance

`meta.json` is the machine-readable project and experiment provenance record.

E1 raw evidence is frozen under `raw/e1/` using deterministic gzip storage (`gzip -9 -n`). `raw/e1/evidence_manifest.json` records, for every retained source file, both:

- the SHA-256 of the original/decompressed scientific evidence bytes
- the SHA-256 of the tracked gzip container

Scientific raw-evidence identity is defined by the original/decompressed bytes. Compression changes storage representation only and does not change any E1 result.

E2 raw evidence is frozen under `raw/e2/` with the same deterministic `gzip -9 -n` convention. `raw/e2/evidence_manifest.json` maps all 72 original condition files to tracked gzip containers and records both original and tracked SHA-256 identities. `src/e2/finalize_e2.py` validates those identities and all condition-level scientific gates before regenerating the final E2 CSV.

E5 compact raw evidence is frozen under `raw/e5/` using deterministic `gzip -9 -n`. Its evidence manifest maps the ten retained condition manifests/sample files to their gzip containers. The synthetic storage artifacts are omitted after deep validation; frozen regeneration verifies their recorded hashes, measured sizes, validation receipts, and retained raw samples.

E9 raw evidence is frozen under `raw/e9/` using `LC_ALL=C gzip -9 -n`. Its top-level manifest maps all 180 original condition manifests, completion CSVs, and ping outputs to deterministic gzip containers while preserving original and tracked byte counts and SHA-256 identities. `src/e9/finalize_e9.py` revalidates the exact 60-condition Cartesian product, scientific environment, calibration, ping gates, raw samples, and statistics before regenerating the byte-identical final CSV.
