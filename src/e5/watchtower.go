// Package e5 implements the minimal E5 watchtower storage and scan benchmark.
package e5

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unsafe"

	e2 "pqc-energy-trading/e2"
)

const (
	ConfigLayerAware            = "layer_aware"
	StateIDBytes                = 8
	FrozenPenaltyBlobBytes      = 5387
	MinimumScientificIterations = 1000
	DefaultWarmupIterations     = 100
	ManifestSchema              = "pqc-energy-trading.e5-condition-evidence.v1"
)

var ScientificStateCounts = []uint64{10, 100, 1000, 10000, 100000}
var ExpectedScientificAffinity = []int{2, 4, 6, 8, 10, 12, 14}

func ValidateRunParameters(nStates uint64, warmup, measured int, scientific bool) error {
	if nStates == 0 || warmup < 0 || measured <= 0 {
		return errors.New("n-states and measured iterations must be positive; warmup cannot be negative")
	}
	if scientific && measured < MinimumScientificIterations {
		return fmt.Errorf("scientific E5 requires at least %d measured iterations", MinimumScientificIterations)
	}
	return nil
}

// GeneratePenaltyBlob uses the frozen E2 state machine, real layer-aware
// backend, and canonical serializer. Key generation, signing, and serialization
// occur once per E5 invocation, never once per stored state.
func GeneratePenaltyBlob() ([]byte, error) {
	transition, err := e2.TransitionForMeasurement(e2.ConfigurationLayerAware, e2.TransitionPenalty)
	if err != nil {
		return nil, err
	}
	backend, err := e2.NewRealCryptoBackend(
		e2.ConfigurationLayerAware,
		[]e2.PartyID{transition.Actor, transition.Counterparty},
	)
	if err != nil {
		return nil, err
	}
	defer backend.Close()
	serializer := e2.CanonicalSerializer{}
	authorized, err := e2.AuthorizeTransition(serializer, backend, transition)
	if err != nil {
		return nil, err
	}
	blob, err := e2.ScientificSerializedTransaction(serializer, authorized)
	if err != nil {
		return nil, err
	}
	return append([]byte(nil), blob...), nil
}

func RecordSize(blob []byte) (int64, error) {
	if len(blob) == 0 {
		return 0, errors.New("penalty blob is empty")
	}
	return int64(StateIDBytes + len(blob)), nil
}

