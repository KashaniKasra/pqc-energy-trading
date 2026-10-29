package main

import (
	"fmt"
	"time"
)

type timedOperation func()

type postOperation func() error

// timingPhaseHook is intentionally invoked only outside individual primitive
// timers. Android collection uses it to align independent frequency telemetry
// with the measured phase without changing the primitive timing boundary.
var timingPhaseHook func(string) error

// stabilizationPhaseHook runs after the ordinary warm-up loop and before the
// measured_start marker. Android scientific collection uses the already
// initialized primitive closure here, so stabilization adds no work inside an
// individual primitive timer. Other E0 modes leave this nil.
var stabilizationPhaseHook func(timedOperation, postOperation) error

// measurementContinueHook is evaluated before starting each measured
// primitive. Android block collection uses it to stop launching new work at a
// conservative monotonic deadline. It is outside the primitive-only timer and
// is nil for the historical server/meter paths.
var measurementContinueHook func(completed int) (bool, error)

// measurementSelectionHook selects which operations receive the unchanged
// primitive timer. Android sustained-workload collection uses it to run the
// same primitive continuously between predetermined timed operations. The
// hook runs before the primitive timer and is nil for historical modes.
// operationOrdinal is one-based and counts timed plus untimed operations.
var measurementSelectionHook func(completedTimed, operationOrdinal int) (timeOperation, proceed bool, err error)

func collectTimingSamples(operation timedOperation, post postOperation, warmup int, nIter int) ([]float64, error) {
	if warmup < 0 {
		return nil, fmt.Errorf("warmup must be non-negative")
	}

	if nIter <= 0 {
		return nil, fmt.Errorf("nIter must be positive")
	}

	for i := 0; i < warmup; i++ {
		operation()

		if post != nil {
			if err := post(); err != nil {
				return nil, fmt.Errorf("warm-up iteration %d failed: %w", i, err)
			}
		}
	}

	if stabilizationPhaseHook != nil {
		if err := stabilizationPhaseHook(operation, post); err != nil {
			return nil, fmt.Errorf("stabilization phase failed: %w", err)
		}
	}

	samples := make([]float64, 0, nIter)
	totalOperations := 0

	if timingPhaseHook != nil {
		if err := timingPhaseHook("measured_start"); err != nil {
			return nil, fmt.Errorf("failed to record measured-phase start: %w", err)
		}
	}

	for len(samples) < nIter {
		timeOperation := true
		if measurementSelectionHook != nil {
			var proceed bool
			var err error
			timeOperation, proceed, err = measurementSelectionHook(len(samples), totalOperations+1)
			if err != nil {
				return nil, fmt.Errorf("measurement operation selection failed: %w", err)
			}
			if !proceed {
				break
			}
		} else if measurementContinueHook != nil {
			proceed, err := measurementContinueHook(len(samples))
			if err != nil {
				return nil, fmt.Errorf("measurement block deadline check failed: %w", err)
			}
			if !proceed {
				break
			}
		}

		if !timeOperation {
			operation()
			if post != nil {
				if err := post(); err != nil {
					return nil, fmt.Errorf("untimed measured-phase operation %d failed: %w", totalOperations, err)
				}
			}
			totalOperations++
			continue
		}

		start := time.Now()

		operation()

		elapsed := time.Since(start)

		if post != nil {
			if err := post(); err != nil {
				return nil, fmt.Errorf("measured iteration %d failed: %w", len(samples), err)
			}
		}

		samples = append(samples, float64(elapsed.Nanoseconds())/1e6)
		totalOperations++
	}

	if timingPhaseHook != nil {
		if err := timingPhaseHook("measured_end"); err != nil {
			return nil, fmt.Errorf("failed to record measured-phase end: %w", err)
		}
	}

	return samples, nil
}
