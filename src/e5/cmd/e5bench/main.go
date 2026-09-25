package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	e5 "pqc-energy-trading/e5"
)

type options struct {
	nStates      uint64
	warmup       int
	iterations   int
	outputRoot   string
	artifactRoot string
	scientific   bool
}

func parseOptions() options {
	var result options
	flag.Uint64Var(&result.nStates, "n-states", 0, "number of stored watchtower states")
	flag.IntVar(&result.warmup, "warmup", e5.DefaultWarmupIterations, "discarded warm-up scans")
	flag.IntVar(&result.iterations, "iterations", e5.MinimumScientificIterations, "measured scans")
	flag.StringVar(&result.outputRoot, "output-root", "", "new directory for raw evidence")
	flag.StringVar(&result.artifactRoot, "artifact-root", "", "new directory for generated storage artifact")
	flag.BoolVar(&result.scientific, "scientific", false, "enforce scientific provenance and iteration policy")
	flag.Parse()
	return result
}

func containsStateCount(value uint64) bool {
	for _, candidate := range e5.ScientificStateCounts {
		if value == candidate {
			return true
		}
	}
	return false
}

func validateOptions(value options) error {
	if err := e5.ValidateRunParameters(value.nStates, value.warmup, value.iterations, value.scientific); err != nil {
		return err
	}
	if value.outputRoot == "" || value.artifactRoot == "" {
		return errors.New("output-root and artifact-root are required")
	}
	if filepath.Clean(value.outputRoot) == filepath.Clean(value.artifactRoot) {
		return errors.New("output-root and artifact-root must be distinct")
	}
	if value.scientific {
		if !containsStateCount(value.nStates) {
			return errors.New("scientific E5 n_states is outside the registered sweep")
		}
	}
	return nil
}

func repositoryRoot() (string, error) {
	output, err := exec.Command("git", "rev-parse", "--show-toplevel").Output()
	if err != nil {
		return "", err
	}
	return string(bytes.TrimSpace(output)), nil
}

func createNewDirectory(path string) error {
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("refusing existing directory %s", path)
	} else if !os.IsNotExist(err) {
		return err
	}
	return os.MkdirAll(path, 0o755)
}

func writeSamplesExclusive(path string, samples []e5.Sample) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	defer file.Close()
	if err := e5.WriteSamples(file, samples); err != nil {
		return err
	}
	return file.Sync()
}

func medianInt(values []int) (float64, error) {
	converted := make([]float64, len(values))
	for index, value := range values {
		converted[index] = float64(value)
	}
	return e5.Percentile(converted, 0.5)
}