// WriteArtifact streams records to a new file and reports its actual stat size.
func WriteArtifact(path string, nStates uint64, blob []byte) (int64, error) {
	if nStates == 0 {
		return 0, errors.New("n_states must be positive")
	}
	recordSize, err := RecordSize(blob)
	if err != nil {
		return 0, err
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return 0, err
	}
	complete := false
	defer func() {
		file.Close()
		if !complete {
			_ = os.Remove(path)
		}
	}()
	writer := bufio.NewWriterSize(file, 1024*1024)
	var identifier [StateIDBytes]byte
	for stateID := uint64(0); stateID < nStates; stateID++ {
		binary.BigEndian.PutUint64(identifier[:], stateID)
		if _, err := writer.Write(identifier[:]); err != nil {
			return 0, err
		}
		if _, err := writer.Write(blob); err != nil {
			return 0, err
		}
	}
	if err := writer.Flush(); err != nil {
		return 0, err
	}
	if err := file.Sync(); err != nil {
		return 0, err
	}
	if err := file.Close(); err != nil {
		return 0, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	expected := int64(nStates) * recordSize
	if info.Size() != expected {
		return 0, fmt.Errorf("artifact size = %d, want %d", info.Size(), expected)
	}
	complete = true
	return info.Size(), nil
}

// ValidateArtifact checks every state identifier and every unchanged E2 blob.
// It runs outside scan timing and returns observed per-record blob lengths.
func ValidateArtifact(path string, nStates uint64, blob []byte) ([]int, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	recordSize, err := RecordSize(blob)
	if err != nil {
		return nil, err
	}
	if info.Size() != int64(nStates)*recordSize {
		return nil, errors.New("artifact length does not match fixed records")
	}
	reader := bufio.NewReaderSize(file, 1024*1024)
	identifier := make([]byte, StateIDBytes)
	payload := make([]byte, len(blob))
	lengths := make([]int, 0, nStates)
	for expectedID := uint64(0); expectedID < nStates; expectedID++ {
		if _, err := io.ReadFull(reader, identifier); err != nil {
			return nil, err
		}
		if got := binary.BigEndian.Uint64(identifier); got != expectedID {
			return nil, fmt.Errorf("state identifier = %d, want %d", got, expectedID)
		}
		if _, err := io.ReadFull(reader, payload); err != nil {
			return nil, err
		}
		if !bytes.Equal(payload, blob) {
			return nil, fmt.Errorf("state %d penalty blob differs from canonical E2 bytes", expectedID)
		}
		lengths = append(lengths, len(payload))
	}
	if extra, err := reader.ReadByte(); err != io.EOF || extra != 0 {
		return nil, errors.New("artifact contains trailing bytes")
	}
	return lengths, nil
}

type MappedScanner struct {
	data       []byte
	recordSize int
	blobBytes  int
}

func OpenMappedScanner(path string, blobBytes int) (*MappedScanner, error) {
	if blobBytes <= 0 {
		return nil, errors.New("blob length must be positive")
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	recordSize := StateIDBytes + blobBytes
	if info.Size() <= 0 || info.Size()%int64(recordSize) != 0 || info.Size() > int64(^uint(0)>>1) {
		return nil, errors.New("artifact is not a non-empty sequence of fixed records")
	}
	data, err := syscall.Mmap(int(file.Fd()), 0, int(info.Size()), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil {
		return nil, err
	}
	return &MappedScanner{data: data, recordSize: recordSize, blobBytes: blobBytes}, nil
}

func (scanner *MappedScanner) Close() error {
	if scanner == nil || scanner.data == nil {
		return nil
	}
	err := syscall.Munmap(scanner.data)
	scanner.data = nil
	return err
}

func (scanner *MappedScanner) StateCount() uint64 {
	return uint64(len(scanner.data) / scanner.recordSize)
}

// Prefault touches every mapped page outside the measured interval.
func (scanner *MappedScanner) Prefault() byte {
	var sink byte
	page := os.Getpagesize()
	for offset := 0; offset < len(scanner.data); offset += page {
		sink ^= scanner.data[offset]
	}
	sink ^= scanner.data[len(scanner.data)-1]
	return sink
}

// Scan performs a sequential identifier scan. Nonmatching payloads are skipped
// by offset; only the matching payload is copied. visited supports audit tests.
func (scanner *MappedScanner) Scan(target uint64) (payload []byte, visited uint64, err error) {
	if scanner == nil || scanner.data == nil {
		return nil, 0, errors.New("scanner is closed")
	}
	for offset := 0; offset < len(scanner.data); offset += scanner.recordSize {
		visited++
		identifier := binary.BigEndian.Uint64(scanner.data[offset : offset+StateIDBytes])
		if identifier == target {
			start := offset + StateIDBytes
			return append([]byte(nil), scanner.data[start:start+scanner.blobBytes]...), visited, nil
		}
	}
	return nil, visited, fmt.Errorf("state %d not found", target)
}

func processCPUSeconds() (float64, error) {
	// Linux CLOCK_PROCESS_CPUTIME_ID is accumulated user+system process CPU
	// time. Calling clock_gettime directly provides nanosecond resolution and
	// does not introduce wall-clock time into scan_cpu_ms.
	const clockProcessCPUTimeID = 2
	var value syscall.Timespec
	_, _, errno := syscall.RawSyscall(
		syscall.SYS_CLOCK_GETTIME,
		uintptr(clockProcessCPUTimeID),
		uintptr(unsafe.Pointer(&value)),
		0,
	)
	if errno != 0 {
		return 0, errno
	}
	return float64(value.Sec) + float64(value.Nsec)/1e9, nil
}

// TimedScan measures only user+system process CPU consumed by Scan. Payload
// equality, crypto, artifact generation, mmap, and prefaulting remain outside.
func TimedScan(scanner *MappedScanner, target uint64) ([]byte, uint64, float64, error) {
	start, err := processCPUSeconds()
	if err != nil {
		return nil, 0, 0, err
	}
	payload, visited, scanErr := scanner.Scan(target)
	end, timingErr := processCPUSeconds()
	if timingErr != nil {
		return nil, visited, 0, timingErr
	}
	return payload, visited, (end - start) * 1000, scanErr
}

type Sample struct {
	Iteration  int
	Phase      string
	Scientific bool
	NStates    uint64
	BlobBytes  int
	TotalBytes int64
	ScanCPUMS  float64
	Success    bool
	Error      string
}

var RawFields = []string{
	"iteration", "phase", "scientific", "n_states", "blob_bytes",
	"total_bytes", "scan_cpu_ms", "success", "error",
}

func WriteSamples(writer io.Writer, samples []Sample) error {
	csvWriter := csv.NewWriter(writer)
	csvWriter.UseCRLF = false
	if err := csvWriter.Write(RawFields); err != nil {
		return err
	}
	for _, sample := range samples {
		if err := csvWriter.Write([]string{
			strconv.Itoa(sample.Iteration), sample.Phase,
			strconv.FormatBool(sample.Scientific), strconv.FormatUint(sample.NStates, 10),
			strconv.Itoa(sample.BlobBytes), strconv.FormatInt(sample.TotalBytes, 10),
			fmt.Sprintf("%.9f", sample.ScanCPUMS), strconv.FormatBool(sample.Success), sample.Error,
		}); err != nil {
			return err
		}
	}
	csvWriter.Flush()
	return csvWriter.Error()
}

type PolicyValues struct {
	Status string            `json:"status"`
	Values map[string]string `json:"values_by_policy"`
}

type TimingEnvironment struct {
	CPUModel      string       `json:"cpu_model"`
	Affinity      []int        `json:"process_affinity"`
	ScalingDriver PolicyValues `json:"scaling_driver"`
	Governor      PolicyValues `json:"governor"`
	EPP           PolicyValues `json:"energy_performance_preference"`
	ScalingMinKHz PolicyValues `json:"scaling_min_freq_khz"`
	ScalingMaxKHz PolicyValues `json:"scaling_max_freq_khz"`
	BoostStatus   string       `json:"boost_status"`
	BoostEnabled  *bool        `json:"boost_enabled"`
	ACStatus      string       `json:"ac_status"`
	ACOnline      *bool        `json:"ac_online"`
}

type Environment struct {
	GitCommit string            `json:"git_commit"`
	GitDirty  bool              `json:"git_dirty"`
	GoVersion string            `json:"go_version"`
	Kernel    string            `json:"kernel"`
	Timing    TimingEnvironment `json:"timing"`
}

func optionalRead(path string) string {
	content, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(content))
}

func collectPolicies(sysRoot, field string) PolicyValues {
	paths, _ := filepath.Glob(filepath.Join(sysRoot, "devices/system/cpu/cpufreq/policy*"))
	sort.Strings(paths)
	values := make(map[string]string)
	missing := false
	for _, policy := range paths {
		value := optionalRead(filepath.Join(policy, field))
		if value == "" {
			missing = true
		} else {
			values[filepath.Base(policy)] = value
		}
	}
	status := "available"
	if len(values) == 0 {
		status = "unavailable"
	} else if missing {
		status = "partial"
	}
	return PolicyValues{Status: status, Values: values}
}

func parseCPUList(value string) ([]int, error) {
	var cpus []int
	seen := make(map[int]bool)
	for _, part := range strings.Split(value, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		bounds := strings.SplitN(part, "-", 2)
		start, err := strconv.Atoi(bounds[0])
		if err != nil || start < 0 {
			return nil, errors.New("invalid CPU list")
		}
		end := start
		if len(bounds) == 2 {
			end, err = strconv.Atoi(bounds[1])
			if err != nil || end < start {
				return nil, errors.New("invalid CPU range")
			}
		}
		for cpu := start; cpu <= end; cpu++ {
			if !seen[cpu] {
				cpus = append(cpus, cpu)
				seen[cpu] = true
			}
		}
	}
	sort.Ints(cpus)
	return cpus, nil
}

func cpuAffinity(procRoot string) ([]int, error) {
	content, err := os.ReadFile(filepath.Join(procRoot, "self/status"))
	if err != nil {
		return nil, err
	}
	for _, line := range strings.Split(string(content), "\n") {
		if strings.HasPrefix(line, "Cpus_allowed_list:") {
			return parseCPUList(strings.TrimSpace(strings.TrimPrefix(line, "Cpus_allowed_list:")))
		}
	}
	return nil, errors.New("process CPU affinity is unavailable")
}

func boolSetting(path string) (string, *bool) {
	value := optionalRead(path)
	if value == "" {
		return "unavailable", nil
	}
	enabled := value == "1"
	if value != "0" && value != "1" {
		return "invalid", nil
	}
	return "available", &enabled
}

func acSetting(sysRoot string) (string, *bool) {
	paths, _ := filepath.Glob(filepath.Join(sysRoot, "class/power_supply/*"))
	found := false
	online := false
	for _, path := range paths {
		if optionalRead(filepath.Join(path, "type")) != "Mains" {
			continue
		}
		value := optionalRead(filepath.Join(path, "online"))
		if value == "0" || value == "1" {
			found = true
			online = online || value == "1"
		}
	}
	if !found {
		return "unavailable", nil
	}
	return "available", &online
}

func cpuModel(procRoot string) string {
	content := optionalRead(filepath.Join(procRoot, "cpuinfo"))
	for _, line := range strings.Split(content, "\n") {
		if strings.HasPrefix(strings.ToLower(line), "model name") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				return strings.TrimSpace(parts[1])
			}
		}
	}
	return ""
}

