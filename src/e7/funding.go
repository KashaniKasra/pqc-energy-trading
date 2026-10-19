package e7

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

const (
	FundingManifestSchema          = "pqc-energy-trading.e7-funding-evidence.v2"
	FundingSmokeManifestSchema     = "pqc-energy-trading.e7-funding-integration-smoke.v1"
	FundingModeIntegrationSmoke    = "integration-smoke"
	FundingAuditSchema             = "pqc-energy-trading.e7-funding-transaction-audit.v1"
	FundingCommitmentSchema        = "e7-channel-funding-v1"
	FundingTimerDefinition         = "Linux CLOCK_MONOTONIC immediately before Fabric Gateway simplekv.Set submission through successful committed VALID commit-status return"
	SettlementMeasurementCommit    = "0a6d20cf06d8e452b0d9bbd85922baff058cdca0"
	FundingMeasuredDenominator     = 1000
	FundingInitialProsumerKWh      = "104.346"
	FundingInitialCounterpartyKWh  = "0"
	FundingClientAlgorithm         = "ECDSA-P256-SHA256"
	FundingClassicalPeerAlgorithm  = "ECDSA-P256-SHA256"
	FundingLayerAwarePeerAlgorithm = "ML-DSA-65-SHA256"
)

var RequiredFundingCodeConfigPaths = []string{
	"src/e7/cmd/e7funding/main.go", "src/e7/funding.go", "src/e7/fundingaudit.go",
	"src/e7/fabric.go", "src/e7/lifecycle.go", "src/e7/fabriccpu.go",
	"src/e7/onchain.go", "src/e7/manifest.go", "src/e7/versions.go", "src/e7/finalize_e7.py",
	"src/e7/go.mod", "src/e7/go.sum", "env/e7/fabric_network.sh",
	"env/e7/docker-compose.fabric-cpu.yaml", "env/e7/docker-shim/docker",
	"env/fabric/e1/setup_fabric_e1.sh", "env/fabric/e1/docker-compose.yaml",
	"env/fabric/e1/build_fabric_pq.sh", "env/fabric/e1/patches/fabric-2.5.16-pq-identities.patch",
	"env/fabric/e1/configtx.yaml", "env/fabric/e1/crypto-config.yaml",
	"src/e1/chaincode/chaincode.go", "src/e1/chaincode/go.mod", "src/e1/chaincode/go.sum",
	"src/e1/pqidentity/generate.go", "src/e1/pqidentity/go.mod", "src/e1/pqidentity/go.sum",
	"env/e7/trace/opsd_household_15min_e7.csv", "env/e7/trace/provenance.json",
}

type FundingFabricClient interface {
	PrepareRun(context.Context, Configuration) error
	SubmitAndAwaitCommit(context.Context, string, []byte) (FabricCompletion, error)
	ReadValue(context.Context, string) ([]byte, error)
	FetchBlock(context.Context, uint64) ([]byte, error)
	Close() error
}

type FundingResult struct {
	TargetConfig      Configuration           `json:"target_config"`
	RunID             string                  `json:"run_id"`
	Key               string                  `json:"key"`
	PayloadSHA256     string                  `json:"payload_sha256"`
	StartMonotonicNS  int64                   `json:"start_monotonic_ns"`
	EndMonotonicNS    int64                   `json:"end_monotonic_ns"`
	FundingDurationNS int64                   `json:"funding_duration_ns"`
	Completion        FabricCompletion        `json:"completion"`
	ReadbackSHA256    string                  `json:"readback_sha256"`
	ReadbackMatched   bool                    `json:"readback_matched"`
	Audit             FundingTransactionAudit `json:"transaction_audit"`
	BlockBytes        []byte                  `json:"-"`
}

