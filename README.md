# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical testbed for measuring cryptographic, blockchain, and communication-network costs. Reported values are derived from retained measurements rather than tuned to expected trends.

## Status and outputs

- E0 primitives, server: complete.
- E0 primitives, meter/SBC: pending hardware measurement.
- E1 Fabric: complete.
- E2 measurement infrastructure and canonical serialization: implemented; final measurements pending.
- E9 network-measurement infrastructure: implemented; final sweep pending.
- E5, E7, and E8: not yet completed.

Final completed-experiment CSVs:

- `data/e0_primitives.csv`
- `data/e1_fabric.csv`

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

Falcon-512 in liboqs 0.15.0 is the round-3 implementation and must not be described as final FIPS 206.

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

## E2 channel-state serialization

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

E2 RTT uses two Mininet hosts joined by one direct 20 ms RTT `TCLink`. A prepares and signs the canonical transaction before timing, starts a monotonic timer immediately before sending the serialized bytes over an already-established `TCP_NODELAY` connection, and stops only after receiving B's success ACK. B sends that ACK only after verifying every included signature over the canonical semantic preimage. Thus RTT includes transmission, receive/framing, real verification at B, and ACK return, while excluding key generation, signing, transaction construction, serialization, and TCP setup. Transport framing and ACK bytes do not contribute to `message_bytes`. The classical verification remains one Ed25519 verification for the representative aggregated-size case; it is not a MuSig2 execution. Each scientific condition requires a retained full ping output passing the strict 20 ms ±10% gate, retains warm-up, measured, and failed raw rows, and uses collision-refusing evidence files with hashes and environment provenance. Final E2 measurement and `data/e2_statemachine.csv` remain pending.

## E9 network sensitivity

E9 is network-only. In its linear Mininet path, `hops` is the number of payment-channel links and `rtt_ms` is the configured RTT of each link; each interface direction receives `rtt_ms/2` delay. The official full-path ping median must be within strictly less than 10% of `hops*rtt_ms` before a scientific condition may run.

The HTLC executor moves inert, already-prepared buffers whose authoritative serialized sizes come from E2: 222 bytes (`classical`), 10,648 bytes (`uniform_mldsa`), and 3,252 bytes (`layer_aware`). It performs no cryptography or serialization. Each neighboring direction uses a persistent `TCP_NODELAY` connection established before timing. Its one-byte message type plus four-byte big-endian payload length is E9 transport framing, not part of E2 transaction serialization.

One completion sample starts immediately before sender A writes `HTLC_ADD`, relays that complete message to the receiver, relays `HTLC_SETTLE` back, and stops immediately after A receives the complete returned frame. Samples are not pipelined. The scientific default is 100 warm-up iterations (an implementation choice) followed by at least 1,000 retained measured iterations. Official ping stdout and every completion record are retained with SHA-256 provenance. The 60-condition final sweep and `data/e9_network.csv` have not yet been run or created.

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

## Evidence storage and provenance

`meta.json` is the machine-readable project and experiment provenance record.

E1 raw evidence is frozen under `raw/e1/` using deterministic gzip storage (`gzip -9 -n`). `raw/e1/evidence_manifest.json` records, for every retained source file, both:

- the SHA-256 of the original/decompressed scientific evidence bytes
- the SHA-256 of the tracked gzip container

Scientific raw-evidence identity is defined by the original/decompressed bytes. Compression changes storage representation only and does not change any E1 result.