func CollectEnvironment(repoRoot, sysRoot, procRoot string) (Environment, error) {
	command := func(name string, args ...string) (string, error) {
		output, err := exec.Command(name, args...).CombinedOutput()
		return strings.TrimSpace(string(output)), err
	}
	commitCommand := exec.Command("git", "-C", repoRoot, "rev-parse", "HEAD")
	commitOutput, err := commitCommand.Output()
	if err != nil {
		return Environment{}, err
	}
	dirtyCommand := exec.Command("git", "-C", repoRoot, "status", "--porcelain")
	dirtyOutput, err := dirtyCommand.Output()
	if err != nil {
		return Environment{}, err
	}
	kernel, err := command("uname", "-r")
	if err != nil {
		return Environment{}, err
	}
	affinity, err := cpuAffinity(procRoot)
	if err != nil {
		return Environment{}, err
	}
	boostStatus, boostEnabled := boolSetting(filepath.Join(sysRoot, "devices/system/cpu/cpufreq/boost"))
	if boostStatus == "unavailable" {
		boostStatus, boostEnabled = boolSetting(filepath.Join(sysRoot, "devices/system/cpu/amd_pstate/cpb_boost"))
	}
	acStatus, acOnline := acSetting(sysRoot)
	return Environment{
		GitCommit: strings.TrimSpace(string(commitOutput)),
		GitDirty:  len(bytes.TrimSpace(dirtyOutput)) != 0,
		GoVersion: runtime.Version(),
		Kernel:    kernel,
		Timing: TimingEnvironment{
			CPUModel: cpuModel(procRoot), Affinity: affinity,
			ScalingDriver: collectPolicies(sysRoot, "scaling_driver"),
			Governor:      collectPolicies(sysRoot, "scaling_governor"),
			EPP:           collectPolicies(sysRoot, "energy_performance_preference"),
			ScalingMinKHz: collectPolicies(sysRoot, "scaling_min_freq"),
			ScalingMaxKHz: collectPolicies(sysRoot, "scaling_max_freq"),
			BoostStatus:   boostStatus, BoostEnabled: boostEnabled,
			ACStatus: acStatus, ACOnline: acOnline,
		},
	}, nil
}

