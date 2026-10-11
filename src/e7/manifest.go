package e7

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	ManifestSchema    = "pqc-energy-trading.e7-run-manifest.v1"
	TimerDefinition   = "Go monotonic time immediately before executor.Settle through successful completion return"
	RunModeDryRun     = "dry-run"
	RunModeScientific = "scientific"
)

var ExpectedScientificCPUs = []int{2, 4, 6, 8, 10, 12, 14}

var RequiredCodeConfigPaths = []string{
	"src/e7/cmd/e7bench/main.go", "src/e7/runner.go", "src/e7/manifest.go",
	"src/e7/evidence.go", "src/e7/model.go", "src/e7/state.go", "src/e7/onchain.go",
	"src/e7/l2.go", "src/e7/fabric.go", "src/e7/runio.go", "src/e7/lifecycle.go",
	"src/e7/fabriccpu.go", "src/e7/versions.go", "src/e7/go.mod", "src/e7/go.sum", "src/e7/config.py",
	"src/e7/finalize_e7.py", "env/e7/fabric_network.sh",
	"env/e7/docker-compose.fabric-cpu.yaml", "env/e7/docker-shim/docker",
	"env/fabric/e1/setup_fabric_e1.sh", "env/fabric/e1/docker-compose.yaml",
	"env/e7/trace/opsd_household_15min_e7.csv", "env/e7/trace/provenance.json",
}

type ScientificEnvironment struct {
	CPUModel          string   `json:"cpu_model"`
	LogicalCPUCount   int      `json:"logical_cpu_count"`
	ScalingDriver     []string `json:"scaling_driver"`
	Governor          []string `json:"governor"`
	EPP               []string `json:"energy_performance_preference"`
	ScalingMinFreqKHz []int    `json:"scaling_min_freq_khz"`
	ScalingMaxFreqKHz []int    `json:"scaling_max_freq_khz"`
	BoostEnabled      *bool    `json:"boost_enabled"`
	ACOnline          *bool    `json:"ac_online"`
	Affinity          []int    `json:"process_affinity"`
	ExpectedAffinity  []int    `json:"expected_affinity"`
	Passed            bool     `json:"passed"`
}

type RunManifest struct {
	Schema                string                  `json:"schema"`
	Experiment            string                  `json:"experiment"`
	Config                Configuration           `json:"config"`
	RunID                 string                  `json:"run_id"`
	GitCommit             string                  `json:"git_commit"`
	GitTrackedClean       bool                    `json:"git_tracked_clean"`
	TracePath             string                  `json:"trace_path"`
	TraceSHA256           string                  `json:"trace_sha256"`
	TraceProvenancePath   string                  `json:"trace_provenance_path"`
	TraceProvenanceSHA256 string                  `json:"trace_provenance_sha256"`
	WarmupRows            int                     `json:"warmup_rows"`
	MeasuredRows          int                     `json:"measured_rows"`
	TimerDefinition       string                  `json:"timer_definition"`
	ConfigSemantics       map[string]any          `json:"config_semantics"`
	ProcessResetMethod    string                  `json:"process_network_reset_method"`
	CodeConfigSHA256      map[string]string       `json:"code_config_sha256"`
	RuntimeVersions       map[string]string       `json:"runtime_versions"`
	Host                  ScientificEnvironment   `json:"scientific_environment"`
	Status                string                  `json:"status"`
	PreparedAt            string                  `json:"prepared_at"`
	StartedAt             string                  `json:"started_at,omitempty"`
	CompletedAt           string                  `json:"completed_at,omitempty"`
	FailedAt              string                  `json:"failed_at,omitempty"`
	Failure               string                  `json:"failure,omitempty"`
	RawFiles              map[string]FileEvidence `json:"raw_files,omitempty"`
	FabricCPUIsolation    *FabricCPUIsolation     `json:"fabric_cpu_isolation,omitempty"`
}

