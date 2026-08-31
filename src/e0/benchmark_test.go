package main

import (
	"os"
	"path/filepath"
	"testing"
)

func requireValidSamples(t *testing.T, samples []float64, err error, want int) {
	t.Helper()

	if err != nil {
		t.Fatalf("benchmark failed: %v", err)
	}

	if len(samples) != want {
		t.Fatalf("got %d samples, want %d", len(samples), want)
	}

	for i, sample := range samples {
		if sample <= 0 {
			t.Fatalf("sample %d is not positive: %f ms", i, sample)
		}
	}
}

func TestMeasuredIterations(t *testing.T) {
	tests := []struct {
		platform string
		want     int
	}{
		{"server", 10000},
		{"meter", 1000},
		{"invalid", 0},
	}

	for _, tt := range tests {
		t.Run(tt.platform, func(t *testing.T) {
			got := measuredIterations(tt.platform)

			if got != tt.want {
				t.Fatalf("measuredIterations(%q) = %d, want %d", tt.platform, got, tt.want)
			}
		})
	}
}

func TestCollectTimingSamples(t *testing.T) {
	callCount := 0
	postCallCount := 0

	operation := func() {
		callCount++
	}

	postOperation := func() error {
		postCallCount++
		return nil
	}

	warmup := 3
	nIter := 5

	samples, err := collectTimingSamples(operation, postOperation, warmup, nIter)

	if err != nil {
		t.Fatalf("collectTimingSamples failed: %v", err)
	}

	if callCount != warmup+nIter {
		t.Fatalf("operation called %d times, want %d", callCount, warmup+nIter)
	}

	if postCallCount != warmup+nIter {
		t.Fatalf("postOperation called %d times, want %d", postCallCount, warmup+nIter)
	}

	if len(samples) != nIter {
		t.Fatalf("got %d samples, want %d", len(samples), nIter)
	}

	for i, sample := range samples {
		if sample < 0 {
			t.Fatalf("sample %d is negative: %f ms", i, sample)
		}
	}
}

func TestBenchmarkOQSSignatures(t *testing.T) {
	schemes := []string{
		"ML-DSA-44",
		"ML-DSA-65",
		"ML-DSA-87",
		"Falcon-512",
		"SPHINCS+-SHA2-128s",
	}

	const (
		warmup = 2
		nIter  = 5
	)

	for _, scheme := range schemes {
		t.Run(scheme+"/keygen", func(t *testing.T) {
			samples, err := benchmarkOQSSignatureKeygen(scheme, warmup, nIter)

			requireValidSamples(t, samples, err, nIter)
		})

		t.Run(scheme+"/sign", func(t *testing.T) {
			samples, err := benchmarkOQSSignatureSign(scheme, warmup, nIter)

			requireValidSamples(t, samples, err, nIter)
		})

		t.Run(scheme+"/verify", func(t *testing.T) {
			samples, err := benchmarkOQSSignatureVerify(scheme, warmup, nIter)

			requireValidSamples(t, samples, err, nIter)
		})
	}
}

