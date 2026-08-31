package main

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

type e0Result struct {
	Platform  string
	Scheme    string
	Operation string
	NIter     int
	Stats     timingStats
}

type rawTimingSample struct {
	Iteration int
	ElapsedMs float64
}

func buildRawTimingSamples(samples []float64) []rawTimingSample {
	rawSamples := make([]rawTimingSample, len(samples))

	for i, sample := range samples {
		rawSamples[i] = rawTimingSample{
			Iteration: i + 1,
			ElapsedMs: sample,
		}
	}

	return rawSamples
}

func writeRawTimingSamples(baseDir string, platform string, scheme string, operation string, samples []float64) (string, error) {
	if len(samples) == 0 {
		return "", fmt.Errorf("no raw timing samples to write")
	}

	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		return "", fmt.Errorf("failed to create raw output directory: %w", err)
	}

	filename := fmt.Sprintf("e0_%s_%s_%s.csv", platform, scheme, operation)

	path := filepath.Join(baseDir, filename)

	file, err := os.Create(path)
	if err != nil {
		return "", fmt.Errorf("failed to create raw timing file %s: %w", path, err)
	}
	defer file.Close()

	writer := csv.NewWriter(file)
	defer writer.Flush()

	if err := writer.Write([]string{
		"iteration",
		"elapsed_ms",
	}); err != nil {
		return "", fmt.Errorf("failed to write raw timing header: %w", err)
	}

	rawSamples := buildRawTimingSamples(samples)

	for _, sample := range rawSamples {
		record := []string{
			strconv.Itoa(sample.Iteration),
			strconv.FormatFloat(sample.ElapsedMs, 'f', 9, 64),
		}

		if err := writer.Write(record); err != nil {
			return "", fmt.Errorf("failed to write raw timing sample %d: %w", sample.Iteration, err)
		}
	}

	writer.Flush()

	if err := writer.Error(); err != nil {
		return "", fmt.Errorf("failed while writing raw timing file: %w", err)
	}

	return path, nil
}

func writeE0Summary(path string, result e0Result) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("failed to create summary output directory: %w", err)
	}

	header := []string{
		"platform",
		"scheme",
		"operation",
		"n_iter",
		"median_ms",
		"p95_ms",
		"p99_ms",
		"stddev_ms",
	}

	newRecord := []string{
		result.Platform,
		result.Scheme,
		result.Operation,
		strconv.Itoa(result.NIter),
		strconv.FormatFloat(result.Stats.MedianMs, 'f', 9, 64),
		strconv.FormatFloat(result.Stats.P95Ms, 'f', 9, 64),
		strconv.FormatFloat(result.Stats.P99Ms, 'f', 9, 64),
		strconv.FormatFloat(result.Stats.StddevMs, 'f', 9, 64),
	}

	records := [][]string{header}
	replaced := false

	file, err := os.Open(path)
	if err == nil {
		reader := csv.NewReader(file)

		existingRecords, readErr := reader.ReadAll()
		file.Close()

		if readErr != nil {
			return fmt.Errorf("failed to read existing E0 summary: %w", readErr)
		}

		for i, record := range existingRecords {
			if i == 0 {
				continue
			}

			if len(record) < 3 {
				return fmt.Errorf("invalid record in existing E0 summary")
			}

			if record[0] == result.Platform && record[1] == result.Scheme && record[2] == result.Operation {
				records = append(records, newRecord)
				replaced = true
				continue
			}

			records = append(records, record)
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("failed to open existing E0 summary: %w", err)
	}

	if !replaced {
		records = append(records, newRecord)
	}

	file, err = os.Create(path)
	if err != nil {
		return fmt.Errorf("failed to create E0 summary file %s: %w", path, err)
	}
	defer file.Close()

	writer := csv.NewWriter(file)

	if err := writer.WriteAll(records); err != nil {
		return fmt.Errorf("failed to write E0 summary: %w", err)
	}

	writer.Flush()

	if err := writer.Error(); err != nil {
		return fmt.Errorf("failed while writing E0 summary: %w", err)
	}

	return nil
}