func run(value options) error {
	if err := validateOptions(value); err != nil {
		return err
	}
	if _, err := os.Stat(value.outputRoot); err == nil {
		return fmt.Errorf("refusing existing output root %s", value.outputRoot)
	}
	if _, err := os.Stat(value.artifactRoot); err == nil {
		return fmt.Errorf("refusing existing artifact root %s", value.artifactRoot)
	}
	repoRoot, err := repositoryRoot()
	if err != nil {
		return err
	}
	environment, err := e5.CollectEnvironment(repoRoot, "/sys", "/proc")
	if err != nil {
		return err
	}
	if value.scientific {
		if err := e5.ValidateScientificEnvironment(environment); err != nil {
			return err
		}
	}
	blob, err := e5.GeneratePenaltyBlob()
	if err != nil {
		return err
	}
	if value.scientific && len(blob) != e5.FrozenPenaltyBlobBytes {
		return fmt.Errorf("real E2 penalty blob length = %d, want frozen %d", len(blob), e5.FrozenPenaltyBlobBytes)
	}
	if err := createNewDirectory(value.outputRoot); err != nil {
		return err
	}
	if err := createNewDirectory(value.artifactRoot); err != nil {
		return err
	}
	stem := fmt.Sprintf("layer_aware_n%d", value.nStates)
	artifactPath := filepath.Join(value.artifactRoot, "watchtower_"+stem+".bin")
	totalBytes, err := e5.WriteArtifact(artifactPath, value.nStates, blob)
	if err != nil {
		return err
	}
	observedBlobLengths, err := e5.ValidateArtifact(artifactPath, value.nStates, blob)
	if err != nil {
		return err
	}
	blobMedian, err := medianInt(observedBlobLengths)
	if err != nil {
		return err
	}
	scanner, err := e5.OpenMappedScanner(artifactPath, len(blob))
	if err != nil {
		return err
	}
	defer scanner.Close()
	_ = scanner.Prefault()
	target := value.nStates - 1
	samples := make([]e5.Sample, 0, value.warmup+value.iterations)
	for _, phase := range []struct {
		name  string
		count int
	}{{"warmup", value.warmup}, {"measured", value.iterations}} {
		for iteration := 0; iteration < phase.count; iteration++ {
			payload, visited, cpuMS, scanErr := e5.TimedScan(scanner, target)
			success := scanErr == nil && visited == value.nStates && bytes.Equal(payload, blob)
			errorText := ""
			if scanErr != nil {
				errorText = scanErr.Error()
			} else if visited != value.nStates {
				errorText = fmt.Sprintf("visited %d states, want %d", visited, value.nStates)
			} else if !bytes.Equal(payload, blob) {
				errorText = "matched payload differs from canonical E2 penalty blob"
			}
			samples = append(samples, e5.Sample{
				Iteration: iteration, Phase: phase.name, Scientific: value.scientific,
				NStates: value.nStates, BlobBytes: len(payload), TotalBytes: totalBytes,
				ScanCPUMS: cpuMS, Success: success, Error: errorText,
			})
		}
	}
	samplePath := filepath.Join(value.outputRoot, "samples_"+stem+".csv")
	if err := writeSamplesExclusive(samplePath, samples); err != nil {
		return err
	}
	failures := 0
	measuredTimings := make([]float64, 0, value.iterations)
	for _, sample := range samples {
		if !sample.Success {
			failures++
		}
		if sample.Phase == "measured" && sample.Success {
			measuredTimings = append(measuredTimings, sample.ScanCPUMS)
		}
	}
	summary, summaryErr := e5.SummarizeMeasuredScans(samples, value.iterations, value.scientific)
	if failures == 0 && summaryErr != nil {
		return summaryErr
	}
	sampleHash, sampleSize, err := e5.SHA256File(samplePath)
	if err != nil {
		return err
	}
	artifactHash, artifactSize, err := e5.SHA256File(artifactPath)
	if err != nil {
		return err
	}
	if artifactSize != totalBytes {
		return fmt.Errorf("hashed artifact size = %d, measured os.Stat size = %d", artifactSize, totalBytes)
	}
	blobDigest := sha256.Sum256(blob)
	manifest := map[string]any{
		"schema": e5.ManifestSchema, "scientific": value.scientific,
		"condition": map[string]any{
			"config": e5.ConfigLayerAware, "n_states": value.nStates,
			"warmup_iterations": value.warmup, "measured_iterations": value.iterations,
			"worst_case_target_state": target,
		},
		"blob": map[string]any{
			"source": "real E2 layer_aware penalty transition",
			"bytes":  len(blob), "observed_blob_bytes_median": blobMedian,
			"sha256": hex.EncodeToString(blobDigest[:]),
		},
		"storage_artifact": map[string]any{
			"filename": filepath.Base(artifactPath), "bytes": totalBytes,
			"sha256": artifactHash, "record_layout": "uint64-big-endian state_id || unchanged E2 penalty blob",
			"validation": map[string]any{
				"physically_generated":                 true,
				"fsynced_and_closed":                   true,
				"total_bytes_measured_with_os_stat":    true,
				"sha256_computed":                      true,
				"exact_byte_size_recorded":             true,
				"all_sequential_ids_validated":         true,
				"all_payloads_match_canonical_e2_blob": true,
				"record_count_validated":               true,
				"no_trailing_bytes":                    true,
			},
		},
		"samples": map[string]any{
			"filename": filepath.Base(samplePath), "bytes": sampleSize, "sha256": sampleHash,
			"row_count": len(samples), "warmup_count": value.warmup,
			"measured_count": value.iterations, "failure_count": failures,
		},
		"scan_methodology": map[string]any{
			"definition":                    "deterministic sequential worst-case identifier scan to final state",
			"nonmatching_payload_treatment": "skip fixed payload by memory offset without parsing",
			"cache_policy":                  "mmap prefault plus discarded warm-up scans",
			"clock":                         "Linux CLOCK_PROCESS_CPUTIME_ID user plus system process CPU",
			"timed_region":                  "identifier scan and matching payload copy only",
			"percentile":                    "linear interpolation rank p*(n-1)",
			"measured_median_ms":            summary.MedianMS, "measured_p95_ms": summary.P95MS, "measured_p99_ms": summary.P99MS,
		},
		"environment": environment,
		"created_utc": time.Now().UTC().Format(time.RFC3339Nano),
	}
	manifestPath := filepath.Join(value.outputRoot, "manifest_"+stem+".json")
	if err := e5.WriteJSONExclusive(manifestPath, manifest); err != nil {
		return err
	}
	if value.scientific && (failures != 0 || len(measuredTimings) != value.iterations) {
		return errors.New("scientific E5 condition retained failures or incomplete measurements")
	}
	fmt.Printf("manifest=%s\nartifact=%s\n", manifestPath, artifactPath)
	return nil
}

func main() {
	if err := run(parseOptions()); err != nil {
		fmt.Fprintln(os.Stderr, "e5bench:", err)
		os.Exit(1)
	}
}
