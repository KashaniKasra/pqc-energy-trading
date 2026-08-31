# Layer-Aware Post-Quantum Cryptography for Blockchain-Based Energy Trading

This repository contains the implementation and measurement code for the experimental evaluation of layer-aware post-quantum cryptography in blockchain-based energy trading.

The project focuses on measuring the real behavior of the system. Experimental results are reported as measured and are not adjusted to match expected results.

## Current Status

Completed:

- Baseline Hyperledger Fabric environment
- Raft-based Fabric E1 network with 2 organizations and 2 peers per organization
- Reproducible E1 Fabric setup script
- Reproducible Caliper 0.7.1 setup with pinned dependencies
- E1 endorsement and commit timing instrumentation
- Fixed E1 server CPU measurement configuration
- Mininet installation and connectivity test
- E0 cryptographic primitive benchmark on the server platform
- Raw E0 timing samples and summary statistics

Not yet completed:

- E0 meter-platform measurements
- E1 post-quantum Fabric identity configurations
- Final E1 measurements and `data/e1_fabric.csv`
- E2 and later experiments

## Repository Structure

```text
.
├── README.md
├── meta.json
├── .gitignore
├── data/
│   └── e0_primitives.csv
├── env/
│   ├── caliper/
│   │   └── e1/
│   │       ├── benchmark.yaml
│   │       ├── connection-org1.yaml
│   │       ├── connection-org2.yaml
│   │       ├── network.yaml
│   │       ├── package.json
│   │       ├── package-lock.json
│   │       ├── patches/
│   │       │   └── peer-gateway-e1-timing.patch
│   │       ├── run_e1.sh
│   │       ├── setup_caliper_e1.sh
│   │       ├── setup_cpu_e1.sh
│   │       └── workload/
│   │           └── set.js
│   ├── fabric/
│   │   └── e1/
│   │       ├── configtx.yaml
│   │       ├── crypto-config.yaml
│   │       ├── docker-compose.yaml
│   │       └── setup_fabric_e1.sh
│   ├── mininet/
│   │   └── .gitkeep
│   └── setup/
│       └── install-fabric.sh
├── figures/
│   └── .gitkeep
├── raw/
│   └── e0/
│       ├── e0_server_run.log
│       └── e0_server_*.csv.gz
└── src/
    ├── e0/
    │   ├── benchmark_test.go
    │   ├── ecdsa_benchmark.go
    │   ├── go.mod
    │   ├── go.sum
    │   ├── main.go
    │   ├── oqs_benchmark.go
    │   ├── output.go
    │   ├── run_e0_server.sh
    │   ├── stats.go
    │   └── timing.go
    └── e1/
        └── chaincode/
            ├── chaincode.go
            ├── go.mod
            └── go.sum
```

- `data/`: final summary CSV files.
- `env/`: reproducible environment, network, benchmark, and setup configuration.
- `env/fabric/e1/`: Hyperledger Fabric E1 topology and setup automation.
- `env/caliper/e1/`: Caliper E1 benchmark configuration, workload, pinned dependencies, timing patch, and execution scripts.
- `src/e0/`: E0 cryptographic primitive benchmark implementation.
- `src/e1/chaincode/`: minimal E1 key/value chaincode used to generate Fabric transactions.
- `raw/`: retained raw measurement samples.
- `figures/`: generated experiment figures.
- `meta.json`: hardware information, exact software versions, cryptographic parameter sets, and experiment metadata.

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
- Node.js: 22.23.2
- npm: 12.0.2
- Hyperledger Caliper: 0.7.1
- Caliper Fabric connector: 0.7.1
- Fabric Gateway SDK: 1.7.1
- @grpc/grpc-js: 1.13.1

Exact release tags, commits, dependency versions, and other environment information are recorded in `meta.json`.

## E1 Fabric Baseline

The E1 Fabric environment uses Hyperledger Fabric 2.5.16 with `etcdraft` ordering.

The network topology is:

- 2 organizations: `Org1MSP` and `Org2MSP`
- 2 peers per organization
- 1 Raft orderer
- Channel: `energychannel`
- Chaincode: `simplekv`
- Benchmark operation: `Set(key,value)`

