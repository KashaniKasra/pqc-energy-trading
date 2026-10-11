package e7

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// SETSECDSAAlgorithm is the exact identifier retained in SETS raw evidence.
const SETSECDSAAlgorithm = "ECDSA-P256-SHA256"

type Configuration string

const (
	ConfigurationL1Only       Configuration = "l1_only"
	ConfigurationSETSReimpl   Configuration = "sets_reimpl"
	ConfigurationClassicalL2  Configuration = "classical_l2"
	ConfigurationLayerAwareL2 Configuration = "layer_aware_l2"
)

var ConfigurationOrder = []Configuration{
	ConfigurationL1Only,
	ConfigurationSETSReimpl,
	ConfigurationClassicalL2,
	ConfigurationLayerAwareL2,
}

func (configuration Configuration) Valid() bool {
	for _, candidate := range ConfigurationOrder {
		if configuration == candidate {
			return true
		}
	}
	return false
}

type CompletionEvidence struct {
	RequestID                       string
	TransactionID                   string
	CompletionStatus                string
	ChannelID                       string
	HTLCID                          string
	AddAlgorithm                    string
	SettleAlgorithm                 string
	AddSerializedBytes              int
	SettleSerializedBytes           int
	AddSerializedSHA256             string
	SettleSerializedSHA256          string
	AuthorizationVerified           bool
	ReceiverVerified                bool
	Committed                       bool
	Verified                        bool
	ValidationStatus                string
	ValidationCode                  int
	PayloadSHA256                   string
	SETSECDSAAlgorithm              string
	SETSSignedPreimageSHA256        string
	SETSPublicKeySHA256             string
	SETSSignatureSHA256             string
	SETSSignatureVerified           bool
	SETSControllerEligible          bool
	SETSControllerBalanceSufficient bool
}

type SettlementExecutor interface {
	Configuration() Configuration
	PrepareRun(context.Context) error
	Settle(context.Context, TradeSample) (CompletionEvidence, error)
	Close() error
}

type Clock interface {
	Now() time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

type WorkloadRunner struct {
	clock Clock
}

func NewWorkloadRunner() *WorkloadRunner { return &WorkloadRunner{clock: systemClock{}} }

func newWorkloadRunnerWithClock(clock Clock) *WorkloadRunner {
	return &WorkloadRunner{clock: clock}
}

// Run uses one outer monotonic timing boundary for every sample. PrepareRun is
// deliberately completed before the origin and before any sample timer.
func (runner *WorkloadRunner) Run(
	ctx context.Context,
	executor SettlementExecutor,
	samples []TradeSample,
	scientific bool,
) ([]RawSample, error) {
	if executor == nil || runner.clock == nil || !executor.Configuration().Valid() {
		return nil, errors.New("invalid E7 workload runner parameters")
	}
	if len(samples) != TraceRowCount {
		return nil, fmt.Errorf("E7 workload requires %d samples", TraceRowCount)
	}
	if err := executor.PrepareRun(ctx); err != nil {
		return nil, fmt.Errorf("prepare E7 run: %w", err)
	}
	origin := runner.clock.Now()
	records := make([]RawSample, 0, len(samples))
	for _, sample := range samples {
		started := runner.clock.Now()
		completion, err := executor.Settle(ctx, sample)
		finished := runner.clock.Now()
		if finished.Before(started) {
			return records, errors.New("E7 monotonic clock moved backwards")
		}
		record := RawSampleFrom(sample, executor.Configuration(), scientific, completion)
		record.StartMonotonicNS = started.Sub(origin).Nanoseconds()
		record.EndMonotonicNS = finished.Sub(origin).Nanoseconds()
		record.LatencyMS = float64(finished.Sub(started).Nanoseconds()) / 1_000_000.0
		if err != nil {
			record.Success = false
			record.Error = err.Error()
			records = append(records, record)
			return records, fmt.Errorf("settle E7 trace row %d: %w", sample.TraceRowIndex, err)
		}
		record.Success = true
		records = append(records, record)
	}
	return records, nil
}
