package e5

import (
	"bytes"
	"encoding/binary"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
)

var (
	penaltyOnce sync.Once
	penaltyBlob []byte
	penaltyErr  error
)

func realPenaltyBlob(t *testing.T) []byte {
	t.Helper()
	penaltyOnce.Do(func() { penaltyBlob, penaltyErr = GeneratePenaltyBlob() })
	if penaltyErr != nil {
		t.Fatalf("GeneratePenaltyBlob: %v", penaltyErr)
	}
	return append([]byte(nil), penaltyBlob...)
}

func TestRealE2PenaltyBlob(t *testing.T) {
	blob := realPenaltyBlob(t)
	if len(blob) != FrozenPenaltyBlobBytes {
		t.Fatalf("real layer-aware E2 penalty bytes = %d, want %d", len(blob), FrozenPenaltyBlobBytes)
	}
}

func TestArtifactLayoutValidationAndWorstCaseScan(t *testing.T) {
	blob := realPenaltyBlob(t)
	path := filepath.Join(t.TempDir(), "watchtower.bin")
	total, err := WriteArtifact(path, 3, blob)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	wantRecord := int64(StateIDBytes + len(blob))
	if total != info.Size() || total != 3*wantRecord {
		t.Fatalf("actual total = %d stat = %d want = %d", total, info.Size(), 3*wantRecord)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for stateID := uint64(0); stateID < 3; stateID++ {
		offset := int(stateID) * int(wantRecord)
		if got := binary.BigEndian.Uint64(raw[offset : offset+8]); got != stateID {
			t.Fatalf("big-endian state ID = %d, want %d", got, stateID)
		}
		if !bytes.Equal(raw[offset+8:offset+int(wantRecord)], blob) {
			t.Fatalf("state %d did not retain exact E2 blob", stateID)
		}
	}
	lengths, err := ValidateArtifact(path, 3, blob)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(lengths, []int{len(blob), len(blob), len(blob)}) {
		t.Fatalf("observed blob lengths = %v", lengths)
	}

	scanner, err := OpenMappedScanner(path, len(blob))
	if err != nil {
		t.Fatal(err)
	}
	defer scanner.Close()
	_ = scanner.Prefault()
	matched, visited, cpuMS, err := TimedScan(scanner, 2)
	if err != nil {
		t.Fatal(err)
	}
	if visited != 3 || !bytes.Equal(matched, blob) || cpuMS < 0 {
		t.Fatalf("worst-case scan visited=%d cpu=%f match=%t", visited, cpuMS, bytes.Equal(matched, blob))
	}
	if _, visited, err := scanner.Scan(3); err == nil || visited != 3 {
		t.Fatalf("missing target result visited=%d err=%v", visited, err)
	}
}

func TestArtifactCollisionRefusal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "watchtower.bin")
	if _, err := WriteArtifact(path, 2, []byte("blob")); err != nil {
		t.Fatal(err)
	}
	if _, err := WriteArtifact(path, 2, []byte("blob")); err == nil {
		t.Fatal("second artifact write unexpectedly overwrote existing file")
	}
}

func TestPercentilesAndWarmupExclusion(t *testing.T) {
	samples := []Sample{
		{Iteration: 0, Phase: "warmup", ScanCPUMS: 1000, Success: true},
		{Iteration: 0, Phase: "measured", ScanCPUMS: 1, Success: true},
		{Iteration: 1, Phase: "measured", ScanCPUMS: 2, Success: true},
		{Iteration: 2, Phase: "measured", ScanCPUMS: 3, Success: true},
		{Iteration: 3, Phase: "measured", ScanCPUMS: 4, Success: true},
	}
	summary, err := SummarizeMeasuredScans(samples, 4, false)
	if err != nil {
		t.Fatal(err)
	}
	if summary.MedianMS != 2.5 || math.Abs(summary.P95MS-3.85) > 1e-12 || math.Abs(summary.P99MS-3.97) > 1e-12 {
		t.Fatalf("unexpected interpolated summary: %+v", summary)
	}
	if err := ValidateRunParameters(10, 100, 999, true); err == nil {
		t.Fatal("scientific iteration guard accepted 999")
	}
}

func boolPointer(value bool) *bool { return &value }

func validScientificEnvironment() Environment {
	values := func(value string) PolicyValues {
		return PolicyValues{Status: "available", Values: map[string]string{"policy0": value, "policy1": value}}
	}
	return Environment{
		GitCommit: "abc", GoVersion: "go1.22.2", Kernel: "test",
		Timing: TimingEnvironment{
			CPUModel: "test", Affinity: append([]int(nil), ExpectedScientificAffinity...),
			ScalingDriver: values("amd-pstate-epp"), Governor: values("performance"),
			EPP: values("performance"), ScalingMinKHz: values("3200000"),
			ScalingMaxKHz: values("3200000"), BoostStatus: "available",
			BoostEnabled: boolPointer(false), ACStatus: "available", ACOnline: boolPointer(true),
		},
	}
}

func TestScientificEnvironmentFailsClosed(t *testing.T) {
	base := validScientificEnvironment()
	if err := ValidateScientificEnvironment(base); err != nil {
		t.Fatalf("valid environment rejected: %v", err)
	}
	tests := []struct {
		name string
		edit func(*Environment)
	}{
		{"dirty Git", func(value *Environment) { value.GitDirty = true }},
		{"wrong affinity", func(value *Environment) { value.Timing.Affinity = []int{2} }},
		{"governor", func(value *Environment) { value.Timing.Governor.Values["policy0"] = "powersave" }},
		{"EPP", func(value *Environment) { value.Timing.EPP.Values["policy0"] = "balance_performance" }},
		{"frequency", func(value *Environment) { value.Timing.ScalingMaxKHz.Values["policy0"] = "4000000" }},
		{"boost", func(value *Environment) { value.Timing.BoostEnabled = boolPointer(true) }},
		{"AC", func(value *Environment) { value.Timing.ACOnline = boolPointer(false) }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			value := validScientificEnvironment()
			test.edit(&value)
			if err := ValidateScientificEnvironment(value); err == nil {
				t.Fatal("invalid scientific environment accepted")
			}
		})
	}
	optional := validScientificEnvironment()
	optional.Timing.EPP = PolicyValues{Status: "unavailable", Values: map[string]string{}}
	optional.Timing.ACStatus, optional.Timing.ACOnline = "unavailable", nil
	if err := ValidateScientificEnvironment(optional); err != nil {
		t.Fatalf("unavailable optional fields rejected: %v", err)
	}
}
