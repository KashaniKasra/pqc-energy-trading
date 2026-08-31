package main

import (
	"fmt"
	"time"
)

type timedOperation func()

type postOperation func() error

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

	samples := make([]float64, nIter)

	for i := 0; i < nIter; i++ {
		start := time.Now()

		operation()

		elapsed := time.Since(start)

		if post != nil {
			if err := post(); err != nil {
				return nil, fmt.Errorf("measured iteration %d failed: %w", i, err)
			}
		}

		samples[i] = float64(elapsed.Nanoseconds()) / 1e6
	}

	return samples, nil
}