type FileEvidence struct {
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

func ConfigSemantics(config Configuration) (map[string]any, error) {
	switch config {
	case ConfigurationL1Only:
		return map[string]any{"path": "fabric_l1", "fabric_identity_mode": "ml-dsa-65", "channel": FabricChannel, "chaincode": FabricChaincode, "function": "Set", "completion": "committed and VALID Fabric status"}, nil
	case ConfigurationSETSReimpl:
		return map[string]any{"path": "fabric_l1", "fabric_identity_mode": "ecdsa", "application_ecdsa": SETSECDSAAlgorithm, "sets_provenance": "our reimplementation; not the original authors' code", "channel": FabricChannel, "chaincode": FabricChaincode, "function": "Set", "completion": "committed and VALID Fabric status"}, nil
	case ConfigurationClassicalL2:
		return map[string]any{"path": "direct_l2", "e2_configuration": "classical", "crypto": "Ed25519 representative compact classical semantics; not MuSig2", "transport": "persistent direct TCP; no Mininet or artificial delay"}, nil
	case ConfigurationLayerAwareL2:
		return map[string]any{"path": "direct_l2", "e2_configuration": "layer_aware", "crypto": "Falcon-padded-512 for HTLC add/settle", "setup": "channel setup, funding, keys and TCP readiness before row 0", "transport": "persistent direct TCP; no Mininet or artificial delay"}, nil
	default:
		return nil, errors.New("unknown E7 configuration")
	}
}

func ValidateScientificEnvironment(environment ScientificEnvironment) error {
	if environment.CPUModel == "" || environment.LogicalCPUCount <= 0 {
		return errors.New("scientific CPU provenance unavailable")
	}
	if !equalInts(environment.Affinity, ExpectedScientificCPUs) {
		return errors.New("scientific affinity must be exactly 2,4,6,8,10,12,14")
	}
	if len(environment.ScalingDriver) == 0 || uniqueStrings(environment.ScalingDriver) != "amd-pstate-epp" {
		return errors.New("scientific scaling driver must be amd-pstate-epp")
	}
	if len(environment.Governor) == 0 || uniqueStrings(environment.Governor) != "performance" {
		return errors.New("scientific governor must be performance")
	}
	if len(environment.EPP) == 0 || uniqueStrings(environment.EPP) != "performance" {
		return errors.New("scientific EPP must be performance")
	}
	if !allInts(environment.ScalingMinFreqKHz, 3200000) || !allInts(environment.ScalingMaxFreqKHz, 3200000) {
		return errors.New("scientific scaling min/max must both be 3200000 kHz")
	}
	if environment.BoostEnabled == nil || *environment.BoostEnabled {
		return errors.New("scientific boost must be readable and disabled")
	}
	if environment.ACOnline == nil || !*environment.ACOnline {
		return errors.New("scientific AC state must be readable and online")
	}
	return nil
}

func ValidateRunMode(mode string, environment ScientificEnvironment) error {
	switch mode {
	case RunModeDryRun:
		return nil
	case RunModeScientific:
		return ValidateScientificEnvironment(environment)
	default:
		return errors.New("E7 mode must be explicitly dry-run or scientific")
	}
}

func ValidateCompleteManifest(manifest RunManifest) error {
	if manifest.Schema != ManifestSchema || manifest.Experiment != "E7" || !manifest.Config.Valid() || manifest.RunID != RunID {
		return errors.New("invalid E7 manifest identity")
	}
	if manifest.Status != "complete" || manifest.GitCommit == "" || !manifest.GitTrackedClean {
		return errors.New("E7 manifest is incomplete or has dirty provenance")
	}
	if len(manifest.CodeConfigSHA256) != len(RequiredCodeConfigPaths) || len(manifest.RuntimeVersions) == 0 || manifest.ProcessResetMethod == "" {
		return errors.New("E7 manifest code/runtime/reset provenance is missing")
	}
	for _, path := range RequiredCodeConfigPaths {
		digest, ok := manifest.CodeConfigSHA256[path]
		if !ok {
			return errors.New("E7 manifest code/config provenance key set is invalid")
		}
		if !validSHA256Hex(digest) {
			return errors.New("E7 manifest code/config SHA-256 is invalid")
		}
	}
	if manifest.RuntimeVersions["go"] == "" || manifest.RuntimeVersions["python"] == "" || manifest.RuntimeVersions["fabric_gateway_go"] != FabricGatewayVersion {
		return errors.New("E7 manifest runtime version provenance is invalid")
	}
	if manifest.Config == ConfigurationClassicalL2 || manifest.Config == ConfigurationLayerAwareL2 {
		if manifest.RuntimeVersions["liboqs"] != ExpectedLiboqsVersion || manifest.RuntimeVersions["liboqs_go"] != ExpectedLiboqsGoVersion {
			return errors.New("E7 manifest L2 crypto version provenance is invalid")
		}
		if manifest.FabricCPUIsolation != nil {
			return errors.New("E7 L2 manifest must not contain Fabric CPU isolation provenance")
		}
	} else if manifest.Config == ConfigurationL1Only || manifest.Config == ConfigurationSETSReimpl {
		if manifest.FabricCPUIsolation == nil || ValidateFabricCPUIsolationRecord(*manifest.FabricCPUIsolation) != nil {
			return errors.New("E7 on-chain manifest Fabric CPU isolation provenance is invalid")
		}
	}
	if manifest.TraceSHA256 != TraceSHA256 || !validSHA256Hex(manifest.TraceProvenanceSHA256) || manifest.WarmupRows != WarmupRowCount || manifest.MeasuredRows != MeasuredRowCount || manifest.TimerDefinition != TimerDefinition {
		return errors.New("E7 manifest trace/population/timer mismatch")
	}
	if err := ValidateScientificEnvironment(manifest.Host); err != nil || !manifest.Host.Passed {
		return errors.New("E7 manifest scientific controls did not pass")
	}
	want, err := ConfigSemantics(manifest.Config)
	if err != nil {
		return err
	}
	wantJSON, _ := json.Marshal(want)
	gotJSON, _ := json.Marshal(manifest.ConfigSemantics)
	if string(wantJSON) != string(gotJSON) {
		return errors.New("E7 manifest configuration semantics mismatch")
	}
	required := []string{"samples.csv", "run.log"}
	if manifest.Config == ConfigurationL1Only || manifest.Config == ConfigurationSETSReimpl {
		required = append(required, "transactions.csv", "ledger_heights.csv")
	}
	for _, name := range required {
		evidence, ok := manifest.RawFiles[name]
		if !ok || evidence.Bytes <= 0 || !validSHA256Hex(evidence.SHA256) {
			return fmt.Errorf("E7 manifest %s evidence is missing", name)
		}
	}
	return nil
}

func CollectScientificEnvironment(sysRoot, procCPUInfo string) (ScientificEnvironment, error) {
	environment := ScientificEnvironment{LogicalCPUCount: runtime.NumCPU(), ExpectedAffinity: append([]int(nil), ExpectedScientificCPUs...)}
	content, err := os.ReadFile(procCPUInfo)
	if err != nil {
		return environment, err
	}
	for _, line := range strings.Split(string(content), "\n") {
		if strings.HasPrefix(strings.ToLower(line), "model name") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				environment.CPUModel = strings.TrimSpace(parts[1])
				break
			}
		}
	}
	policies, _ := filepath.Glob(filepath.Join(sysRoot, "devices/system/cpu/cpufreq/policy*"))
	sort.Strings(policies)
	for _, policy := range policies {
		driver, e1 := readTrim(filepath.Join(policy, "scaling_driver"))
		governor, e2 := readTrim(filepath.Join(policy, "scaling_governor"))
		epp, e3 := readTrim(filepath.Join(policy, "energy_performance_preference"))
		minText, e4 := readTrim(filepath.Join(policy, "scaling_min_freq"))
		maxText, e5 := readTrim(filepath.Join(policy, "scaling_max_freq"))
		if e1 != nil || e2 != nil || e3 != nil || e4 != nil || e5 != nil {
			return environment, errors.New("incomplete CPU policy provenance")
		}
		minimum, err := strconv.Atoi(minText)
		if err != nil {
			return environment, err
		}
		maximum, err := strconv.Atoi(maxText)
		if err != nil {
			return environment, err
		}
		environment.ScalingDriver = append(environment.ScalingDriver, driver)
		environment.Governor = append(environment.Governor, governor)
		environment.EPP = append(environment.EPP, epp)
		environment.ScalingMinFreqKHz = append(environment.ScalingMinFreqKHz, minimum)
		environment.ScalingMaxFreqKHz = append(environment.ScalingMaxFreqKHz, maximum)
	}
	boostText, err := firstReadable(filepath.Join(sysRoot, "devices/system/cpu/cpufreq/boost"), filepath.Join(sysRoot, "devices/system/cpu/amd_pstate/cpb_boost"))
	if err != nil {
		return environment, err
	}
	boost := boostText == "1"
	if boostText != "0" && boostText != "1" {
		return environment, errors.New("invalid boost state")
	}
	environment.BoostEnabled = &boost
	supplies, _ := filepath.Glob(filepath.Join(sysRoot, "class/power_supply/*"))
	foundAC := false
	online := false
	for _, s := range supplies {
		typ, _ := readTrim(filepath.Join(s, "type"))
		if typ == "Mains" {
			foundAC = true
			value, _ := readTrim(filepath.Join(s, "online"))
			if value == "1" {
				online = true
			}
		}
	}
	if foundAC {
		environment.ACOnline = &online
	} else {
		return environment, errors.New("AC state unavailable")
	}
	var set unix.CPUSet
	if err := unix.SchedGetaffinity(0, &set); err != nil {
		return environment, err
	}
	for cpu := 0; cpu < 1024; cpu++ {
		if set.IsSet(cpu) {
			environment.Affinity = append(environment.Affinity, cpu)
		}
	}
	if err := ValidateScientificEnvironment(environment); err != nil {
		return environment, err
	}
	environment.Passed = true
	return environment, nil
}

