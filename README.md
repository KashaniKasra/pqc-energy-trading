# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository contains the implementation and measurement code for the experimental evaluation of layer-aware post-quantum cryptography in blockchain-based energy trading.

The project focuses on measuring the real behavior of the system. Experimental results are reported as measured and are not adjusted to match expected results.

## Current Status

Completed:

- Baseline Hyperledger Fabric environment
- Raft-based Fabric test network verification
- Mininet installation and connectivity test
- E0 cryptographic primitive benchmark on the server platform
- Raw E0 timing samples and summary statistics

Not yet completed:

- E0 meter-platform measurements
- E1 and later experiments

## Repository Structure

```text
.
├── README.md
├── meta.json
├── data/
│   └── e0_primitives.csv
├── env/
│   ├── caliper/
│   ├── fabric/
│   ├── mininet/
│   └── setup/
│       └── install-fabric.sh
├── figures/
├── raw/
│   └── e0/
│       ├── e0_server_*.csv.gz
│       └── e0_server_run.log
└── src/
    └── e0/
        ├── benchmark_test.go
        ├── ecdsa_benchmark.go
        ├── go.mod
        ├── go.sum
        ├── main.go
        ├── oqs_benchmark.go
        ├── output.go
        ├── run_e0_server.sh
        ├── stats.go
        └── timing.go
```

- `env/`: environment and experiment configuration
- `src/`: experiment source code
- `data/`: final summary CSV files
- `raw/`: compressed raw measurement samples
- `figures/`: generated figures
- `meta.json`: pinned software versions, hardware information, parameter sets, and benchmark metadata

## Environment

The current server platform is:

- CPU: AMD Ryzen 7 7735HS
- Architecture: x86_64
- Physical cores: 8
- Logical CPUs: 16
- RAM: 16 GiB DDR5-4800
- OS: Ubuntu 24.04.3 LTS
- Kernel: 6.14.0-37-generic

Main software versions:

- Hyperledger Fabric: 2.5.16
- Fabric CA: 1.5.17
- Go: 1.22.2
- OpenSSL: 3.0.13-0ubuntu3.12
- liboqs: 0.15.0
- liboqs-go: v0.15.0
- Mininet: 2.3.0
- Docker: 28.2.2

Exact release tags, commits, and other environment information are recorded in `meta.json`.

## Baseline Verification

Hyperledger Fabric was configured using Raft (`etcdraft`) ordering.

The baseline test network used:

- Channel: `energychannel`
- Organizations: `Org1MSP` and `Org2MSP`
- Chaincode: `basic` v1.0

Both peers successfully joined the channel. The chaincode was installed, approved, and committed.

`InitLedger` and `GetAllAssets` were executed successfully, confirming basic end-to-end Fabric operation and ledger state persistence.

Mininet 2.3.0 was also verified using its default `pingall` smoke test. The `h1-s1-h2` topology completed with 0% packet loss.

## E0 — Cryptographic Primitive Benchmark

E0 measures the cryptographic primitives directly, without Hyperledger Fabric or Mininet in the timing path.

The following parameter sets are benchmarked:

- ML-KEM-768
- ML-DSA-44
- ML-DSA-65
- ML-DSA-87
- Falcon-512
- SPHINCS+-SHA2-128s
- ECDSA P-256

The exact liboqs algorithm string used for SPHINCS+ is:

```text
SPHINCS+-SHA2-128s-simple
```

ECDSA P-256 uses the OpenSSL `prime256v1` curve.

### Operations

Signature schemes:

- key generation
- signing
- verification

ML-KEM-768:

- key generation
- encapsulation
- decapsulation

### Benchmark Method

For the server benchmark:

- Warm-up iterations: 100
- Measured iterations: 10,000 per data point
- `GOMAXPROCS=1`
- CPU affinity: logical CPUs 2 and 3
- CPUs 2 and 3 are SMT siblings of the same physical core
- CPU governor: `performance`
- CPU frequency: fixed at 3.20 GHz
- AMD P-state mode: `passive`
- CPU boost: disabled
- Laptop connected to AC power
- Major background workloads removed from the benchmark CPUs

The benchmark records every measured iteration.

For each data point, the final summary reports:

- median
- p95
- p99
- sample standard deviation

The mean is not used as the only latency statistic.

Percentiles are calculated using linear interpolation at rank:

```text
p * (n - 1)
```

Sample standard deviation uses denominator:

```text
n - 1
```

### Benchmark Input

All signature algorithms use the same 32-byte message.

The message is generated deterministically as:

```text
SHA-256("pqc-energy-trading-e0-message-v1")
```

The seed and generation method are also recorded in `meta.json`.

ML-KEM does not use a message input.

### Cryptographic Backend

liboqs is called through its direct library API. `oqs-provider` is not used.

liboqs was built with OpenSSL support and uses OpenSSL for its SHA-2 backend.

ECDSA uses OpenSSL `libcrypto` directly with the `prime256v1` curve.

For ECDSA signing and verification, SHA-256 hashing is performed outside the measured timing region. The measured region contains the cryptographic primitive call.

The low-level OpenSSL ECDSA API used by the benchmark is deprecated in OpenSSL 3.0, but it is intentionally used here for direct primitive measurement without provider dispatch.

### Falcon Note

Falcon-512 in liboqs 0.15.0 follows the round-3 Falcon submission rather than the final FIPS 206 specification. This is recorded in `meta.json`.

## E0 Output

The summary file is:

```text
data/e0_primitives.csv
```

with the schema:

```text
platform,scheme,operation,n_iter,median_ms,p95_ms,p99_ms,stddev_ms
```

Raw timing samples are stored as compressed CSV files under:

```text
raw/e0/
```

The benchmark execution log is stored at:

```text
raw/e0/e0_server_run.log
```

Raw measurements are retained so that summary statistics can be checked or recalculated later.

## Build and Test E0

liboqs and liboqs-go must be installed using the exact versions recorded in `meta.json`.

Make sure pkg-config can find the installed liboqs package:

```bash
export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH
```

Then:

```bash
cd src/e0
go test ./...
go build -o e0bench .
```

OpenSSL 3.0 may report deprecation warnings for the low-level ECDSA functions. These warnings do not indicate a build or test failure.

## Run E0

From the E0 source directory:

```bash
cd src/e0
./run_e0_server.sh
```

The benchmark should only be run after applying the CPU settings recorded in `meta.json`.

The server runner executes all required scheme/operation combinations sequentially.

## Reproducibility

The exact software versions, release tags, commits, hardware information, cryptographic parameter-set strings, message-generation method, and E0 timing configuration are recorded in:

```text
meta.json
```

Raw measurements are retained separately from the summary results.

E0 cryptographic measurements are performed independently from network emulation. Mininet is not included in cryptographic primitive timing.
