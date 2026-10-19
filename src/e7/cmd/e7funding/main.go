package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	e7 "pqc-energy-trading/e7"
)

func commandVersion(name string, arguments ...string) (string, error) {
	output, err := exec.Command(name, arguments...).CombinedOutput()
	if err != nil {
		return "", err
	}
	value := strings.TrimSpace(string(output))
	if value == "" {
		return "", errors.New("empty version output")
	}
	return value, nil
}

func fail(path string, manifest *e7.FundingManifest, cause error) {
	manifest.Status, manifest.FailedAt, manifest.Failure = "failed", e7.NowUTC(), cause.Error()
	_ = e7.WriteFundingManifest(path, *manifest)
}

func run() error {
	targetText := flag.String("target-config", "", "classical_l2 or layer_aware_l2")
	runID := flag.String("run-id", "", "must be 1")
	trace := flag.String("trace", "", "frozen E7 trace")
	evidenceRoot := flag.String("evidence-root", "", "new external funding evidence root")
	mode := flag.String("mode", "", "dry-run, integration-smoke, or scientific")
	projectRoot := flag.String("project-root", ".", "repository root")
	flag.Parse()
	target := e7.Configuration(*targetText)
	networkConfig, networkMode, peerAlgorithm, err := e7.FundingNetworkConfiguration(target)
	if err != nil || *runID != e7.RunID || *trace == "" || *evidenceRoot == "" {
		return errors.New("target-config, run-id=1, trace, and evidence-root are required")
	}
	traceEvidence, err := e7.HashFile(*trace)
	if err != nil || traceEvidence.SHA256 != e7.TraceSHA256 {
		return errors.New("frozen E7 trace hash mismatch")
	}
	traceProvenanceEvidence, err := e7.HashFile(filepath.Join(filepath.Dir(*trace), "provenance.json"))
	if err != nil {
		return fmt.Errorf("hash frozen E7 trace provenance: %w", err)
	}
	key, payload, err := e7.FundingCommitment(target)
	if err != nil {
		return err
	}
	if *mode == e7.RunModeDryRun {
		fmt.Printf("funding dry-run validated target=%s fabric_mode=%s key=%s payload_bytes=%d; no directory or transaction created\n", target, networkMode, key, len(payload))
		return nil
	}
	if *mode != e7.RunModeScientific && *mode != e7.FundingModeIntegrationSmoke {
		return errors.New("mode must be explicitly dry-run, integration-smoke, or scientific")
	}

	// All checks which do not require a run directory happen first.
	environment := e7.ScientificEnvironment{}
	scientific := *mode == e7.RunModeScientific
	if scientific {
		environment, err = e7.CollectScientificEnvironment("/sys", "/proc/cpuinfo")
		if err != nil {
			return fmt.Errorf("scientific host preflight: %w", err)
		}
	}
	commit, clean, err := e7.GitProvenance(*projectRoot)
	if err != nil {
		return fmt.Errorf("collect Git provenance: %w", err)
	}
	if scientific && !clean {
		return errors.New("scientific E7 funding requires a clean tracked Git tree")
	}
	_, fabricVersion, err := e7.CollectSystemFabricPeerVersion()
	if err != nil {
		return err
	}
	cryptoVersions, err := e7.CollectSystemL2CryptoVersions(filepath.Join(*projectRoot, "src/e7"))
	if err != nil {
		return err
	}
	pythonVersion, err := commandVersion("python3", "--version")
	if err != nil {
		return err
	}
	codeHashes, err := e7.HashFundingCode(*projectRoot)
	if err != nil {
		return err
	}
	gatewayConfig, err := e7.DefaultFabricGatewayConfig(*projectRoot, networkConfig)
	if err != nil {
		return err
	}
	client, err := e7.NewRealFabricSubmitter(gatewayConfig)
	if err != nil {
		return err
	}
	runtimeVersions := map[string]string{"go": runtime.Version(), "python": pythonVersion, "fabric": fabricVersion, "fabric_gateway_go": e7.FabricGatewayVersion, "liboqs": cryptoVersions["liboqs"], "liboqs_go": cryptoVersions["liboqs_go"]}
	payloadEvidence := fmt.Sprintf("%x", sha256Bytes(payload))

	directory, err := e7.CreateFundingRunDirectory(*evidenceRoot, target, *mode)
	if err != nil {
		return err
	}
	schema, experiment := e7.FundingManifestSchema, "E7 supplemental channel funding"
	if !scientific {
		schema, experiment = e7.FundingSmokeManifestSchema, "E7 supplemental channel funding integration smoke"
	}
	manifest := e7.FundingManifest{
		Schema: schema, Experiment: experiment, Mode: *mode, Scientific: scientific, TargetConfig: target,
		FabricNetworkMode: networkMode, RunID: e7.RunID, SettlementCommit: e7.SettlementMeasurementCommit,
		GitCommit: commit, GitTrackedClean: clean, TraceSHA256: e7.TraceSHA256,
		TraceProvenanceSHA256: traceProvenanceEvidence.SHA256,
		WarmupRowsExcluded:    e7.WarmupRowCount, MeasuredDenominator: e7.FundingMeasuredDenominator,
		Channel: e7.FabricChannel, Chaincode: e7.FabricChaincode, Function: "Set", CommitmentKey: key,
		CommitmentPayloadSHA256:  payloadEvidence,
		CommitmentInterpretation: "measurement commitment only; not enforceable escrow or on-chain asset locking",
		GatewayClientAlgorithm:   e7.FundingClientAlgorithm, PeerEndorsementAlgorithm: peerAlgorithm,
		TimerDefinition: e7.FundingTimerDefinition, CodeConfigSHA256: codeHashes, RuntimeVersions: runtimeVersions,
		Host: environment, Status: "prepared", PreparedAt: e7.NowUTC(), RawFiles: map[string]e7.FileEvidence{},
	}
	manifestPath := filepath.Join(directory, "manifest.json")
	if err := e7.WriteFundingManifest(manifestPath, manifest); err != nil {
		return err
	}
	logPath := filepath.Join(directory, "run.log")
	logFile, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	log := io.MultiWriter(os.Stdout, logFile)
	ctx, stopSignals := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopSignals()
	lifecycle := e7.CommandFabricLifecycle{Script: filepath.Join(*projectRoot, "env/e7/fabric_network.sh")}
	running := false
	defer func() {
		_ = client.Close()
		if running {
			cleanupContext, cancelCleanup := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancelCleanup()
			_ = lifecycle.Stop(cleanupContext, networkConfig, log)
		}
		_ = logFile.Close()
	}()
	running = true
	isolation, err := lifecycle.Start(ctx, networkConfig, log)
	if err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	manifest.FabricCPUIsolation = &isolation
	if err = client.PrepareRun(ctx, networkConfig); err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	manifest.Status, manifest.StartedAt = "running", e7.NowUTC()
	if err = e7.WriteFundingManifest(manifestPath, manifest); err != nil {
		return err
	}
	result, err := e7.MeasureFunding(ctx, target, client)
	if err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	if err = client.Close(); err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	if err = lifecycle.Stop(ctx, networkConfig, log); err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	running = false
	if err = logFile.Sync(); err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	if err = logFile.Close(); err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	files, err := e7.PersistFundingEvidence(directory, result, result.BlockBytes, logPath)
	if err != nil {
		fail(manifestPath, &manifest, err)
		return err
	}
	completion := result.Completion
	manifest.StartMonotonicNS, manifest.EndMonotonicNS = result.StartMonotonicNS, result.EndMonotonicNS
	manifest.FundingDurationNS, manifest.Completion = result.FundingDurationNS, &completion
	manifest.ReadbackVerified, manifest.TransactionAuditVerified = result.ReadbackMatched, result.Audit.AllSignaturesValid
	manifest.RawFiles, manifest.Status, manifest.CompletedAt = files, "complete", e7.NowUTC()
	if err = e7.WriteFundingManifest(manifestPath, manifest); err != nil {
		return err
	}
	fmt.Fprintf(os.Stdout, "complete mode=%s target=%s transaction_id=%s funding_duration_ns=%d\n", *mode, target, completion.TransactionID, result.FundingDurationNS)
	return nil
}

func sha256Bytes(value []byte) [32]byte { return sha256.Sum256(value) }

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}
