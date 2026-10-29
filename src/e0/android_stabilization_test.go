package main

import (
	"encoding/json"
	"net"
	"testing"
	"time"
)

func readyMessage(start int64, values []int64) stabilizationMessage {
	observations := make([]stabilizationObservation, len(values))
	minimum, maximum := values[0], values[0]
	for index, value := range values {
		observations[index] = stabilizationObservation{
			ScheduledMonotonicNS: start + int64(index+26)*100_000_000,
			ActualMonotonicNS:    start + int64(index+26)*100_000_000,
			FrequencyKHz:         value,
		}
		if value < minimum {
			minimum = value
		}
		if value > maximum {
			maximum = value
		}
	}
	qualified := observations[len(observations)-1].ActualMonotonicNS
	return stabilizationMessage{
		Schema: stabilizationSchema, Type: "ready", Token: "token", StartMonotonicNS: start,
		QualifiedMonotonicNS: qualified, WorkDurationNS: qualified - start,
		MinimumKHz: minimum, MaximumKHz: maximum, Observations: observations,
	}
}

func TestValidateStabilizationReadyAtExactDurationAndBoundary(t *testing.T) {
	message := readyMessage(1_000_000_000, []int64{1_000_000, 1_020_000, 1_010_000, 1_000_000, 1_020_000})
	if err := validateStabilizationReady(message, "token", message.StartMonotonicNS, message.QualifiedMonotonicNS); err != nil {
		t.Fatal(err)
	}
}

func TestValidateStabilizationReadyRejectsShortFewerUnstableAndStale(t *testing.T) {
	base := readyMessage(1_000_000_000, []int64{1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000})
	tests := map[string]func(*stabilizationMessage){
		"short": func(value *stabilizationMessage) {
			value.QualifiedMonotonicNS--
			value.WorkDurationNS--
		},
		"fewer": func(value *stabilizationMessage) { value.Observations = value.Observations[:4] },
		"unstable": func(value *stabilizationMessage) {
			value.Observations[4].FrequencyKHz = 1_020_001
			value.MaximumKHz = 1_020_001
		},
		"stale":        func(value *stabilizationMessage) { value.Token = "old-token" },
		"missing-slot": func(value *stabilizationMessage) { value.Observations[3].ScheduledMonotonicNS++ },
		"future":       func(value *stabilizationMessage) {},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			value := base
			value.Observations = append([]stabilizationObservation(nil), base.Observations...)
			mutate(&value)
			receivedNS := value.QualifiedMonotonicNS
			if name == "future" {
				receivedNS--
			}
			if err := validateStabilizationReady(value, "token", base.StartMonotonicNS, receivedNS); err == nil {
				t.Fatal("expected validation failure")
			}
		})
	}
}

func TestRunStabilizationStopsOnContinuousWorkInterruption(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		var start stabilizationMessage
		_ = json.NewDecoder(connection).Decode(&start)
	}()
	operations := 0
	err = runStabilization(
		listener.Addr().String(), "token", 3*time.Second,
		func() { operations++ },
		func() error { return errInterruptedWork },
		func(phaseEvent) error { return nil },
	)
	if err == nil || operations != 1 {
		t.Fatalf("err=%v operations=%d", err, operations)
	}
}

type interruptionError string

func (value interruptionError) Error() string { return string(value) }

const errInterruptedWork = interruptionError("continuous work interrupted")
