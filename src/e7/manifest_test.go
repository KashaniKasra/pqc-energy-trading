package e7

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func boolPointer(value bool) *bool { return &value }

func validScientificEnvironment() ScientificEnvironment {
	return ScientificEnvironment{
		CPUModel: "AMD Ryzen 7 7735HS with Radeon Graphics", LogicalCPUCount: 16,
		ScalingDriver: []string{"amd-pstate-epp"}, Governor: []string{"performance"},
		EPP: []string{"performance"}, ScalingMinFreqKHz: []int{3200000},
		ScalingMaxFreqKHz: []int{3200000}, BoostEnabled: boolPointer(false),
		ACOnline: boolPointer(true), Affinity: append([]int(nil), ExpectedScientificCPUs...),
		ExpectedAffinity: append([]int(nil), ExpectedScientificCPUs...), Passed: true,
	}
}

func TestScientificEnvironmentFailsClosedAndDryRunBypassesHostPolicy(t *testing.T) {
	if err := ValidateRunMode(RunModeDryRun, ScientificEnvironment{}); err != nil {
		t.Fatal(err)
	}
	base := validScientificEnvironment()
	if err := ValidateRunMode(RunModeScientific, base); err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(*ScientificEnvironment){
		"affinity": func(value *ScientificEnvironment) { value.Affinity = []int{2} },
		"governor": func(value *ScientificEnvironment) { value.Governor = []string{"powersave"} },
		"epp":      func(value *ScientificEnvironment) { value.EPP = []string{"balance_performance"} },
		"min":      func(value *ScientificEnvironment) { value.ScalingMinFreqKHz = []int{400000} },
		"max":      func(value *ScientificEnvironment) { value.ScalingMaxFreqKHz = []int{4800000} },
		"boost":    func(value *ScientificEnvironment) { value.BoostEnabled = boolPointer(true) },
		"ac":       func(value *ScientificEnvironment) { value.ACOnline = boolPointer(false) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			value := base
			mutate(&value)
			if ValidateRunMode(RunModeScientific, value) == nil {
				t.Fatal("invalid scientific environment accepted")
			}
		})
	}
}

