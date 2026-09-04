package main

import (
	"fmt"
	"os"
	"path/filepath"
)

const (
	warmupIterations = 100
	serverIterations = 10000
	meterIterations  = 1000
)

var platformIterations = map[string]int{
	"server": serverIterations,
	"meter":  meterIterations,
}

var schemeOperations = map[string]map[string]bool{
	"ML-KEM-768": {
		"keygen": true,
		"encaps": true,
		"decaps": true,
	},

	"ML-DSA-44": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},

	"ML-DSA-65": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},

	"ML-DSA-87": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},

	"Falcon-512": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},

	"SPHINCS+-SHA2-128s": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},

	"ECDSA-P-256": {
		"keygen": true,
		"sign":   true,
		"verify": true,
	},
}

func measuredIterations(platform string) int {
	return platformIterations[platform]
}

func isValidSchemeOperation(scheme, operation string) bool {
	operations, ok := schemeOperations[scheme]
	if !ok {
		return false
	}

	return operations[operation]
}

func runBenchmark(scheme string, operation string, warmup int, nIter int) ([]float64, error) {
	switch scheme {
	case "ML-KEM-768":
		switch operation {
		case "keygen":
			return benchmarkOQSKEMKeygen(scheme, warmup, nIter)
		case "encaps":
			return benchmarkOQSKEMEncaps(scheme, warmup, nIter)
		case "decaps":
			return benchmarkOQSKEMDecaps(scheme, warmup, nIter)
		}

	case "ECDSA-P-256":
		switch operation {
		case "keygen":
			return benchmarkOpenSSLECDSAP256Keygen(warmup, nIter)
		case "sign":
			return benchmarkOpenSSLECDSAP256Sign(warmup, nIter)
		case "verify":
			return benchmarkOpenSSLECDSAP256Verify(warmup, nIter)
		}

	default:
		switch operation {
		case "keygen":
			return benchmarkOQSSignatureKeygen(scheme, warmup, nIter)
		case "sign":
			return benchmarkOQSSignatureSign(scheme, warmup, nIter)
		case "verify":
			return benchmarkOQSSignatureVerify(scheme, warmup, nIter)
		}
	}

	return nil, fmt.Errorf("unsupported benchmark combination: scheme=%s operation=%s", scheme, operation)
}

func findProjectRoot() (string, error) {
	currentDir, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("failed to get current working directory: %w", err)
	}

	dir := currentDir

	for {
		metaPath := filepath.Join(dir, "meta.json")
		e0ModulePath := filepath.Join(dir, "src", "e0", "go.mod")

		if _, err := os.Stat(metaPath); err == nil {
			if _, err := os.Stat(e0ModulePath); err == nil {
				return dir, nil
			}
		}

		parent := filepath.Dir(dir)

		if parent == dir {
			break
		}

		dir = parent
	}

	return "", fmt.Errorf("project root not found from working directory %s", currentDir)
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintf(os.Stderr, "usage: %s <platform> <scheme> <operation>\n", os.Args[0])
		os.Exit(2)
	}

	platform := os.Args[1]
	scheme := os.Args[2]
	operation := os.Args[3]

	nIter, validPlatform := platformIterations[platform]
	if !validPlatform {
		fmt.Fprintf(os.Stderr, "invalid platform: %s\n", platform)
		os.Exit(2)
	}

	_, validScheme := schemeOperations[scheme]
	if !validScheme {
		fmt.Fprintf(os.Stderr, "invalid scheme: %s\n", scheme)
		os.Exit(2)
	}

	if !isValidSchemeOperation(scheme, operation) {
		fmt.Fprintf(os.Stderr, "operation %s is not valid for scheme %s\n", operation, scheme)
		os.Exit(2)
	}

	projectRoot, err := findProjectRoot()
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to resolve project root: %v\n", err)
		os.Exit(1)
	}

	rawSamplesBaseDir := filepath.Join(projectRoot, "raw", "e0")
	summaryPath := filepath.Join(projectRoot, "data", "e0_primitives.csv")

	samples, err := runBenchmark(scheme, operation, warmupIterations, nIter)
	if err != nil {
		fmt.Fprintf(os.Stderr, "benchmark failed: %v\n", err)
		os.Exit(1)
	}

	rawPath, err := writeRawTimingSamples(rawSamplesBaseDir, platform, scheme, operation, samples)
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to write raw samples: %v\n", err)
		os.Exit(1)
	}

	stats, err := calculateTimingStats(samples)
	if err != nil {
		fmt.Fprintf(os.Stderr, "statistics calculation failed: %v\n", err)
		os.Exit(1)
	}

	result := e0Result{
		Platform:  platform,
		Scheme:    scheme,
		Operation: operation,
		NIter:     len(samples),
		Stats:     stats,
	}

	if err := writeE0Summary(summaryPath, result); err != nil {
		fmt.Fprintf(os.Stderr, "failed to write E0 summary: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf(
		"DONE: platform=%s scheme=%s operation=%s n_iter=%d median_ms=%.6f p95_ms=%.6f p99_ms=%.6f stddev_ms=%.6f\nraw=%s\nsummary=%s\n",
		platform,
		scheme,
		operation,
		len(samples),
		stats.MedianMs,
		stats.P95Ms,
		stats.P99Ms,
		stats.StddevMs,
		rawPath,
		summaryPath,
	)
}
