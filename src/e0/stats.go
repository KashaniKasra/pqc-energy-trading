package main

import (
	"fmt"
	"math"
	"sort"
)

type timingStats struct {
	MedianMs float64
	P95Ms    float64
	P99Ms    float64
	StddevMs float64
}

func percentile(sortedSamples []float64, p float64) float64 {
	if len(sortedSamples) == 1 {
		return sortedSamples[0]
	}

	rank := p * float64(len(sortedSamples)-1)

	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))

	if lower == upper {
		return sortedSamples[lower]
	}

	weight := rank - float64(lower)

	return sortedSamples[lower] + weight*(sortedSamples[upper]-sortedSamples[lower])
}

func calculateTimingStats(samples []float64) (timingStats, error) {
	if len(samples) < 2 {
		return timingStats{}, fmt.Errorf("at least two timing samples are required")
	}

	sortedSamples := append([]float64(nil), samples...)
	sort.Float64s(sortedSamples)

	var sum float64
	for _, sample := range samples {
		sum += sample
	}

	mean := sum / float64(len(samples))

	var squaredDiffSum float64
	for _, sample := range samples {
		diff := sample - mean
		squaredDiffSum += diff * diff
	}

	stddev := math.Sqrt(squaredDiffSum / float64(len(samples)-1))

	return timingStats{
		MedianMs: percentile(sortedSamples, 0.50),
		P95Ms:    percentile(sortedSamples, 0.95),
		P99Ms:    percentile(sortedSamples, 0.99),
		StddevMs: stddev,
	}, nil
}