The Fabric environment can be rebuilt using:

```bash
./env/fabric/e1/setup_fabric_e1.sh
```

The setup script:

- regenerates Fabric cryptographic material
- generates the genesis block and channel artifacts
- starts the orderer and four peers
- waits for the Raft leader and peer readiness
- creates `energychannel`
- joins all four peers
- applies the Org1 and Org2 anchor peer updates
- packages and installs `simplekv`
- approves and commits the chaincode definition
- performs a Set/Get smoke test
- verifies cross-organization discovery of all four peers

Generated Fabric cryptographic material, channel artifacts, and chaincode packages are not committed to the repository.

Mininet 2.3.0 was separately verified using its default `pingall` smoke test. The `h1-s1-h2` topology completed with 0% packet loss.

## E1 Caliper Setup

E1 uses Hyperledger Caliper 0.7.1.

The Caliper dependency tree is pinned by:

```text
env/caliper/e1/package.json
env/caliper/e1/package-lock.json
```

The environment is prepared using:

```bash
cd env/caliper/e1
./setup_caliper_e1.sh
```

The setup script installs the pinned dependencies and applies the repository-tracked E1 timing patch to the Caliper Fabric Peer Gateway connector.

The benchmark workload invokes only:

```text
Set(key,value)
```

The committed measurement profile currently contains:

- 120 seconds at 50 TPS
- 120 seconds at 200 TPS

The same workload profile is used across E1 cryptographic configurations.

The Caliper instrumentation records:

- endorsement latency around `proposal.endorse()`
- commit latency from `transaction.submit()` through `submittedTransaction.getStatus()`

Timing uses `process.hrtime.bigint()`.

Samples are retained in memory during each round and written to raw CSV files after the round completes.

## E1 CPU Measurement State

Before an E1 server measurement, the CPU state is prepared using:

```bash
./env/caliper/e1/setup_cpu_e1.sh
```

The E1 server measurement configuration is:

- fixed logical CPU set: `2-15`
- AMD P-state mode: `passive`
- CPU governor: `performance`
- CPU boost: disabled
- frequency scaling range on CPUs `2-15`: fixed to `3201000 kHz`

The E1 runner verifies the required CPU state before starting a measurement.

The base Fabric containers defined in the E1 Docker Compose configuration are assigned to logical CPUs `2-15`.

The Caliper process is launched with CPU affinity restricted to logical CPUs `2-15`.

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

Exact software versions, release tags, commits, hardware information, cryptographic parameter strings, CPU controls, Fabric parameters, and experiment metadata are recorded in:

```text
meta.json
```

### E0

E0 cryptographic measurements are performed independently from Hyperledger Fabric and Mininet.

Raw E0 measurements are retained separately from the summary results.

The E0 server benchmark is reproduced from:

```bash
cd src/e0
./run_e0_server.sh
```

using the CPU and software configuration recorded in `meta.json`.

### E1

The E1 environment is reproduced in the following order.

First, build the Fabric environment:

```bash
./env/fabric/e1/setup_fabric_e1.sh
```

Prepare Caliper:

```bash
cd env/caliper/e1
./setup_caliper_e1.sh
cd ../../..
```

Prepare the server CPU state:

```bash
./env/caliper/e1/setup_cpu_e1.sh
```

The following local hostname mappings must resolve before the Fabric setup is executed:

```text
127.0.0.1 orderer.example.com
127.0.0.1 peer0.org1.example.com peer1.org1.example.com peer0.org2.example.com peer1.org2.example.com
```

At the current implementation stage, only the ECDSA E1 identity configuration is enabled for measurement:

```bash
cd env/caliper/e1
./run_e1.sh ecdsa
```

ML-DSA-44, ML-DSA-65, and SPHINCS+-SHA2-128s-simple E1 measurements must not be run until their Fabric identity implementations are completed.

Raw measurements are retained separately from final summary files.

Final E1 results will be written to:

```text
data/e1_fabric.csv
```

with the required schema:

```text
config,identity_bytes,endorse_median_ms,endorse_p95_ms,commit_median_ms,tps_sustained,block_utilisation
```
