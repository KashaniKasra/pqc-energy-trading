package main

import (
	"encoding/csv"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestFrequencyReaderReusesDescriptorWithSeek(t *testing.T) {
	path := filepath.Join(t.TempDir(), "scaling_cur_freq")
	if err := os.WriteFile(path, []byte("2000000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	reader, err := newFrequencyReader(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	got, err := reader.ReadKHz()
	if err != nil {
		t.Fatal(err)
	}
	if got != 2000000 {
		t.Fatalf("got %d", got)
	}
	if err := os.WriteFile(path, []byte("1800000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err = reader.ReadKHz()
	if err != nil {
		t.Fatal(err)
	}
	if got != 1800000 {
		t.Fatalf("second read got %d", got)
	}
}

func TestFrequencyReaderRejectsInvalidValue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "scaling_cur_freq")
	if err := os.WriteFile(path, []byte("0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	reader, err := newFrequencyReader(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if _, err := reader.ReadKHz(); err == nil {
		t.Fatal("expected invalid frequency error")
	}
}

type seekFailFile struct {
	*os.File
}

func (file *seekFailFile) Seek(int64, int) (int64, error) {
	return 0, errors.New("seek unsupported")
}

func TestFrequencyReaderFallsBackToReopenWhenSeekUnsupported(t *testing.T) {
	path := filepath.Join(t.TempDir(), "scaling_cur_freq")
	if err := os.WriteFile(path, []byte("2000000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	openCount := 0
	reader, err := newFrequencyReaderWithOpen(path, func(path string) (seekReadCloser, error) {
		openCount++
		file, err := os.Open(path)
		if err != nil {
			return nil, err
		}
		return &seekFailFile{File: file}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	for index := 0; index < 3; index++ {
		value, err := reader.ReadKHz()
		if err != nil || value != 2000000 {
			t.Fatalf("read %d: value=%d err=%v", index, value, err)
		}
	}
	if openCount != 3 {
		t.Fatalf("opened %d times, want 3", openCount)
	}
}

func TestEncodeSampleRowPreservesCSVError(t *testing.T) {
	encoded := encodeSampleRow(nil, 123, 0, false, "read failed, \"temporary\"\nretry")
	record, err := csv.NewReader(strings.NewReader(string(encoded))).Read()
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"123", "0", "false", "read failed, \"temporary\"\nretry"}
	if len(record) != len(want) {
		t.Fatalf("record=%v", record)
	}
	for index := range want {
		if record[index] != want[index] {
			t.Fatalf("field %d=%q, want %q", index, record[index], want[index])
		}
	}
}

func TestNextAbsoluteDeadlineSkipsMissedIntervals(t *testing.T) {
	if got := nextAbsoluteDeadline(100, 150, 100); got != 200 {
		t.Fatalf("ordinary next deadline=%d", got)
	}
	if got := nextAbsoluteDeadline(100, 350, 100); got != 400 {
		t.Fatalf("missed next deadline=%d", got)
	}
	if got := nextAbsoluteDeadline(100, 200, 100); got != 300 {
		t.Fatalf("exact missed deadline=%d", got)
	}
}

func TestStableFrequencyWindowExactBoundary(t *testing.T) {
	values := []int64{1_000_000, 1_020_000, 1_010_000, 1_000_000, 1_020_000}
	observations := make([]stabilizationObservation, len(values))
	for index, value := range values {
		observations[index] = stabilizationObservation{
			ScheduledMonotonicNS: int64(index) * 100_000_000,
			ActualMonotonicNS:    int64(index)*100_000_000 + 1,
			FrequencyKHz:         value,
		}
	}
	minimum, maximum, stable := stableFrequencyWindow(observations)
	if !stable || minimum != 1_000_000 || maximum != 1_020_000 {
		t.Fatalf("minimum=%d maximum=%d stable=%v", minimum, maximum, stable)
	}
	observations[4].FrequencyKHz++
	if _, _, stable := stableFrequencyWindow(observations); stable {
		t.Fatal("greater-than-two-percent window must be unstable")
	}
}

func TestStabilizationTrackerRequiresFiveConsecutiveSuccessfulSlots(t *testing.T) {
	tracker := stabilizationTracker{
		start:     stabilizationMessage{Token: "token", StartMonotonicNS: 1_000_000_000},
		timeoutNS: int64(30 * time.Second),
	}
	for index := 0; index < 4; index++ {
		response, err := tracker.observe(stabilizationObservation{
			ScheduledMonotonicNS: 3_600_000_000 + int64(index)*100_000_000,
			ActualMonotonicNS:    3_600_000_001 + int64(index)*100_000_000,
			FrequencyKHz:         2_000_000,
		})
		if err != nil || response != nil {
			t.Fatalf("index=%d response=%v err=%v", index, response, err)
		}
	}
	tracker.resetSequence() // models either a failed read or an explicitly missed slot
	if len(tracker.consecutive) != 0 {
		t.Fatal("failed/missed observation did not break the sequence")
	}
	for index := 0; index < 5; index++ {
		response, err := tracker.observe(stabilizationObservation{
			ScheduledMonotonicNS: 4_100_000_000 + int64(index)*100_000_000,
			ActualMonotonicNS:    4_100_000_001 + int64(index)*100_000_000,
			FrequencyKHz:         2_000_000,
		})
		if err != nil {
			t.Fatal(err)
		}
		if index < 4 && response != nil {
			t.Fatal("qualified with fewer than five consecutive samples")
		}
		if index == 4 && (response == nil || response.Type != "ready") {
			t.Fatal("did not qualify after five stable consecutive samples")
		}
	}
}

func TestStabilizationTrackerIgnoresStalePreStartSample(t *testing.T) {
	tracker := stabilizationTracker{
		start:     stabilizationMessage{Token: "token", StartMonotonicNS: 1_000_000_000},
		timeoutNS: int64(30 * time.Second),
	}
	response, err := tracker.observe(stabilizationObservation{
		ScheduledMonotonicNS: 800_000_000,
		ActualMonotonicNS:    900_000_000,
		FrequencyKHz:         2_000_000,
	})
	if err != nil || response != nil || len(tracker.consecutive) != 0 {
		t.Fatalf("response=%v err=%v sequence=%v", response, err, tracker.consecutive)
	}
}

func TestStabilizationTrackerTimesOutFailClosed(t *testing.T) {
	tracker := stabilizationTracker{
		start:     stabilizationMessage{Token: "token", StartMonotonicNS: 1_000_000_000},
		timeoutNS: int64(3 * time.Second),
	}
	response, err := tracker.observe(stabilizationObservation{
		ScheduledMonotonicNS: 4_100_000_000,
		ActualMonotonicNS:    4_100_000_001,
		FrequencyKHz:         2_000_000,
	})
	if err != nil || response == nil || response.Type != "failed" {
		t.Fatalf("response=%v err=%v", response, err)
	}
}

func TestScientificRowPreservesScheduledAndActualEvidence(t *testing.T) {
	encoded := encodeScientificSampleRow(nil, 100, 103, 2_000_000, true, false, "", "stabilization")
	record, err := csv.NewReader(strings.NewReader(string(encoded))).Read()
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"100", "103", "2000000", "true", "false", "", "stabilization"}
	if strings.Join(record, "|") != strings.Join(want, "|") {
		t.Fatalf("record=%v", record)
	}
}

func TestStopRequested(t *testing.T) {
	path := filepath.Join(t.TempDir(), "stop")
	if stop, err := stopRequested(path); err != nil || stop {
		t.Fatalf("missing stop: stop=%v err=%v", stop, err)
	}
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if stop, err := stopRequested(path); err != nil || !stop {
		t.Fatalf("present stop: stop=%v err=%v", stop, err)
	}
}

var _ io.Seeker = (*seekFailFile)(nil)

func TestCreateExclusiveRefusesCollision(t *testing.T) {
	path := filepath.Join(t.TempDir(), "out")
	file, err := createExclusive(path)
	if err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := createExclusive(path); err == nil {
		t.Fatal("expected collision refusal")
	}
}

func TestCurrentAllowedCPUs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status")
	if err := os.WriteFile(path, []byte("Cpus_allowed_list:\t0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := currentAllowedCPUs(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != "0" {
		t.Fatalf("got %q, want 0", got)
	}
}
