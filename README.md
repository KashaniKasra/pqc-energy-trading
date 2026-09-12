# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository is an empirical testbed for measuring cryptographic, Fabric,
and network costs in a blockchain-based energy-trading setting. The governing
principle is to measure what the system actually does and retain the raw samples
behind every reported value. Unexpected results and saturation failures are
evidence, not reasons to tune the experiment.

The implementation authority is docs/student_spec(1).pdf. Later direct
professor clarification overrides that document only for the clarified point;
the proposal is background and has lower authority.

## Status

- E0 server measurements are complete and retained. The required meter/SBC
  measurements are not complete.
- E1 Fabric/PQ implementation, clean common-profile latency runs, exact
  transaction-size evidence, identity public-key measurements, PQ verification
  audits, and integer sustainable-rate boundaries are complete.
- E1 block utilisation is final for ECDSA. ML-DSA-44, ML-DSA-65, and SPHINCS+
  remain pending professor methodology review; their existing candidate block
  evidence must be retained.
- data/e1_fabric.csv does not exist yet because E1 is incomplete.
- E2, E5, E7, E8, and E9 have not been measured.
- The specification refers to a supplied make_figures.py sanity checker, but
  that source and its plausible bands are absent. No replacement is fabricated.

## Repository layout

    README.md
    meta.json
    env/       Fabric, Caliper, and later network configurations
    src/       benchmark, analyzer, chaincode, identity, and evidence code
    data/      retained derived CSVs; final deliverables only when complete
    raw/       retained raw measurements
    figures/   generated final PDFs only

Raw E1 evidence is intentionally untracked while its final KEEP set and
compression layout are being frozen. Do not delete or overwrite raw/e1.

## Exact software and hardware provenance

The full machine, source, image, and experiment metadata is in meta.json.
Important pins are:

- Hyperledger Fabric v2.5.16 at
  f871cf92a026aba7b12e6f06d71ded3e6e659d71
- liboqs 0.15.0 at
  97f6b86b1b6d109cfd43cf276ae39c2e776aed80
- liboqs-go v0.15.0 at
  75451133b94a6c4be5f528eef94916ce08475f24
- Hyperledger Caliper 0.7.1
- Fabric build Go 1.26.4

The current trace-capable images are:

- fabric-peer:2.5.16-pq:
  sha256:3d71da5ac46b179c981527b69ac832cb7905bd3bc428ee6b2620bc580e3c944e
- fabric-orderer:2.5.16-pq:
  sha256:4ec6f0287e3fda35a874357bfe9c9f2ef8b9fbb427a42457de3b6b1b98290058
- patch SHA-256:
  5268f9f67656233d7e7b9ac7c449f20f05fc8a8257fb91eb0de63698a72d1293

Superseded image IDs remain in meta.json only because retained measurements
explicitly record them. No current image is retroactively attributed to an old
run.

## E0 primitives

E0 uses direct liboqs APIs and OpenSSL/libcrypto for ECDSA P-256. The
deterministic 32-byte signature message is:

    SHA-256("pqc-energy-trading-e0-message-v1")

ECDSA hashes outside the timed sign/verify region. The canonical server run
used 100 discarded warm-ups, 10,000 measured iterations, GOMAXPROCS=1, logical
CPUs 2 and 3, amd_pstate passive mode, performance governor, boost disabled,
approximately 3.2 GHz fixed frequency, and AC power. Raw samples are compressed
under raw/e0; data/e0_primitives.csv contains median, p95, p99, and supporting
statistics. Do not rerun it automatically.

To build the harness without measuring:

    cd src/e0
    go test ./...
    go build ./...

The measurement entry point is src/e0/run_e0_server.sh. A controlled human must
run it only after verifying the recorded CPU and power conditions.

## E1 Fabric architecture

E1 is a two-organization Fabric 2.5 testbed with two peers per organization,
one etcdraft/Raft orderer, channel energychannel, and simple Set/Get chaincode.
It intentionally contains no energy-market logic.

Stock Fabric does not natively provide these PQ peer identity signing paths.
The patch implements an experimental hybrid mechanism:

- standard ECDSA X.509 membership and CA signatures remain;
- experimental noncritical OID 1.3.6.1.3.9999.1 carries the PQ algorithm and
  raw PQ public key;
- PQ SKI is SHA-256(raw PQ public key);
- patched BCCSP imports such certificates as PQ public keys and uses liboqs for
  peer transaction/endorsement signing and verification;
- client identity, TLS identities, and orderer identity remain ECDSA.

