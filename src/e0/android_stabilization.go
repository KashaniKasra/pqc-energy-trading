package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"time"
)

const (
	stabilizationSchema          = "pqc-energy-trading.e0-android-stabilization.v1"
	minimumStabilizationDuration = 3 * time.Second
	defaultStabilizationTimeout  = 30 * time.Second
	qualifyingFrequencySamples   = 5
)

type stabilizationObservation struct {
	ScheduledMonotonicNS int64 `json:"scheduled_monotonic_ns"`
	ActualMonotonicNS    int64 `json:"actual_monotonic_ns"`
	FrequencyKHz         int64 `json:"freq_khz"`
}

type stabilizationMessage struct {
	Schema               string                     `json:"schema"`
	Type                 string                     `json:"type"`
	Token                string                     `json:"token"`
	StartMonotonicNS     int64                      `json:"start_monotonic_ns"`
	QualifiedMonotonicNS int64                      `json:"qualified_monotonic_ns,omitempty"`
	WorkDurationNS       int64                      `json:"work_duration_ns,omitempty"`
	MinimumKHz           int64                      `json:"minimum_khz,omitempty"`
	MaximumKHz           int64                      `json:"maximum_khz,omitempty"`
	Observations         []stabilizationObservation `json:"observations,omitempty"`
	Error                string                     `json:"error,omitempty"`
}

func validateStabilizationReady(message stabilizationMessage, token string, startNS, receivedNS int64) error {
	if message.Schema != stabilizationSchema || message.Type != "ready" || message.Token != token {
		return fmt.Errorf("invalid stabilization readiness identity")
	}
	if receivedNS < startNS || message.StartMonotonicNS != startNS ||
		message.QualifiedMonotonicNS < startNS || message.QualifiedMonotonicNS > receivedNS {
		return fmt.Errorf("stale or invalid stabilization timestamps")
	}
	if message.WorkDurationNS < int64(minimumStabilizationDuration) ||
		message.QualifiedMonotonicNS-message.StartMonotonicNS != message.WorkDurationNS {
		return fmt.Errorf("stabilization work duration is below three seconds or inconsistent")
	}
	if len(message.Observations) != qualifyingFrequencySamples {
		return fmt.Errorf("stabilization readiness requires exactly five observations")
	}
	minimum, maximum := int64(0), int64(0)
	previousScheduled := int64(0)
	for index, observation := range message.Observations {
		if observation.ScheduledMonotonicNS < startNS ||
			observation.ActualMonotonicNS < observation.ScheduledMonotonicNS ||
			observation.ActualMonotonicNS > message.QualifiedMonotonicNS ||
			observation.FrequencyKHz <= 0 {
			return fmt.Errorf("invalid stabilization observation %d", index)
		}
		if index > 0 && observation.ScheduledMonotonicNS-previousScheduled != 100_000_000 {
			return fmt.Errorf("stabilization observations are not consecutive 100 ms slots")
		}
		previousScheduled = observation.ScheduledMonotonicNS
		if index == 0 || observation.FrequencyKHz < minimum {
			minimum = observation.FrequencyKHz
		}
		if index == 0 || observation.FrequencyKHz > maximum {
			maximum = observation.FrequencyKHz
		}
	}
	if message.Observations[len(message.Observations)-1].ActualMonotonicNS != message.QualifiedMonotonicNS {
		return fmt.Errorf("qualifying timestamp does not match the final observation")
	}
	if minimum != message.MinimumKHz || maximum != message.MaximumKHz {
		return fmt.Errorf("stabilization min/max do not match observations")
	}
	if maximum*100 > minimum*102 {
		return fmt.Errorf("stabilization observations exceed the two-percent range")
	}
	return nil
}

func runStabilization(
	address string,
	token string,
	timeout time.Duration,
	operation timedOperation,
	post postOperation,
	record func(phaseEvent) error,
) error {
	if address == "" || token == "" || timeout < minimumStabilizationDuration {
		return fmt.Errorf("invalid stabilization coordination settings")
	}
	connection, err := net.DialTimeout("tcp", address, 5*time.Second)
	if err != nil {
		return fmt.Errorf("connect to stabilization sampler: %w", err)
	}
	defer connection.Close()

	startNS, err := monotonicNanoseconds()
	if err != nil {
		return err
	}
	start := stabilizationMessage{
		Schema: stabilizationSchema, Type: "start", Token: token, StartMonotonicNS: startNS,
	}
	if err := json.NewEncoder(connection).Encode(start); err != nil {
		return fmt.Errorf("send stabilization start: %w", err)
	}
	if err := record(phaseEvent{Event: "stabilization_start", MonotonicNS: startNS}); err != nil {
		return err
	}

	responses := make(chan stabilizationMessage, 1)
	readErrors := make(chan error, 1)
	go func() {
		var response stabilizationMessage
		err := json.NewDecoder(bufio.NewReader(connection)).Decode(&response)
		if err != nil {
			readErrors <- err
			return
		}
		responses <- response
	}()

	deadline := time.Now().Add(timeout)
	operationCount := 0
	for {
		operation()
		operationCount++
		if post != nil {
			if err := post(); err != nil {
				return fmt.Errorf("stabilization operation %d failed: %w", operationCount, err)
			}
		}
		select {
		case response := <-responses:
			if response.Type == "failed" {
				return fmt.Errorf("sampler stabilization failed: %s", response.Error)
			}
			receivedNS, err := monotonicNanoseconds()
			if err != nil {
				return err
			}
			if err := validateStabilizationReady(response, token, startNS, receivedNS); err != nil {
				return err
			}
			return record(phaseEvent{
				Event: "stabilization_ready", MonotonicNS: response.QualifiedMonotonicNS,
				Stabilization: &response, OperationCount: operationCount,
			})
		case err := <-readErrors:
			return fmt.Errorf("read stabilization readiness: %w", err)
		default:
		}
		if !time.Now().Before(deadline) {
			return fmt.Errorf("stabilization did not qualify within %s", timeout)
		}
	}
}