type FundingManifest struct {
	Schema                   string                  `json:"schema"`
	Experiment               string                  `json:"experiment"`
	Mode                     string                  `json:"mode"`
	Scientific               bool                    `json:"scientific"`
	TargetConfig             Configuration           `json:"target_config"`
	FabricNetworkMode        string                  `json:"fabric_network_mode"`
	RunID                    string                  `json:"run_id"`
	SettlementCommit         string                  `json:"settlement_measurement_commit"`
	GitCommit                string                  `json:"git_commit"`
	GitTrackedClean          bool                    `json:"git_tracked_clean"`
	TraceSHA256              string                  `json:"trace_sha256"`
	TraceProvenanceSHA256    string                  `json:"trace_provenance_sha256"`
	WarmupRowsExcluded       int                     `json:"warmup_rows_excluded"`
	MeasuredDenominator      int                     `json:"measured_settlement_denominator"`
	Channel                  string                  `json:"fabric_channel"`
	Chaincode                string                  `json:"fabric_chaincode"`
	Function                 string                  `json:"fabric_function"`
	CommitmentKey            string                  `json:"commitment_key"`
	CommitmentPayloadSHA256  string                  `json:"commitment_payload_sha256"`
	CommitmentInterpretation string                  `json:"commitment_interpretation"`
	GatewayClientAlgorithm   string                  `json:"gateway_client_signature_algorithm"`
	PeerEndorsementAlgorithm string                  `json:"peer_endorsement_signature_algorithm"`
	TimerDefinition          string                  `json:"timer_definition"`
	StartMonotonicNS         int64                   `json:"start_monotonic_ns,omitempty"`
	EndMonotonicNS           int64                   `json:"end_monotonic_ns,omitempty"`
	FundingDurationNS        int64                   `json:"funding_duration_ns,omitempty"`
	Completion               *FabricCompletion       `json:"completion,omitempty"`
	ReadbackVerified         bool                    `json:"independent_ledger_readback_verified"`
	TransactionAuditVerified bool                    `json:"transaction_specific_block_endorsement_verification"`
	CodeConfigSHA256         map[string]string       `json:"code_config_sha256"`
	RuntimeVersions          map[string]string       `json:"runtime_versions"`
	Host                     ScientificEnvironment   `json:"scientific_environment"`
	FabricCPUIsolation       *FabricCPUIsolation     `json:"fabric_cpu_isolation,omitempty"`
	Status                   string                  `json:"status"`
	PreparedAt               string                  `json:"prepared_at"`
	StartedAt                string                  `json:"started_at,omitempty"`
	CompletedAt              string                  `json:"completed_at,omitempty"`
	FailedAt                 string                  `json:"failed_at,omitempty"`
	Failure                  string                  `json:"failure,omitempty"`
	RawFiles                 map[string]FileEvidence `json:"raw_files,omitempty"`
}

type MonotonicClock func() (int64, error)

func LinuxMonotonicNS() (int64, error) {
	var value unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &value); err != nil {
		return 0, fmt.Errorf("read CLOCK_MONOTONIC: %w", err)
	}
	return value.Nano(), nil
}

func FundingNetworkConfiguration(target Configuration) (Configuration, string, string, error) {
	switch target {
	case ConfigurationClassicalL2:
		return ConfigurationSETSReimpl, "ecdsa", FundingClassicalPeerAlgorithm, nil
	case ConfigurationLayerAwareL2:
		return ConfigurationL1Only, "ml-dsa-65", FundingLayerAwarePeerAlgorithm, nil
	default:
		return "", "", "", errors.New("funding target must be classical_l2 or layer_aware_l2")
	}
}

func FundingCommitment(target Configuration) (string, []byte, error) {
	if _, _, _, err := FundingNetworkConfiguration(target); err != nil {
		return "", nil, err
	}
	key := fmt.Sprintf("e7/funding/%s/%s", target, RunID)
	var value strings.Builder
	appendIdentityField(&value, "schema_version", FundingCommitmentSchema)
	appendIdentityField(&value, "target_config", string(target))
	appendIdentityField(&value, "run_id", RunID)
	appendIdentityField(&value, "settlement_measurement_commit", SettlementMeasurementCommit)
	appendIdentityField(&value, "trace_sha256", TraceSHA256)
	appendIdentityField(&value, "channel_id", "c1")
	appendIdentityField(&value, "prosumer", Prosumer)
	appendIdentityField(&value, "counterparty", Counterparty)
	appendIdentityField(&value, "prosumer_initial_kwh_credit", FundingInitialProsumerKWh)
	appendIdentityField(&value, "counterparty_initial_kwh_credit", FundingInitialCounterpartyKWh)
	appendIdentityField(&value, "interpretation", "measurement commitment only; not enforceable escrow or on-chain asset locking")
	return key, []byte(value.String()), nil
}

// MeasureFunding executes exactly one submission. Preparation, Gateway
// connection, readback, block retrieval, cryptographic audit, and teardown are
// intentionally outside the measured interval.
func MeasureFunding(ctx context.Context, target Configuration, client FundingFabricClient) (FundingResult, error) {
	return MeasureFundingWithClock(ctx, target, client, LinuxMonotonicNS)
}

// MeasureFundingWithClock exists to make the exact two timing observations
// testable. The first clock read is immediately before the one submission and
// the second is immediately after SubmitAndAwaitCommit returns. Readback,
// block retrieval, signature audit, and teardown remain outside this interval.
func MeasureFundingWithClock(ctx context.Context, target Configuration, client FundingFabricClient, clock MonotonicClock) (FundingResult, error) {
	key, payload, err := FundingCommitment(target)
	if err != nil {
		return FundingResult{}, err
	}
	start, err := clock()
	if err != nil {
		return FundingResult{}, err
	}
	completion, err := client.SubmitAndAwaitCommit(ctx, key, payload)
	end, clockErr := clock()
	result := FundingResult{TargetConfig: target, RunID: RunID, Key: key, StartMonotonicNS: start, EndMonotonicNS: end, FundingDurationNS: end - start, Completion: completion}
	digest := sha256.Sum256(payload)
	result.PayloadSHA256 = hex.EncodeToString(digest[:])
	if clockErr != nil {
		return result, clockErr
	}
	if err != nil {
		return result, err
	}
	if start < 0 || end <= start || result.FundingDurationNS <= 0 || !completion.Committed || !completion.Verified || completion.ValidationStatus != "VALID" || completion.ValidationCode != 0 || completion.TransactionID == "" {
		return result, errors.New("funding transaction did not return exact committed VALID status")
	}
	readback, err := client.ReadValue(ctx, key)
	if err != nil {
		return result, err
	}
	readbackDigest := sha256.Sum256(readback)
	result.ReadbackSHA256 = hex.EncodeToString(readbackDigest[:])
	result.ReadbackMatched = string(readback) == string(payload)
	if !result.ReadbackMatched {
		return result, errors.New("independent funding ledger readback mismatch")
	}
	block, err := client.FetchBlock(ctx, completion.BlockNumber)
	if err != nil {
		return result, err
	}
	_, _, expectedPeer, _ := FundingNetworkConfiguration(target)
	audit, err := AuditFundingTransaction(block, completion.TransactionID, key, payload, FundingClientAlgorithm, expectedPeer)
	if err != nil {
		return result, err
	}
	if audit.BlockNumber != completion.BlockNumber || audit.TransactionID != completion.TransactionID {
		return result, errors.New("funding block audit disagrees with commit status")
	}
	result.Audit = audit
	result.BlockBytes = append([]byte(nil), block...)
	return result, nil
}