func TestBenchmarkOQSKEM(t *testing.T) {
	const (
		scheme = "ML-KEM-768"
		warmup = 2
		nIter  = 5
	)

	t.Run("keygen", func(t *testing.T) {
		samples, err := benchmarkOQSKEMKeygen(scheme, warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})

	t.Run("encaps", func(t *testing.T) {
		samples, err := benchmarkOQSKEMEncaps(scheme, warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})

	t.Run("decaps", func(t *testing.T) {
		samples, err := benchmarkOQSKEMDecaps(scheme, warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})
}

func TestBenchmarkOpenSSLECDSAP256(t *testing.T) {
	const (
		warmup = 2
		nIter  = 5
	)

	t.Run("keygen", func(t *testing.T) {
		samples, err := benchmarkOpenSSLECDSAP256Keygen(warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})

	t.Run("sign", func(t *testing.T) {
		samples, err := benchmarkOpenSSLECDSAP256Sign(warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})

	t.Run("verify", func(t *testing.T) {
		samples, err := benchmarkOpenSSLECDSAP256Verify(warmup, nIter)

		requireValidSamples(t, samples, err, nIter)
	})
}

func TestRunBenchmark(t *testing.T) {
	const (
		warmup = 1
		nIter  = 2
	)

	tests := []struct {
		name      string
		scheme    string
		operation string
	}{
		{
			name:      "signature",
			scheme:    "ML-DSA-44",
			operation: "sign",
		},
		{
			name:      "kem",
			scheme:    "ML-KEM-768",
			operation: "encaps",
		},
		{
			name:      "ecdsa",
			scheme:    "ECDSA-P-256",
			operation: "verify",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			samples, err := runBenchmark(tt.scheme, tt.operation, warmup, nIter)

			requireValidSamples(t, samples, err, nIter)
		})
	}
}

func TestCalculateTimingStats(t *testing.T) {
	samples := []float64{
		10,
		20,
		30,
		40,
		50,
	}

	stats, err := calculateTimingStats(samples)
	if err != nil {
		t.Fatalf("calculateTimingStats failed: %v", err)
	}

	const tolerance = 1e-9

	checkClose := func(name string, got, want float64) {
		t.Helper()

		diff := got - want
		if diff < 0 {
			diff = -diff
		}

		if diff > tolerance {
			t.Fatalf(
				"%s = %f, want %f",
				name,
				got,
				want,
			)
		}
	}

	checkClose("median", stats.MedianMs, 30.0)
	checkClose("p95", stats.P95Ms, 48.0)
	checkClose("p99", stats.P99Ms, 49.6)
	checkClose("stddev", stats.StddevMs, 15.811388300841896)
}

func TestWriteRawTimingSamples(t *testing.T) {
	tempDir := t.TempDir()

	samples := []float64{
		0.123456789,
		0.119832000,
		0.121004000,
	}

	path, err := writeRawTimingSamples(tempDir, "server", "ML-DSA-44", "sign", samples)
	if err != nil {
		t.Fatalf("writeRawTimingSamples failed: %v", err)
	}

	if path == "" {
		t.Fatal("writeRawTimingSamples returned empty path")
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read raw timing file: %v", err)
	}

	want := "" +
		"iteration,elapsed_ms\n" +
		"1,0.123456789\n" +
		"2,0.119832000\n" +
		"3,0.121004000\n"

	if string(content) != want {
		t.Fatalf("unexpected raw timing file content:\n%s\nwant:\n%s", string(content), want)
	}
}

func TestWriteE0Summary(t *testing.T) {
	tempDir := t.TempDir()
	path := filepath.Join(tempDir, "e0_primitives.csv")

	result := e0Result{
		Platform:  "server",
		Scheme:    "ML-DSA-44",
		Operation: "sign",
		NIter:     10000,
		Stats: timingStats{
			MedianMs: 0.123456789,
			P95Ms:    0.150000000,
			P99Ms:    0.175000000,
			StddevMs: 0.010000000,
		},
	}

	if err := writeE0Summary(path, result); err != nil {
		t.Fatalf("writeE0Summary failed: %v", err)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read E0 summary file: %v", err)
	}

	want := "" +
		"platform,scheme,operation,n_iter,median_ms,p95_ms,p99_ms,stddev_ms\n" +
		"server,ML-DSA-44,sign,10000,0.123456789,0.150000000,0.175000000,0.010000000\n"

	if string(content) != want {
		t.Fatalf("unexpected E0 summary content:\n%s\nwant:\n%s", string(content), want)
	}
}

func TestWriteE0SummaryUpsert(t *testing.T) {
	tempDir := t.TempDir()
	path := filepath.Join(tempDir, "e0_primitives.csv")

	first := e0Result{
		Platform:  "server",
		Scheme:    "ML-DSA-44",
		Operation: "sign",
		NIter:     10000,
		Stats: timingStats{
			MedianMs: 0.100000000,
			P95Ms:    0.120000000,
			P99Ms:    0.140000000,
			StddevMs: 0.010000000,
		},
	}

	replacement := e0Result{
		Platform:  "server",
		Scheme:    "ML-DSA-44",
		Operation: "sign",
		NIter:     10000,
		Stats: timingStats{
			MedianMs: 0.200000000,
			P95Ms:    0.220000000,
			P99Ms:    0.240000000,
			StddevMs: 0.020000000,
		},
	}

	other := e0Result{
		Platform:  "server",
		Scheme:    "ML-DSA-44",
		Operation: "verify",
		NIter:     10000,
		Stats: timingStats{
			MedianMs: 0.300000000,
			P95Ms:    0.320000000,
			P99Ms:    0.340000000,
			StddevMs: 0.030000000,
		},
	}

	if err := writeE0Summary(path, first); err != nil {
		t.Fatalf("first writeE0Summary failed: %v", err)
	}

	if err := writeE0Summary(path, replacement); err != nil {
		t.Fatalf("replacement writeE0Summary failed: %v", err)
	}

	if err := writeE0Summary(path, other); err != nil {
		t.Fatalf("other writeE0Summary failed: %v", err)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("failed to read E0 summary file: %v", err)
	}

	want := "" +
		"platform,scheme,operation,n_iter,median_ms,p95_ms,p99_ms,stddev_ms\n" +
		"server,ML-DSA-44,sign,10000,0.200000000,0.220000000,0.240000000,0.020000000\n" +
		"server,ML-DSA-44,verify,10000,0.300000000,0.320000000,0.340000000,0.030000000\n"

	if string(content) != want {
		t.Fatalf("unexpected E0 summary content:\n%s\nwant:\n%s", string(content), want)
	}
}
