package main

import (
	"bufio"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const androidScientificCPU = "7"

const (
	androidBlockStartBudget = 59 * time.Second
	androidBlockHardMaximum = 60 * time.Second
	androidMonitoringTarget = 6 * time.Second
	androidScheduleSchema   = "pqc-energy-trading.e0-android-sustained-schedule.v1"
)

type timedOperationSelection struct {
	TimedIndex           int
	OperationOrdinal     int
	TargetOffsetNS       int64
	SelectionMonotonicNS int64
}

type sustainedWorkloadSelector struct {
	measuredStartNS int64
	startBudgetNS   int64
	targets         []int64
	selections      []timedOperationSelection
	operationCount  int
}

func distributedTargetOffsets(count int, monitoringTargetNS int64) ([]int64, error) {
	if count <= 0 || monitoringTargetNS <= 0 {
		return nil, fmt.Errorf("invalid sustained-workload target parameters")
	}
	targets := make([]int64, count)
	if count == 1 {
		// A singleton can only occur as the final remainder after earlier
		// accepted blocks. Place it at the end of the monitored interval;
		// never fabricate a second observation or discard the remainder.
		targets[0] = monitoringTargetNS
		return targets, nil
	}
	denominator := int64(count - 1)
	for index := range targets {
		targets[index] = int64(index) * monitoringTargetNS / denominator
	}
	return targets, nil
}

func (selector *sustainedWorkloadSelector) decide(nowNS int64, completedTimed, operationOrdinal int) (bool, bool, error) {
	if selector.measuredStartNS <= 0 || nowNS < selector.measuredStartNS || operationOrdinal <= 0 {
		return false, false, fmt.Errorf("invalid sustained-workload selector state")
	}
	if completedTimed < 0 || completedTimed >= len(selector.targets) || completedTimed != len(selector.selections) {
		return false, false, fmt.Errorf("invalid sustained-workload timing index")
	}
	elapsed := nowNS - selector.measuredStartNS
	if elapsed >= selector.startBudgetNS {
		return false, false, nil
	}
	selector.operationCount = operationOrdinal
	if elapsed < selector.targets[completedTimed] {
		return false, true, nil
	}
	selector.selections = append(selector.selections, timedOperationSelection{
		TimedIndex: completedTimed, OperationOrdinal: operationOrdinal,
		TargetOffsetNS: selector.targets[completedTimed], SelectionMonotonicNS: nowNS,
	})
	return true, true, nil
}

type blockSummary struct {
	WarmupIterations    int    `json:"warmup_iterations"`
	RequestedIterations int    `json:"requested_iterations"`
	CompletedIterations int    `json:"completed_iterations"`
	StartBudgetNS       int64  `json:"start_budget_ns"`
	HardMaximumNS       int64  `json:"hard_maximum_ns"`
	BlockDurationNS     int64  `json:"block_duration_ns"`
	CrossedStartBudget  bool   `json:"crossed_start_budget"`
	ExceededHardMaximum bool   `json:"exceeded_hard_maximum"`
	ScheduleSchema      string `json:"schedule_schema"`
	SchedulingAlgorithm string `json:"scheduling_algorithm"`
	MonitoringTargetNS  int64  `json:"monitoring_target_ns"`
	TotalOperations     int    `json:"total_primitive_operations"`
	TimedOperations     int    `json:"timed_primitive_operations"`
	UntimedOperations   int    `json:"untimed_primitive_operations"`
}

type phaseEvent struct {
	Event          string                `json:"event"`
	MonotonicNS    int64                 `json:"monotonic_ns"`
	Stabilization  *stabilizationMessage `json:"stabilization,omitempty"`
	OperationCount int                   `json:"operation_count,omitempty"`
	Block          *blockSummary         `json:"block,omitempty"`
}

func mayStartMeasuredOperation(nowNS, measuredStartNS, startBudgetNS int64) (bool, error) {
	if measuredStartNS <= 0 || startBudgetNS <= 0 || nowNS < measuredStartNS {
		return false, fmt.Errorf("invalid measurement block deadline state")
	}
	return nowNS-measuredStartNS < startBudgetNS, nil
}

func blockExceedsHardMaximum(durationNS int64) bool {
	return durationNS > int64(androidBlockHardMaximum)
}

func currentAllowedCPUs(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", fmt.Errorf("open CPU affinity status: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "Cpus_allowed_list:") {
			value := strings.TrimSpace(strings.TrimPrefix(line, "Cpus_allowed_list:"))
			if value == "" {
				return "", fmt.Errorf("Cpus_allowed_list is empty")
			}
			return value, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return "", fmt.Errorf("read CPU affinity status: %w", err)
	}
	return "", fmt.Errorf("Cpus_allowed_list not found")
}