func CreateRunDirectory(root string, config Configuration, runID string) (string, error) {
	if !config.Valid() || runID != "1" {
		return "", errors.New("E7 requires a valid config and run_id=1")
	}
	parent := filepath.Join(root, string(config))
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(parent, "run_1")
	if err := os.Mkdir(path, 0o755); err != nil {
		return "", fmt.Errorf("refuse existing E7 run directory %s: %w", path, err)
	}
	return path, nil
}

func CreateRunDirectoryAfterPreflight(root string, config Configuration, runID string, preflight func() error) (string, error) {
	if preflight == nil {
		return "", errors.New("E7 scientific preflight is required")
	}
	if err := preflight(); err != nil {
		return "", err
	}
	return CreateRunDirectory(root, config, runID)
}

func WriteManifest(path string, manifest RunManifest) error {
	content, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	content = append(content, '\n')
	temporary := path + ".tmp"
	if err = os.WriteFile(temporary, content, 0o644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}
func HashFile(path string) (FileEvidence, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return FileEvidence{}, err
	}
	digest := sha256.Sum256(content)
	return FileEvidence{Bytes: int64(len(content)), SHA256: hex.EncodeToString(digest[:])}, nil
}
func GitProvenance(projectRoot string) (string, bool, error) {
	commitBytes, err := exec.Command("git", "-C", projectRoot, "rev-parse", "HEAD").Output()
	if err != nil {
		return "", false, err
	}
	status, err := exec.Command("git", "-C", projectRoot, "status", "--porcelain", "--untracked-files=no").Output()
	if err != nil {
		return "", false, err
	}
	return strings.TrimSpace(string(commitBytes)), len(status) == 0, nil
}
func NowUTC() string { return time.Now().UTC().Format(time.RFC3339Nano) }
func readTrim(path string) (string, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	value := strings.TrimSpace(string(content))
	if value == "" {
		return "", errors.New("empty system provenance")
	}
	return value, nil
}
func firstReadable(paths ...string) (string, error) {
	for _, path := range paths {
		if value, err := readTrim(path); err == nil {
			return value, nil
		}
	}
	return "", errors.New("required system provenance unavailable")
}
func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
func allInts(values []int, want int) bool {
	if len(values) == 0 {
		return false
	}
	for _, v := range values {
		if v != want {
			return false
		}
	}
	return true
}
func uniqueStrings(values []string) string {
	if len(values) == 0 {
		return ""
	}
	want := values[0]
	for _, v := range values {
		if v != want {
			return ""
		}
	}
	return want
}
