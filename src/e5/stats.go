package e5

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

type ScanSummary struct {
	MedianMS float64
	P95MS    float64
	P99MS    float64
}

// Percentile uses the project-wide linear interpolation rank p*(n-1).
func Percentile(values []float64, probability float64) (float64, error) {
	if len(values) == 0 {
		return 0, errors.New("percentile requires at least one sample")
	}
	if probability < 0 || probability > 1 || math.IsNaN(probability) {
		return 0, errors.New("percentile probability must be in [0,1]")
	}
	ordered := append([]float64(nil), values...)
	for _, value := range ordered {
		if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
			return 0, errors.New("percentile samples must be finite and non-negative")
		}
	}
	sort.Float64s(ordered)
	rank := probability * float64(len(ordered)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))
	if lower == upper {
		return ordered[lower], nil
	}
	return ordered[lower] + (rank-float64(lower))*(ordered[upper]-ordered[lower]), nil
}

// SummarizeMeasuredScans excludes warm-up rows and fails closed for scientific
// evidence that is incomplete, duplicated, failed, or non-scientific.
func SummarizeMeasuredScans(samples []Sample, expected int, scientific bool) (ScanSummary, error) {
	if expected <= 0 {
		return ScanSummary{}, errors.New("expected measured iterations must be positive")
	}
	if scientific && expected < MinimumScientificIterations {
		return ScanSummary{}, fmt.Errorf("scientific E5 requires at least %d measured iterations", MinimumScientificIterations)
	}
	iterations := make(map[int]bool)
	values := make([]float64, 0, expected)
	for _, sample := range samples {
		if sample.Phase != "measured" {
			continue
		}
		if sample.Iteration < 0 || sample.Iteration >= expected || iterations[sample.Iteration] {
			return ScanSummary{}, errors.New("measured iterations are out of range or duplicated")
		}
		iterations[sample.Iteration] = true
		if scientific && !sample.Scientific {
			return ScanSummary{}, errors.New("scientific scan summary contains non-scientific provenance")
		}
		if !sample.Success || sample.Error != "" {
			return ScanSummary{}, errors.New("scan summary contains a failed sample")
		}
		values = append(values, sample.ScanCPUMS)
	}
	if len(values) != expected {
		return ScanSummary{}, errors.New("measured scan iterations are incomplete")
	}
	median, err := Percentile(values, 0.5)
	if err != nil {
		return ScanSummary{}, err
	}
	p95, err := Percentile(values, 0.95)
	if err != nil {
		return ScanSummary{}, err
	}
	p99, err := Percentile(values, 0.99)
	if err != nil {
		return ScanSummary{}, err
	}
	return ScanSummary{MedianMS: median, P95MS: p95, P99MS: p99}, nil
}