func uniformValues(field PolicyValues, name string, required bool) (string, error) {
	if field.Status == "unavailable" && !required {
		return "", nil
	}
	if field.Status != "available" || len(field.Values) == 0 {
		return "", fmt.Errorf("scientific E5 requires complete %s provenance", name)
	}
	var result string
	for _, value := range field.Values {
		if result == "" {
			result = value
		} else if value != result {
			return "", fmt.Errorf("%s differs across CPU policies", name)
		}
	}
	return result, nil
}

func ValidateScientificEnvironment(environment Environment) error {
	if environment.GitCommit == "" || environment.GitDirty {
		return errors.New("scientific E5 requires a clean Git commit")
	}
	if environment.GoVersion == "" || environment.Kernel == "" || environment.Timing.CPUModel == "" {
		return errors.New("scientific E5 software, kernel, and CPU-model provenance is required")
	}
	if !equalInts(environment.Timing.Affinity, ExpectedScientificAffinity) {
		return fmt.Errorf("scientific E5 affinity = %v, want %v", environment.Timing.Affinity, ExpectedScientificAffinity)
	}
	if _, err := uniformValues(environment.Timing.ScalingDriver, "scaling driver", true); err != nil {
		return err
	}
	governor, err := uniformValues(environment.Timing.Governor, "governor", true)
	if err != nil || governor != "performance" {
		return errors.New("scientific E5 requires performance governor")
	}
	epp, err := uniformValues(environment.Timing.EPP, "EPP", false)
	if err != nil || (epp != "" && epp != "performance") {
		return errors.New("scientific E5 requires performance EPP when available")
	}
	minimum, err := uniformValues(environment.Timing.ScalingMinKHz, "scaling minimum", true)
	if err != nil {
		return err
	}
	maximum, err := uniformValues(environment.Timing.ScalingMaxKHz, "scaling maximum", true)
	if err != nil || minimum != maximum {
		return errors.New("scientific E5 requires equal, consistent scaling min/max")
	}
	if environment.Timing.BoostStatus != "available" || environment.Timing.BoostEnabled == nil || *environment.Timing.BoostEnabled {
		return errors.New("scientific E5 requires boost disabled")
	}
	if environment.Timing.ACStatus == "available" && (environment.Timing.ACOnline == nil || !*environment.Timing.ACOnline) {
		return errors.New("scientific E5 requires AC online when available")
	}
	if environment.Timing.ACStatus != "available" && environment.Timing.ACStatus != "unavailable" {
		return errors.New("invalid AC provenance")
	}
	return nil
}

func equalInts(left, right []int) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func SHA256File(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	digest := sha256.New()
	size, err := io.Copy(digest, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(digest.Sum(nil)), size, nil
}

func WriteJSONExclusive(path string, value any) error {
	content, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	content = append(content, '\n')
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(content); err != nil {
		return err
	}
	return file.Sync()
}
