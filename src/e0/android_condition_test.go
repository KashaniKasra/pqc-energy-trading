package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestTimingPhaseHookSurroundsOnlyMeasuredLoop(t *testing.T) {
	callCount := 0
	events := []string{}
	eventCalls := []int{}
	timingPhaseHook = func(event string) error {
		events = append(events, event)
		eventCalls = append(eventCalls, callCount)
		return nil
	}
	defer func() { timingPhaseHook = nil }()

	_, err := collectTimingSamples(func() { callCount++ }, nil, 3, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 2 || events[0] != "measured_start" || events[1] != "measured_end" {
		t.Fatalf("unexpected events: %v", events)
	}
	if eventCalls[0] != 3 || eventCalls[1] != 8 {
		t.Fatalf("phase events at operation counts %v, want [3 8]", eventCalls)
	}
}

func TestExactlyOneHundredWarmupsPrecedeStabilization(t *testing.T) {
	calls := 0
	stabilizationCalls := -1
	stabilizationPhaseHook = func(operation timedOperation, post postOperation) error {
		stabilizationCalls = calls
		operation()
		return nil
	}
	defer func() { stabilizationPhaseHook = nil }()
	if _, err := collectTimingSamples(func() { calls++ }, nil, 100, 2); err != nil {
		t.Fatal(err)
	}
	if stabilizationCalls != 100 {
		t.Fatalf("stabilization began after %d operations, want 100", stabilizationCalls)
	}
	if calls != 103 {
		t.Fatalf("total operations=%d, want 100 warmup + 1 stabilization + 2 measured", calls)
	}
}

func TestMeasurementBlockDeadlineBoundaries(t *testing.T) {
	start := int64(1_000)
	budget := int64(androidBlockStartBudget)
	before, err := mayStartMeasuredOperation(start+budget-1, start, budget)
	if err != nil || !before {
		t.Fatalf("before deadline: proceed=%v err=%v", before, err)
	}
	at, err := mayStartMeasuredOperation(start+budget, start, budget)
	if err != nil || at {
		t.Fatalf("at deadline: proceed=%v err=%v", at, err)
	}
	if blockExceedsHardMaximum(int64(androidBlockHardMaximum)) {
		t.Fatal("exactly 60 seconds must be valid")
	}
	if !blockExceedsHardMaximum(int64(androidBlockHardMaximum) + 1) {
		t.Fatal("greater than 60 seconds must fail")
	}
}

func TestRunningPrimitiveIsNotInterruptedAtDeadline(t *testing.T) {
	calls := 0
	checks := 0
	measurementContinueHook = func(completed int) (bool, error) {
		checks++
		return checks == 1, nil
	}
	defer func() { measurementContinueHook = nil }()
	samples, err := collectTimingSamples(func() { calls++ }, nil, 0, 3)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || len(samples) != 1 || checks != 2 {
		t.Fatalf("calls=%d samples=%d checks=%d", calls, len(samples), checks)
	}
}

func TestDistributedTargetsCoverSixSecondInterval(t *testing.T) {
	targets, err := distributedTargetOffsets(1000, int64(androidMonitoringTarget))
	if err != nil {
		t.Fatal(err)
	}
	if len(targets) != 1000 || targets[0] != 0 || targets[len(targets)-1] != int64(androidMonitoringTarget) {
		t.Fatalf("unexpected target endpoints: count=%d first=%d last=%d", len(targets), targets[0], targets[len(targets)-1])
	}
	for index := 1; index < len(targets); index++ {
		if targets[index] <= targets[index-1] {
			t.Fatalf("targets are not strictly increasing at %d", index)
		}
	}
}

func TestSingletonRemainderTargetsEndOfMonitoringInterval(t *testing.T) {
	targets, err := distributedTargetOffsets(1, int64(androidMonitoringTarget))
	if err != nil {
		t.Fatal(err)
	}
	if len(targets) != 1 || targets[0] != int64(androidMonitoringTarget) {
		t.Fatalf("singleton targets=%v", targets)
	}
}

func TestSustainedSelectorRunsFastPrimitiveUntimedBetweenTargets(t *testing.T) {
	targets, err := distributedTargetOffsets(3, 6_000)
	if err != nil {
		t.Fatal(err)
	}
	selector := sustainedWorkloadSelector{measuredStartNS: 1_000, startBudgetNS: 59_000, targets: targets}
	times := []int64{1_000, 2_000, 4_000, 7_000}
	wantTimed := []bool{true, false, true, true}
	completed := 0
	for index, now := range times {
		timed, proceed, err := selector.decide(now, completed, index+1)
		if err != nil || !proceed || timed != wantTimed[index] {
			t.Fatalf("index=%d timed=%v proceed=%v err=%v", index, timed, proceed, err)
		}
		if timed {
			completed++
		}
	}
	if completed != 3 || selector.operationCount != 4 || len(selector.selections) != 3 {
		t.Fatalf("completed=%d operations=%d selections=%d", completed, selector.operationCount, len(selector.selections))
	}
}

func TestSustainedSelectorSlowPrimitiveTimesConsecutiveOperations(t *testing.T) {
	targets, err := distributedTargetOffsets(3, 6_000)
	if err != nil {
		t.Fatal(err)
	}
	selector := sustainedWorkloadSelector{measuredStartNS: 1_000, startBudgetNS: 59_000, targets: targets}
	completed := 0
	for index, now := range []int64{1_000, 5_000, 9_000} {
		timed, proceed, err := selector.decide(now, completed, index+1)
		if err != nil || !proceed || !timed {
			t.Fatalf("index=%d timed=%v proceed=%v err=%v", index, timed, proceed, err)
		}
		completed++
	}
}

func TestSelectionHookPreservesPrimitiveTimerPopulation(t *testing.T) {
	calls := 0
	decisions := []bool{true, false, false, true}
	measurementSelectionHook = func(completedTimed, operationOrdinal int) (bool, bool, error) {
		return decisions[operationOrdinal-1], true, nil
	}
	defer func() { measurementSelectionHook = nil }()
	samples, err := collectTimingSamples(func() { calls++ }, nil, 0, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) != 2 || calls != 4 {
		t.Fatalf("timings=%d total operations=%d", len(samples), calls)
	}
}

func TestCurrentAllowedCPUs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status")
	content := "Name:\te0bench\nCpus_allowed:\t00000080\nCpus_allowed_list:\t7\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := currentAllowedCPUs(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != "7" {
		t.Fatalf("got %q, want 7", got)
	}
}

func TestWriteTimingSamplesExclusiveRefusesCollision(t *testing.T) {
	path := filepath.Join(t.TempDir(), "samples.csv")
	if err := writeTimingSamplesExclusive(path, []float64{1.0, 2.0}); err != nil {
		t.Fatal(err)
	}
	original, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := writeTimingSamplesExclusive(path, []float64{3.0}); err == nil {
		t.Fatal("expected collision refusal")
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(after) != string(original) {
		t.Fatal("collision changed existing output")
	}
}

func TestMonotonicNanoseconds(t *testing.T) {
	first, err := monotonicNanoseconds()
	if err != nil {
		t.Fatal(err)
	}
	second, err := monotonicNanoseconds()
	if err != nil {
		t.Fatal(err)
	}
	if first <= 0 || second < first {
		t.Fatalf("invalid monotonic values %d, %d", first, second)
	}
}
