package e7

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type fakeClock struct{ now time.Time }

func (clock *fakeClock) Now() time.Time {
	current := clock.now
	clock.now = clock.now.Add(time.Millisecond)
	return current
}

type recordingExecutor struct {
	prepared bool
	calls    int
	config   Configuration
	failAt   int
}

func (e *recordingExecutor) Configuration() Configuration     { return e.config }
func (e *recordingExecutor) PrepareRun(context.Context) error { e.prepared = true; return nil }
func (e *recordingExecutor) Settle(_ context.Context, sample TradeSample) (CompletionEvidence, error) {
	if !e.prepared {
		panic("Settle before PrepareRun")
	}
	e.calls++
	if e.failAt > 0 && e.calls == e.failAt {
		return CompletionEvidence{RequestID: sample.LogicalTradeID}, errors.New("injected settlement failure")
	}
	return CompletionEvidence{RequestID: sample.LogicalTradeID, CompletionStatus: "ok"}, nil
}

func TestRunnerStopsAndRetainsFirstFailure(t *testing.T) {
	samples, err := LoadFrozenSamples(frozenTracePath(t))
	if err != nil {
		t.Fatal(err)
	}
	executor := &recordingExecutor{config: ConfigurationClassicalL2, failAt: 3}
	clock := &fakeClock{now: time.Unix(0, 0)}
	records, err := newWorkloadRunnerWithClock(clock).Run(context.Background(), executor, samples, false)
	if err == nil {
		t.Fatal("injected executor failure was ignored")
	}
	if executor.calls != 3 || len(records) != 3 {
		t.Fatalf("calls=%d records=%d", executor.calls, len(records))
	}
	failed := records[2]
	if failed.Success || failed.Error != "injected settlement failure" {
		t.Fatalf("failure row=%+v", failed)
	}
	runDirectory := t.TempDir()
	if _, err := PersistSamples(runDirectory, records); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(filepath.Join(runDirectory, "samples.csv"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(content, []byte("injected settlement failure")) {
		t.Fatal("failure row was not persisted")
	}
}
func (e *recordingExecutor) Close() error { return nil }

func TestRunnerUsesOneSequentialSampleLoop(t *testing.T) {
	samples, err := LoadFrozenSamples(frozenTracePath(t))
	if err != nil {
		t.Fatal(err)
	}
	executor := &recordingExecutor{config: ConfigurationClassicalL2}
	clock := &fakeClock{now: time.Unix(0, 0)}
	records, err := newWorkloadRunnerWithClock(clock).Run(context.Background(), executor, samples, false)
	if err != nil {
		t.Fatal(err)
	}
	if executor.calls != TraceRowCount || len(records) != TraceRowCount {
		t.Fatal("wrong invocation count")
	}
	if records[100].SampleIndex != 0 || records[1099].SampleIndex != 999 {
		t.Fatal("sample index mapping")
	}
	if records[0].LatencyMS != 1 {
		t.Fatalf("latency=%v", records[0].LatencyMS)
	}
}