func createExclusive(path string) (*os.File, error) {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return nil, fmt.Errorf("create collision-safe output %s: %w", path, err)
	}
	return file, nil
}

func writeTimingSamplesExclusive(path string, samples []float64) error {
	if len(samples) == 0 {
		return fmt.Errorf("no timing samples to write")
	}
	file, err := createExclusive(path)
	if err != nil {
		return err
	}
	defer file.Close()

	w := csv.NewWriter(file)
	if err := w.Write([]string{"iteration", "elapsed_ms"}); err != nil {
		return err
	}
	for index, sample := range samples {
		if err := w.Write([]string{
			strconv.Itoa(index + 1),
			strconv.FormatFloat(sample, 'f', 9, 64),
		}); err != nil {
			return err
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return err
	}
	return file.Sync()
}

func writeScheduleExclusive(path string, selections []timedOperationSelection) error {
	if len(selections) == 0 {
		return fmt.Errorf("no timed operation selections to write")
	}
	file, err := createExclusive(path)
	if err != nil {
		return err
	}
	defer file.Close()
	w := csv.NewWriter(file)
	if err := w.Write([]string{"timed_index", "operation_ordinal", "target_offset_ns", "selection_monotonic_ns"}); err != nil {
		return err
	}
	for _, selection := range selections {
		if err := w.Write([]string{
			strconv.Itoa(selection.TimedIndex), strconv.Itoa(selection.OperationOrdinal),
			strconv.FormatInt(selection.TargetOffsetNS, 10), strconv.FormatInt(selection.SelectionMonotonicNS, 10),
		}); err != nil {
			return err
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return err
	}
	return file.Sync()
}

func runAndroidCondition(args []string) error {
	flags := flag.NewFlagSet("android-condition", flag.ContinueOnError)
	scheme := flags.String("scheme", "", "E0 scheme")
	operation := flags.String("operation", "", "E0 operation")
	rawOutput := flags.String("raw-output", "", "new timing CSV path")
	phaseOutput := flags.String("phase-output", "", "new phase JSONL path")
	scheduleOutput := flags.String("schedule-output", "", "new timed-operation schedule CSV path")
	stabilizationAddress := flags.String("stabilization-address", "", "device-local sampler TCP address")
	stabilizationToken := flags.String("stabilization-token", "", "unique per-attempt coordination token")
	stabilizationTimeout := flags.Duration("stabilization-timeout", defaultStabilizationTimeout, "finite stabilization safety timeout")
	legacyNoStabilization := flags.Bool("legacy-no-stabilization", false, "non-scientific compatibility mode")
	requestedIterations := flags.Int("requested-iterations", meterIterations, "maximum measured timings requested for this block")
	blockStartBudget := flags.Duration("block-start-budget", androidBlockStartBudget, "stop starting primitives at this measured duration")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || *scheme == "" || *operation == "" || *rawOutput == "" || *phaseOutput == "" {
		return fmt.Errorf("android-condition requires --scheme, --operation, --raw-output, and --phase-output")
	}
	if !isValidSchemeOperation(*scheme, *operation) {
		return fmt.Errorf("invalid scheme/operation: %s/%s", *scheme, *operation)
	}
	if *rawOutput == *phaseOutput {
		return fmt.Errorf("raw and phase output paths must differ")
	}
	if *legacyNoStabilization {
		if *stabilizationAddress != "" || *stabilizationToken != "" {
			return fmt.Errorf("legacy compatibility mode cannot use stabilization coordination")
		}
	} else if *stabilizationAddress == "" || *stabilizationToken == "" || *stabilizationTimeout < minimumStabilizationDuration || *scheduleOutput == "" {
		return fmt.Errorf("scientific Android condition requires stabilization address, token, and timeout >= 3s")
	}
	if *scheduleOutput != "" && (*scheduleOutput == *rawOutput || *scheduleOutput == *phaseOutput) {
		return fmt.Errorf("schedule, raw, and phase output paths must differ")
	}
	if *requestedIterations <= 0 || *requestedIterations > meterIterations {
		return fmt.Errorf("requested iterations must be in [1,%d]", meterIterations)
	}
	if !*legacyNoStabilization && (*blockStartBudget <= 0 || *blockStartBudget >= androidBlockHardMaximum) {
		return fmt.Errorf("scientific block start budget must be positive and below 60s")
	}
	if _, err := os.Lstat(*rawOutput); err == nil {
		return fmt.Errorf("refusing existing raw output %s", *rawOutput)
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("inspect raw output: %w", err)
	}
	if runtime.GOMAXPROCS(0) != 1 {
		return fmt.Errorf("Android scientific condition requires GOMAXPROCS=1")
	}
	allowed, err := currentAllowedCPUs("/proc/self/status")
	if err != nil {
		return err
	}
	if allowed != androidScientificCPU {
		return fmt.Errorf("Android scientific condition requires exact CPU affinity %s, got %s", androidScientificCPU, allowed)
	}

	phaseFile, err := createExclusive(*phaseOutput)
	if err != nil {
		return err
	}
	defer phaseFile.Close()
	encoder := json.NewEncoder(phaseFile)
	var measuredStartNS int64
	var measuredEndNS int64
	var selector *sustainedWorkloadSelector
	recordEvent := func(value phaseEvent) error {
		if err := encoder.Encode(value); err != nil {
			return err
		}
		return nil
	}
	timingPhaseHook = func(event string) error {
		value, err := monotonicNanoseconds()
		if err != nil {
			return err
		}
		if event == "measured_start" {
			measuredStartNS = value
		} else if event == "measured_end" {
			measuredEndNS = value
		}
		return recordEvent(phaseEvent{Event: event, MonotonicNS: value})
	}
	if !*legacyNoStabilization {
		targets, err := distributedTargetOffsets(*requestedIterations, int64(androidMonitoringTarget))
		if err != nil {
			return err
		}
		stabilizationPhaseHook = func(operation timedOperation, post postOperation) error {
			return runStabilization(
				*stabilizationAddress, *stabilizationToken, *stabilizationTimeout,
				operation, post, recordEvent,
			)
		}
		selector = &sustainedWorkloadSelector{startBudgetNS: int64(*blockStartBudget), targets: targets}
		measurementSelectionHook = func(completedTimed, operationOrdinal int) (bool, bool, error) {
			if measuredStartNS <= 0 {
				return false, false, fmt.Errorf("measured start is unavailable")
			}
			selector.measuredStartNS = measuredStartNS
			now, err := monotonicNanoseconds()
			if err != nil {
				return false, false, err
			}
			return selector.decide(now, completedTimed, operationOrdinal)
		}
	}
	defer func() {
		timingPhaseHook = nil
		stabilizationPhaseHook = nil
		measurementContinueHook = nil
		measurementSelectionHook = nil
	}()

	samples, err := runBenchmark(*scheme, *operation, warmupIterations, *requestedIterations)
	if err != nil {
		return err
	}
	if len(samples) == 0 {
		return fmt.Errorf("measurement block completed no primitive timings")
	}
	if !*legacyNoStabilization {
		if measuredStartNS <= 0 || measuredEndNS <= measuredStartNS {
			return fmt.Errorf("measurement block markers are incomplete")
		}
		duration := measuredEndNS - measuredStartNS
		if selector == nil || len(selector.selections) != len(samples) || selector.operationCount < len(samples) {
			return fmt.Errorf("sustained-workload schedule is incomplete")
		}
		summary := &blockSummary{
			WarmupIterations: warmupIterations, RequestedIterations: *requestedIterations,
			CompletedIterations: len(samples), StartBudgetNS: int64(*blockStartBudget),
			HardMaximumNS: int64(androidBlockHardMaximum), BlockDurationNS: duration,
			CrossedStartBudget:  duration > int64(*blockStartBudget),
			ExceededHardMaximum: blockExceedsHardMaximum(duration),
			ScheduleSchema:      androidScheduleSchema,
			SchedulingAlgorithm: "precomputed linear target offsets; identical untimed primitive operations fill target gaps without sleeps",
			MonitoringTargetNS:  int64(androidMonitoringTarget), TotalOperations: selector.operationCount,
			TimedOperations: len(samples), UntimedOperations: selector.operationCount - len(samples),
		}
		if err := recordEvent(phaseEvent{Event: "block_summary", MonotonicNS: measuredEndNS, Block: summary}); err != nil {
			return err
		}
	}
	if err := writeTimingSamplesExclusive(*rawOutput, samples); err != nil {
		return err
	}
	if !*legacyNoStabilization {
		if err := writeScheduleExclusive(*scheduleOutput, selector.selections); err != nil {
			return err
		}
	}
	if err := phaseFile.Sync(); err != nil {
		return fmt.Errorf("sync phase events: %w", err)
	}
	if !*legacyNoStabilization && blockExceedsHardMaximum(measuredEndNS-measuredStartNS) {
		return fmt.Errorf("measurement block exceeded 60-second hard maximum")
	}
	stats, err := calculateTimingStats(samples)
	if err != nil {
		return err
	}
	fmt.Printf("ANDROID_CONDITION_DONE scheme=%s operation=%s n_iter=%d median_ms=%.9f p95_ms=%.9f p99_ms=%.9f stddev_ms=%.9f\n",
		*scheme, *operation, len(samples), stats.MedianMs, stats.P95Ms, stats.P99Ms, stats.StddevMs)
	return nil
}