func TestManifestSerializationAndValidation(t *testing.T) {
	semantics, _ := ConfigSemantics(ConfigurationClassicalL2)
	codeHashes := map[string]string{}
	for _, path := range RequiredCodeConfigPaths {
		codeHashes[path] = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	}
	manifest := RunManifest{
		Schema: ManifestSchema, Experiment: "E7", Config: ConfigurationClassicalL2,
		RunID: RunID, GitCommit: "0123456789012345678901234567890123456789", GitTrackedClean: true,
		TraceSHA256: TraceSHA256, TraceProvenanceSHA256: "1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c",
		WarmupRows: WarmupRowCount, MeasuredRows: MeasuredRowCount, TimerDefinition: TimerDefinition,
		ConfigSemantics: semantics, ProcessResetMethod: L2ResetMethod, Host: validScientificEnvironment(),
		CodeConfigSHA256: codeHashes,
		RuntimeVersions: map[string]string{"go": "go1.22.2", "python": "Python 3.12.3", "fabric_gateway_go": FabricGatewayVersion,
			"liboqs": ExpectedLiboqsVersion, "liboqs_go": ExpectedLiboqsGoVersion},
		Status: "complete", RawFiles: map[string]FileEvidence{
			"samples.csv": {Bytes: 12, SHA256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
			"run.log":     {Bytes: 12, SHA256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
		},
	}
	if err := ValidateCompleteManifest(manifest); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "manifest.json")
	if err := WriteManifest(path, manifest); err != nil {
		t.Fatal(err)
	}
	var decoded RunManifest
	content, _ := os.ReadFile(path)
	if err := json.Unmarshal(content, &decoded); err != nil || decoded.Config != manifest.Config {
		t.Fatal("manifest did not round-trip")
	}
	decoded.GitTrackedClean = false
	if ValidateCompleteManifest(decoded) == nil {
		t.Fatal("dirty manifest accepted")
	}
}

func validFabricCPUIsolation() FabricCPUIsolation {
	return FabricCPUIsolation{
		ExpectedCPUSet: ExpectedFabricCPUSet,
		Components: []FabricCPUComponent{
			{Role: "orderer", Container: "orderer.example.com", CPUSet: ExpectedFabricCPUSet},
			{Role: "peer0.org1", Container: "peer0.org1.example.com", CPUSet: ExpectedFabricCPUSet, ChaincodeLaunchCPUSet: ExpectedFabricCPUSet},
			{Role: "peer1.org1", Container: "peer1.org1.example.com", CPUSet: ExpectedFabricCPUSet, ChaincodeLaunchCPUSet: ExpectedFabricCPUSet},
			{Role: "peer0.org2", Container: "peer0.org2.example.com", CPUSet: ExpectedFabricCPUSet, ChaincodeLaunchCPUSet: ExpectedFabricCPUSet},
			{Role: "peer1.org2", Container: "peer1.org2.example.com", CPUSet: ExpectedFabricCPUSet, ChaincodeLaunchCPUSet: ExpectedFabricCPUSet},
			{Role: "simplekv_chaincode", Container: "dev-peer0.org1-simplekv", CPUSet: ExpectedFabricCPUSet,
				ChaincodeID: "simplekv_1:" + strings.Repeat("a", 64)},
		},
	}
}

func TestManifestFabricCPUProvenanceIsConfigSpecific(t *testing.T) {
	record := validFabricCPUIsolation()
	if err := ValidateFabricCPUIsolationRecord(record); err != nil {
		t.Fatal(err)
	}
	bad := record
	bad.Components = append([]FabricCPUComponent(nil), record.Components...)
	bad.Components[0].CPUSet = "2-15"
	if ValidateFabricCPUIsolationRecord(bad) == nil {
		t.Fatal("broad Fabric CPU set accepted")
	}
	bad = record
	bad.Components = append([]FabricCPUComponent(nil), record.Components...)
	bad.Components[1].ChaincodeLaunchCPUSet = ""
	if ValidateFabricCPUIsolationRecord(bad) == nil {
		t.Fatal("missing peer chaincode launch CPU set accepted")
	}
	bad = record
	bad.Components = append([]FabricCPUComponent(nil), record.Components...)
	bad.Components[0].ChaincodeLaunchCPUSet = ExpectedFabricCPUSet
	if ValidateFabricCPUIsolationRecord(bad) == nil {
		t.Fatal("fabricated orderer chaincode launch CPU set accepted")
	}
	bad = record
	bad.Components = append([]FabricCPUComponent(nil), record.Components...)
	bad.Components[len(bad.Components)-1].ChaincodeLaunchCPUSet = ExpectedFabricCPUSet
	if ValidateFabricCPUIsolationRecord(bad) == nil {
		t.Fatal("fabricated chaincode-container launch CPU set accepted")
	}

	semantics, _ := ConfigSemantics(ConfigurationClassicalL2)
	codeHashes := map[string]string{}
	for _, path := range RequiredCodeConfigPaths {
		codeHashes[path] = strings.Repeat("c", 64)
	}
	l2 := RunManifest{
		Schema: ManifestSchema, Experiment: "E7", Config: ConfigurationClassicalL2,
		RunID: RunID, GitCommit: strings.Repeat("a", 40), GitTrackedClean: true,
		TraceSHA256: TraceSHA256, TraceProvenanceSHA256: strings.Repeat("b", 64),
		WarmupRows: WarmupRowCount, MeasuredRows: MeasuredRowCount, TimerDefinition: TimerDefinition,
		ConfigSemantics: semantics, ProcessResetMethod: L2ResetMethod, CodeConfigSHA256: codeHashes,
		Host: validScientificEnvironment(),
		RuntimeVersions: map[string]string{"go": "go-test", "python": "python-test", "fabric_gateway_go": FabricGatewayVersion,
			"liboqs": ExpectedLiboqsVersion, "liboqs_go": ExpectedLiboqsGoVersion},
		Status: "complete", RawFiles: map[string]FileEvidence{
			"samples.csv": {Bytes: 1, SHA256: strings.Repeat("d", 64)},
			"run.log":     {Bytes: 1, SHA256: strings.Repeat("e", 64)},
		},
	}
	if err := ValidateCompleteManifest(l2); err != nil {
		t.Fatal(err)
	}
	l2.FabricCPUIsolation = &record
	if ValidateCompleteManifest(l2) == nil {
		t.Fatal("L2 manifest accepted Fabric container provenance")
	}
}

func TestRunDirectoryRefusesCollision(t *testing.T) {
	root := t.TempDir()
	if _, err := CreateRunDirectory(root, ConfigurationL1Only, RunID); err != nil {
		t.Fatal(err)
	}
	if _, err := CreateRunDirectory(root, ConfigurationL1Only, RunID); err == nil {
		t.Fatal("existing run directory was overwritten")
	}
}

func TestPreflightFailureDoesNotCreateRunDirectory(t *testing.T) {
	root := t.TempDir()
	_, err := CreateRunDirectoryAfterPreflight(root, ConfigurationL1Only, RunID, func() error {
		return errors.New("injected preflight failure")
	})
	if err == nil {
		t.Fatal("preflight failure was ignored")
	}
	if _, statErr := os.Stat(filepath.Join(root, string(ConfigurationL1Only), "run_1")); !os.IsNotExist(statErr) {
		t.Fatal("preflight failure left a run directory")
	}
}
