package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	e7 "pqc-energy-trading/e7"
)

func commandVersion(name string, arguments ...string) string {
	output, err := exec.Command(name, arguments...).CombinedOutput()
	if err != nil {
		return "unavailable"
	}
	return strings.TrimSpace(string(output))
}

func failManifest(path string, manifest *e7.RunManifest, cause error) {
	manifest.Status = "failed"
	manifest.FailedAt = e7.NowUTC()
	manifest.Failure = cause.Error()
	_ = e7.WriteManifest(path, *manifest)
}

func run() error {
	configText := flag.String("config", "", "l1_only, sets_reimpl, classical_l2, or layer_aware_l2")
	runID := flag.String("run-id", "", "must be 1")
	trace := flag.String("trace", "", "frozen E7 trace CSV")
	evidenceRoot := flag.String("evidence-root", "", "new external evidence root")
	mode := flag.String("mode", "", "dry-run or scientific (never inferred)")
	projectRoot := flag.String("project-root", ".", "repository root")
	flag.Parse()
	config := e7.Configuration(*configText)
	if !config.Valid() || *runID != e7.RunID || *trace == "" || *evidenceRoot == "" {
		return errors.New("config, run-id=1, trace, and evidence-root are required")
	}
	samples, err := e7.LoadFrozenSamples(*trace)
	if err != nil {
		return err
	}
	if *mode == e7.RunModeDryRun {
		if err := e7.ValidateRunMode(*mode, e7.ScientificEnvironment{}); err != nil {
			return err
		}
		fmt.Printf("dry-run validated config=%s run_id=1 samples=%d; no evidence or settlement executed\n", config, len(samples))
		return nil
	}
	if *mode != e7.RunModeScientific {
		return errors.New("mode must be explicitly dry-run or scientific")
	}
	var environment e7.ScientificEnvironment
	var commit string
	var semantics map[string]any
	var traceProvenance string
	var traceProvenanceEvidence e7.FileEvidence
	var resetMethod string
	var runtimeVersions map[string]string
	var codeConfigSHA256 map[string]string
	var preparedSubmitter *e7.RealFabricSubmitter
	runDirectory, err := e7.CreateRunDirectoryAfterPreflight(*evidenceRoot, config, *runID, func() error {
		var preflightErr error
		environment, preflightErr = e7.CollectScientificEnvironment("/sys", "/proc/cpuinfo")
		if preflightErr != nil {
			return fmt.Errorf("scientific host preflight: %w", preflightErr)
		}
		if preflightErr = e7.ValidateRunMode(*mode, environment); preflightErr != nil {
			return preflightErr
		}
		var clean bool
		commit, clean, preflightErr = e7.GitProvenance(*projectRoot)
		if preflightErr != nil || !clean {
			return errors.New("scientific E7 requires a clean tracked Git tree")
		}
		semantics, preflightErr = e7.ConfigSemantics(config)
		if preflightErr != nil {
			return preflightErr
		}
		traceProvenance = filepath.Join(filepath.Dir(*trace), "provenance.json")
		traceProvenanceEvidence, preflightErr = e7.HashFile(traceProvenance)
		if preflightErr != nil {
			return preflightErr
		}
		resetMethod = e7.L2ResetMethod
		runtimeVersions = map[string]string{
			"go": runtime.Version(), "python": commandVersion("python3", "--version"),
			"fabric_gateway_go": e7.FabricGatewayVersion,
		}
		if runtimeVersions["python"] == "unavailable" {
			return errors.New("scientific E7 requires observable Python version provenance")
		}
		if config == e7.ConfigurationClassicalL2 || config == e7.ConfigurationLayerAwareL2 {
			versions, versionErr := e7.CollectSystemL2CryptoVersions(filepath.Join(*projectRoot, "src/e7"))
			if versionErr != nil {
				return versionErr
			}
			for name, value := range versions {
				runtimeVersions[name] = value
			}
		} else {
			resetMethod = e7.FabricResetMethod
			_, fabricVersion, versionErr := e7.CollectSystemFabricPeerVersion()
			if versionErr != nil {
				return fmt.Errorf("scientific E7 requires Fabric %s version provenance: %w", e7.FabricVersion, versionErr)
			}
			runtimeVersions["fabric"] = fabricVersion
			runtimeVersions["liboqs"] = "not applicable to on-chain E7 config"
			runtimeVersions["liboqs_go"] = "not applicable to on-chain E7 config"
			gatewayConfig, configErr := e7.DefaultFabricGatewayConfig(*projectRoot, config)
			if configErr != nil {
				return configErr
			}
			preparedSubmitter, preflightErr = e7.NewRealFabricSubmitter(gatewayConfig)
			if preflightErr != nil {
				return preflightErr
			}
		}
		codeConfigSHA256 = map[string]string{}
		for _, relative := range e7.RequiredCodeConfigPaths {
			evidence, hashErr := e7.HashFile(filepath.Join(*projectRoot, relative))
			if hashErr != nil {
				return fmt.Errorf("hash E7 code/config %s: %w", relative, hashErr)
			}
			codeConfigSHA256[relative] = evidence.SHA256
		}
		return nil
	})
	if err != nil {
		return err
	}
	manifest := e7.RunManifest{
		Schema: e7.ManifestSchema, Experiment: "E7", Config: config, RunID: *runID,
		GitCommit: commit, GitTrackedClean: true, TracePath: *trace, TraceSHA256: e7.TraceSHA256,
		TraceProvenancePath: traceProvenance, TraceProvenanceSHA256: traceProvenanceEvidence.SHA256,
		WarmupRows: e7.WarmupRowCount, MeasuredRows: e7.MeasuredRowCount,
		TimerDefinition: e7.TimerDefinition, ConfigSemantics: semantics, ProcessResetMethod: resetMethod,
		RuntimeVersions: runtimeVersions, CodeConfigSHA256: codeConfigSHA256,
		Host: environment, Status: "prepared", PreparedAt: e7.NowUTC(), RawFiles: map[string]e7.FileEvidence{},
	}
	manifestPath := filepath.Join(runDirectory, "manifest.json")
	if err := e7.WriteManifest(manifestPath, manifest); err != nil {
		return err
	}
	logPath := filepath.Join(runDirectory, "run.log")
	logFile, err := os.OpenFile(logPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		failManifest(manifestPath, &manifest, err)
		return err
	}
	defer logFile.Close()
	log := io.MultiWriter(os.Stdout, logFile)
	fmt.Fprintf(log, "E7 config=%s run_id=%s mode=scientific prepared=%s\n", config, *runID, e7.NowUTC())

	ctx := context.Background()
	var executor e7.SettlementExecutor
	var lifecycle e7.FabricLifecycle
	lifecycleRunning := false
	executorOpen := false
	defer func() {
		if executorOpen && executor != nil {
			_ = executor.Close()
		}
		if lifecycleRunning && lifecycle != nil {
			_ = lifecycle.Stop(ctx, config, log)
		}
	}()
	if config == e7.ConfigurationL1Only || config == e7.ConfigurationSETSReimpl {
		lifecycle = e7.CommandFabricLifecycle{Script: filepath.Join(*projectRoot, "env/e7/fabric_network.sh")}
		// From this point a failed start may still have created containers. Keep
		// cleanup armed so setup or post-start CPU verification failures cannot
		// leave an E7 Fabric network behind.
		lifecycleRunning = true
		fabricCPUIsolation, startErr := lifecycle.Start(ctx, config, log)
		if startErr != nil {
			err = startErr
			failManifest(manifestPath, &manifest, err)
			return err
		}
		manifest.FabricCPUIsolation = &fabricCPUIsolation
		state, stateErr := e7.NewTraceDerivedLogicalState()
		if stateErr != nil {
			failManifest(manifestPath, &manifest, stateErr)
			return stateErr
		}
		executor, err = e7.NewOnChainExecutor(config, state, preparedSubmitter)
		if err != nil {
			failManifest(manifestPath, &manifest, err)
			return err
		}
	} else {
		clientConnection, receiverConnection, err := e7.NewLoopbackTCPPair(ctx)
		if err != nil {
			failManifest(manifestPath, &manifest, err)
			return err
		}
		state, stateErr := e7.NewTraceDerivedLogicalState()
		if stateErr != nil {
			failManifest(manifestPath, &manifest, stateErr)
			return stateErr
		}
		executor, err = e7.NewL2Executor(config, clientConnection, receiverConnection, state)
		if err != nil {
			failManifest(manifestPath, &manifest, err)
			return err
		}
	}
	executorOpen = true
	manifest.Status, manifest.StartedAt = "running", e7.NowUTC()
	if err := e7.WriteManifest(manifestPath, manifest); err != nil {
		failManifest(manifestPath, &manifest, err)
		return err
	}
	records, runErr := e7.NewWorkloadRunner().Run(ctx, executor, samples, true)
	samplesEvidence, persistErr := e7.PersistSamples(runDirectory, records)
	if persistErr != nil {
		failManifest(manifestPath, &manifest, persistErr)
		return persistErr
	}
	manifest.RawFiles["samples.csv"] = samplesEvidence
	if config == e7.ConfigurationL1Only || config == e7.ConfigurationSETSReimpl {
		audit, auditErr := e7.PersistFabricAudit(runDirectory, records)
		if auditErr != nil {
			failManifest(manifestPath, &manifest, auditErr)
			return auditErr
		}
		for name, evidence := range audit {
			manifest.RawFiles[name] = evidence
		}
	}
	if syncErr := logFile.Sync(); syncErr != nil && runErr == nil {
		runErr = syncErr
	}
	if runErr != nil {
		failManifest(manifestPath, &manifest, runErr)
		return runErr
	}
	if closeErr := executor.Close(); closeErr != nil {
		failManifest(manifestPath, &manifest, closeErr)
		return closeErr
	}
	executorOpen = false
	if lifecycleRunning {
		if stopErr := lifecycle.Stop(ctx, config, log); stopErr != nil {
			failManifest(manifestPath, &manifest, stopErr)
			return stopErr
		}
		lifecycleRunning = false
	}
	if syncErr := logFile.Sync(); syncErr != nil {
		failManifest(manifestPath, &manifest, syncErr)
		return syncErr
	}
	logEvidence, hashErr := e7.HashFile(logPath)
	if hashErr != nil {
		failManifest(manifestPath, &manifest, hashErr)
		return hashErr
	}
	manifest.RawFiles["run.log"] = logEvidence
	manifest.Status, manifest.CompletedAt = "complete", e7.NowUTC()
	if err := e7.ValidateCompleteManifest(manifest); err != nil {
		failManifest(manifestPath, &manifest, err)
		return err
	}
	return e7.WriteManifest(manifestPath, manifest)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