func CreateFundingRunDirectory(root string, target Configuration, mode ...string) (string, error) {
	if _, _, _, err := FundingNetworkConfiguration(target); err != nil {
		return "", err
	}
	selectedMode := RunModeScientific
	if len(mode) > 1 {
		return "", errors.New("at most one funding mode may be supplied")
	}
	if len(mode) == 1 {
		selectedMode = mode[0]
	}
	parent := filepath.Join(root, string(target))
	if selectedMode == FundingModeIntegrationSmoke {
		parent = filepath.Join(root, "integration-smoke", string(target))
	} else if selectedMode != RunModeScientific {
		return "", errors.New("funding directory mode must be scientific or integration-smoke")
	}
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return "", err
	}
	directory := filepath.Join(parent, "run_1")
	if err := os.Mkdir(directory, 0o755); err != nil {
		return "", fmt.Errorf("refuse existing E7 funding directory %s: %w", directory, err)
	}
	return directory, nil
}

func WriteFundingManifest(path string, manifest FundingManifest) error {
	content, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	content = append(content, '\n')
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, content, 0o644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

func PersistFundingEvidence(directory string, result FundingResult, block []byte, logPath string) (map[string]FileEvidence, error) {
	files := map[string][]byte{}
	files["funding.csv"] = []byte("target_config,run_id,start_monotonic_ns,end_monotonic_ns,funding_duration_ns,transaction_id,block_number,validation_status,validation_code,committed,verified,readback_verified,payload_sha256\n" +
		fmt.Sprintf("%s,%s,%d,%d,%d,%s,%d,%s,%d,true,true,true,%s\n", result.TargetConfig, result.RunID, result.StartMonotonicNS, result.EndMonotonicNS, result.FundingDurationNS, result.Completion.TransactionID, result.Completion.BlockNumber, result.Completion.ValidationStatus, result.Completion.ValidationCode, result.PayloadSHA256))
	audit, err := json.MarshalIndent(result.Audit, "", "  ")
	if err != nil {
		return nil, err
	}
	files["transaction_audit.json"] = append(audit, '\n')
	readback, _ := json.MarshalIndent(map[string]any{"key": result.Key, "payload_sha256": result.PayloadSHA256, "readback_sha256": result.ReadbackSHA256, "matched": result.ReadbackMatched}, "", "  ")
	files["ledger_readback.json"] = append(readback, '\n')
	files["funding_block.pb"] = block
	output := map[string]FileEvidence{}
	for name, content := range files {
		path := filepath.Join(directory, name)
		file, openErr := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
		if openErr != nil {
			return nil, openErr
		}
		_, writeErr := file.Write(content)
		if syncErr := file.Sync(); writeErr == nil {
			writeErr = syncErr
		}
		if closeErr := file.Close(); writeErr == nil {
			writeErr = closeErr
		}
		if writeErr != nil {
			return nil, writeErr
		}
		evidence, hashErr := HashFile(path)
		if hashErr != nil {
			return nil, hashErr
		}
		output[name] = evidence
	}
	if logPath != "" {
		evidence, err := HashFile(logPath)
		if err != nil {
			return nil, err
		}
		output[filepath.Base(logPath)] = evidence
	}
	return output, nil
}

func HashFundingCode(projectRoot string) (map[string]string, error) {
	result := make(map[string]string, len(RequiredFundingCodeConfigPaths))
	for _, relative := range RequiredFundingCodeConfigPaths {
		evidence, err := HashFile(filepath.Join(projectRoot, relative))
		if err != nil {
			return nil, fmt.Errorf("hash funding code/config %s: %w", relative, err)
		}
		result[relative] = evidence.SHA256
	}
	return result, nil
}

func ValidateFundingDuration(text string) (int64, error) {
	value, err := strconv.ParseInt(text, 10, 64)
	if err != nil || value <= 0 {
		return 0, errors.New("funding_duration_ns must be a positive integer")
	}
	return value, nil
}