This is not full PQ PKI, standardized PQ X.509, or native Fabric PQ identity
support. The exact algorithms are ML-DSA-44, ML-DSA-65, and
SPHINCS+-SHA2-128s-simple. Falcon-512 in pinned liboqs is the round-3
implementation and must not be described as final FIPS 206.

Runtime audits in data/e1_pq_verification_audits.csv show successful calls to
oqs.Signature.Verify on the orderer and both organizations for every PQ scheme.
The regression test TestPQVerifierDispatchRejectsClassicalFallback proves that
valid PQ signatures pass, tampered PQ signatures fail, and an ECDSA signature
presented to a PQ public key fails without classical fallback. Verification
tracing is one-shot functional evidence only; run_e1.sh rejects trace logging
during performance runs.

## E1 Fabric parameters

The declared and decoded effective values are identical across retained clean
common-profile runs:

- BatchTimeout: 2s
- MaxMessageCount: 500
- PreferredMaxBytes: 2 MiB = 2,097,152 bytes
- AbsoluteMaxBytes: 10 MiB = 10,485,760 bytes
- ordering: etcdraft
- chaincode endorsement: Application ImplicitMeta MAJORITY
- empirical endorsements per retained transaction: exactly 2

The E1 CPU-controlled state is CPUs 2-15, amd_pstate passive, boost off,
performance governor, and min=max=3,201,000 kHz. Caliper runs under
taskset -c 2-15; Fabric and chaincode containers use cpuset 2-15. Final runs
require AC power, low background load, and no unrelated Docker containers.

## Reproducing E1 setup

Build the patched images only when a rebuild is required:

    cd env/fabric/e1
    ./build_fabric_pq.sh

Provisioning a selected configuration always creates a fresh matching network:

    ./setup_fabric_e1.sh ecdsa
    ./setup_fabric_e1.sh ml-dsa-44
    ./setup_fabric_e1.sh ml-dsa-65
    ./setup_fabric_e1.sh sphincs

The runner provisions fresh state itself. Its scientific allowlist binds each
profile to its permitted configuration and run type, requires labels for
noncanonical runs, refuses namespace collisions, records source/profile/image/
CPU/block provenance, and rejects PQ trace logging.

The canonical common workload is env/caliper/e1/benchmark.yaml, SHA-256:

    5ae8aa973467a7828237fbc097c8e3936d5a05196de3cfd2881f550b8afd6e1a

It contains a discarded 20-second 50-TPS warm-up, 120 seconds at 50 TPS, and
120 seconds at 200 TPS. It must not be changed. Professor-selected final
latency uses the 200-TPS population; 50 TPS remains supporting evidence.

## E1 timing semantics

Endorsement timing covers transaction endorsement evaluation in PeerGateway.
The column called commit latency measures the precise interval
post_endorsement_submit_to_commit_status_ms: it starts after endorsement,
immediately before transaction.submit(), and ends after subtx.getStatus().
It includes client signing/status work, Gateway RPC/client waiting, orderer
submission and acknowledgement, block cutting or BatchTimeout waiting,
validation, ledger commit, and commit-status delivery. It excludes proposal
construction, endorsement, and Caliper scheduling before connector invocation.
It is not pure peer commit-processing time.

data/e1_latency_clean_professor_review.csv is deterministically regenerated by:

    ./src/e1/analyze_timings.py --clean-latency-professor-review \
      --output data/e1_latency_clean_professor_review.csv --replace

It retains separate 50- and 200-TPS rows. ECDSA, ML-DSA-44, and ML-DSA-65
have valid clean 200-TPS latency populations. SPHINCS+ saturated at both rates;
its success/error outcomes are reportable, but its timing fields remain blank.

## E1 identity and transaction size

Professor-defined identity_bytes is the actual public-key representation used
by Fabric, not MSP signcert PEM size and not signature size. Arithmetic mean,
min, max, and dispersion are retained:

| Configuration | public-key bytes (mean=min=max) |
| --- | ---: |
| ECDSA P-256 DER SubjectPublicKeyInfo | 91 |
| ML-DSA-44 raw liboqs key | 1,312 |
| ML-DSA-65 raw liboqs key | 1,952 |
| SPHINCS+-SHA2-128s-simple raw liboqs key | 32 |

The older per-peer MSP signcert measurements remain supporting evidence only.

Transaction size is measured from each exact serialized common.Envelope byte
string in fetched blocks. It is never estimated from block size divided by
transaction count. Embedded endorsements are counted for every included
transaction:

| Configuration | n | tx_bytes_mean | endorsements_per_tx |
| --- | ---: | ---: | ---: |
| ECDSA | 6,001 | 3,929.301450 | 2 |
| ML-DSA-44 | 6,001 | 12,288.617897 | 2 |
| ML-DSA-65 | 6,001 | 15,803.491918 | 2 |
| SPHINCS+-SHA2-128s-simple | 1,201 | 19,711.570358 | 2 |

The extractor is env/fabric/e1/measure_block_bytes.sh backed by
src/e1/evidence/cmd/blockinspect. It must run against the same live ledger,
before teardown or setup of another configuration.

## E1 sustainable throughput

Each immutable probe has a discarded warm-up and one 60-second measured round.
A tested rate passes only when all gates pass:

- tx_success_rate >= 0.99;
- successful throughput = success_count / 60 >= 0.95 times configured TPS;
- successful begin/end windows each contain at least five samples;
- request-start windows are [0,12000) ms and [48000,60000) ms;
- end-window p95 <= 2 times begin-window p95;
- percentiles use linear interpolation at rank p*(n-1).

Only each adjacent PASS/FAIL pair is the permanent basis for the final integer
boundary:

| Configuration | highest PASS | first FAIL | tps_sustained |
| --- | ---: | ---: | ---: |
| ECDSA | 228 | 229 | 228 |
| ML-DSA-44 | 356 | 357 | 356 |
| ML-DSA-65 | 344 | 345 | 344 |
| SPHINCS+-SHA2-128s-simple | 22 | 23 | 22 |

The professor-required SPHINCS+ sweep at 1, 2, 5, 10, and 20 TPS remains
retained separately. Its low-rate 60-second populations contain fewer than
1,000 requests by design; the later professor-specified duration is the
topic-specific authority.

## E1 block utilisation

The definition is:

    mean(actual fetched ordinary-transaction block file bytes)
    / effective PreferredMaxBytes (2,097,152)

Genesis, config, and other nonordinary blocks are excluded. Terminal ordinary
blocks are retained. A final population must be traffic-volume-driven rather
than primarily cut by BatchTimeout.

ECDSA final evidence is namespace blockutil-300_ecdsa, blocks 17-48. Blocks
17-47 each contain MaxMessageCount=500 transactions, and terminal ordinary
block 48 contains 386:

- ordinary blocks: 32
- block_bytes_mean: 1,952,347.156250
- block_utilisation: 0.930951670

The 31 full-block diagnostic comparison is 1,966,319.451613 bytes and
0.937614179; it does not replace the boundary-inclusive result. The difference
is 0.666250900 percentage points.

Pending evidence that must not be deleted:

- ML-DSA-44 sustained-200-v1 same-ledger fetched blocks/transactions;
- ML-DSA-65 sustained-345-v1 same-ledger fetched blocks/transactions;
- SPHINCS+ 20-TPS timeout-driven evidence and its unrun 54-TPS diagnostic
  profile.

No final ML-DSA-44, ML-DSA-65, or SPHINCS+ block-utilisation value is claimed
until the professor resolves the remaining population methodology.

## Deterministic validation

Safe non-performance validation:

    python3 -m py_compile src/e1/analyze_timings.py
    python3 -m unittest src/e1/test_analyze_timings.py
    bash env/caliper/e1/test_run_policy.sh
    bash -n env/caliper/e1/run_e1.sh
    bash -n env/caliper/e1/run_policy.sh
    node --check env/caliper/e1/workload/set.js
    go test ./...

The last command must be run separately in each Go module under src/e0,
src/e1/chaincode, src/e1/pqidentity, and src/e1/evidence. None of these
commands launches a performance workload.

Useful deterministic E1 views:

    ./src/e1/analyze_timings.py --identity-public-keys
    ./src/e1/analyze_timings.py --endorsement-policy
    ./src/e1/analyze_timings.py --pq-verification-audits
    ./src/e1/analyze_timings.py --working-e1

The working E1 view is stdout-only and cannot be mistaken for
data/e1_fabric.csv. It leaves unresolved block fields empty and populates only
retained, validated values.

## Raw-data policy

Never delete a run needed for a final claim, an adjacent boundary, SPHINCS+
saturation, PQ verification, identity/transaction evidence, or a pending block
decision. Once E1 block methodology is settled, the KEEP manifest will be
frozen, raw CSV/log evidence will be gzip-compressed without concatenating
namespaces, SHA-256 provenance will be updated to the compressed bytes, and
readers will be adapted deterministically. Compression must not happen while
the pending block populations are under review.
