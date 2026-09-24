package e2

import (
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
)

const MinimumScientificIterations = 1000

var finalTransitionOrder = []TransitionType{
	TransitionChannelSetup,
	TransitionCommitmentUpdate,
	TransitionHTLCAdd,
	TransitionHTLCSettle,
	TransitionFunding,
	TransitionCoopClose,
	TransitionForceClose,
	TransitionPenalty,
}

// MeasurementScenario contains one measured instance of each required E2
// transition. Cooperative close and stale force-close use independent channels.
type MeasurementScenario struct {
	Transitions             []Transition
	CooperativeChannelClose CloseType
	PenaltyChannelClose     CloseType
}

// BuildMeasurementScenario constructs transitions through valid state-machine
// operations. In particular, the penalty is never synthesized independently.
func BuildMeasurementScenario(configuration Configuration) (MeasurementScenario, error) {
	const (
		partyA PartyID = "alice"
		partyB PartyID = "bob"
	)

	cooperative, err := NewChannel(configuration, partyA, partyB)
	if err != nil {
		return MeasurementScenario{}, err
	}
	setup, err := cooperative.Setup(partyA)
	if err != nil {
		return MeasurementScenario{}, err
	}
	funding, err := cooperative.Fund(partyA)
	if err != nil {
		return MeasurementScenario{}, err
	}
	update, err := cooperative.UpdateCommitment(partyA)
	if err != nil {
		return MeasurementScenario{}, err
	}
	htlcAdd, err := cooperative.AddHTLC(partyA, "h1")
	if err != nil {
		return MeasurementScenario{}, err
	}
	htlcSettle, err := cooperative.SettleHTLC(partyB, "h1")
	if err != nil {
		return MeasurementScenario{}, err
	}
	coopClose, err := cooperative.CooperativeClose(partyB)
	if err != nil {
		return MeasurementScenario{}, err
	}

	punishment, err := NewChannel(configuration, partyA, partyB)
	if err != nil {
		return MeasurementScenario{}, err
	}
	if _, err = punishment.Setup(partyA); err != nil {
		return MeasurementScenario{}, err
	}
	if _, err = punishment.Fund(partyA); err != nil {
		return MeasurementScenario{}, err
	}
	if _, err = punishment.UpdateCommitment(partyA); err != nil { // state 1 revokes state 0
		return MeasurementScenario{}, err
	}
	if _, err = punishment.UpdateCommitment(partyB); err != nil { // state 2 revokes state 1
		return MeasurementScenario{}, err
	}
	forceClose, err := punishment.ForceClose(partyA, 1)
	if err != nil {
		return MeasurementScenario{}, err
	}
	penalty, err := punishment.Penalty(partyB)
	if err != nil {
		return MeasurementScenario{}, err
	}

	return MeasurementScenario{
		Transitions: []Transition{
			setup,
			funding,
			update,
			htlcAdd,
			htlcSettle,
			coopClose,
			forceClose,
			penalty,
		},
		CooperativeChannelClose: cooperative.CloseType(),
		PenaltyChannelClose:     punishment.CloseType(),
	}, nil
}

// TransitionExecutor supplies the RTT for a future real transition execution.
// E2 does not emulate network delay or prescribe a transport here.
type TransitionExecutor interface {
	Execute(authorized AuthorizedTransition) (rttMS float64, err error)
}

type RunOptions struct {
	WarmupIterations   int
	MeasuredIterations int
	Scientific         bool
}

func ValidateRunOptions(options RunOptions, serializer Serializer) error {
	if options.WarmupIterations < 0 {
		return errors.New("warmup iterations cannot be negative")
	}
	if options.MeasuredIterations <= 0 {
		return errors.New("measured iterations must be positive")
	}
	if serializer == nil {
		return errors.New("serializer is required")
	}
	if options.Scientific {
		if options.MeasuredIterations < MinimumScientificIterations {
			return fmt.Errorf("scientific runs require at least %d measured iterations", MinimumScientificIterations)
		}
		if !serializer.Scientific() {
			return ErrNonScientificSerializer
		}
	}
	return nil
}

// RunMeasurements collects warm-up and measured records without discarding
// failures. Scientific runs obtain message_bytes only through
// ScientificMessageBytes.
func RunMeasurements(
	configuration Configuration,
	backend CryptoBackend,
	serializer Serializer,
	executor TransitionExecutor,
	options RunOptions,
) ([]RawMeasurementRecord, error) {
	if err := ValidateRunOptions(options, serializer); err != nil {
		return nil, err
	}
	if backend == nil || executor == nil {
		return nil, errors.New("crypto backend and transition executor are required")
	}
	if backend.Configuration() != configuration {
		return nil, errors.New("crypto backend configuration mismatch")
	}

	total := (options.WarmupIterations + options.MeasuredIterations) * len(finalTransitionOrder)
	records := make([]RawMeasurementRecord, 0, total)
	for _, phase := range []struct {
		name       SamplePhase
		iterations int
	}{
		{SampleWarmup, options.WarmupIterations},
		{SampleMeasured, options.MeasuredIterations},
	} {
		for iteration := 0; iteration < phase.iterations; iteration++ {
			scenario, err := BuildMeasurementScenario(configuration)
			if err != nil {
				return nil, fmt.Errorf("build %s iteration %d: %w", phase.name, iteration, err)
			}
			for _, transition := range scenario.Transitions {
				record := measureTransition(
					configuration,
					transition,
					uint64(iteration),
					phase.name,
					backend,
					serializer,
					executor,
					options.Scientific,
				)
				records = append(records, record)
			}
		}
	}
	return records, nil
}

func measureTransition(
	configuration Configuration,
	transition Transition,
	iteration uint64,
	phase SamplePhase,
	backend CryptoBackend,
	serializer Serializer,
	executor TransitionExecutor,
	scientific bool,
) RawMeasurementRecord {
	record := RawMeasurementRecord{
		Config:     configuration,
		Transition: transition.Type,
		Iteration:  iteration,
		Phase:      phase,
		Scientific: scientific,
	}
	authorized, err := AuthorizeTransition(serializer, backend, transition)
	if err != nil {
		record.ErrorMessage = err.Error()
		return record
	}
	if scientific {
		record.MessageBytes, err = ScientificMessageBytes(serializer, authorized)
	} else {
		var serialized []byte
		serialized, err = serializer.Serialize(authorized)
		record.MessageBytes = len(serialized)
	}
	if err != nil {
		record.ErrorMessage = err.Error()
		return record
	}
	record.RTTMS, err = executor.Execute(authorized)
	if err != nil {
		record.ErrorMessage = err.Error()
		return record
	}
	if !finite(record.RTTMS) || record.RTTMS < 0 {
		record.ErrorMessage = "executor returned invalid RTT"
		return record
	}
	record.Success = true
	return record
}

type TimingStatistics struct {
	MedianMS float64
	P95MS    float64
	P99MS    float64
}

func CalculateTimingStatistics(values []float64) (TimingStatistics, error) {
	if len(values) == 0 {
		return TimingStatistics{}, errors.New("no timing samples")
	}
	sorted := append([]float64(nil), values...)
	for _, value := range sorted {
		if !finite(value) {
			return TimingStatistics{}, errors.New("timing samples must be finite")
		}
	}
	sort.Float64s(sorted)
	return TimingStatistics{
		MedianMS: interpolatedPercentile(sorted, 0.50),
		P95MS:    interpolatedPercentile(sorted, 0.95),
		P99MS:    interpolatedPercentile(sorted, 0.99),
	}, nil
}

func interpolatedPercentile(sorted []float64, probability float64) float64 {
	rank := probability * float64(len(sorted)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))
	if lower == upper {
		return sorted[lower]
	}
	weight := rank - float64(lower)
	return sorted[lower] + weight*(sorted[upper]-sorted[lower])
}

func finite(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0)
}

type SummaryRow struct {
	Config       Configuration
	Transition   TransitionType
	Scientific   bool
	MessageBytes int
	RTTMedianMS  float64
	RTTP95MS     float64
	RTTP99MS     float64
}

type summaryKey struct {
	configuration Configuration
	transition    TransitionType
}

func SummarizeMeasurements(records []RawMeasurementRecord, expectedMeasuredIterations int) ([]SummaryRow, error) {
	type samples struct {
		messageBytes *int
		timings      []float64
		iterations   map[uint64]struct{}
	}
	if expectedMeasuredIterations < 0 {
		return nil, errors.New("expected measured iterations cannot be negative")
	}
	if len(records) == 0 {
		return nil, errors.New("no measurement records")
	}
	scientific := records[0].Scientific
	for _, record := range records[1:] {
		if record.Scientific != scientific {
			return nil, errors.New("mixed scientific and non-scientific measurement records")
		}
	}
	if scientific && expectedMeasuredIterations < MinimumScientificIterations {
		return nil, fmt.Errorf("scientific summaries require at least %d expected measured iterations", MinimumScientificIterations)
	}
	groups := make(map[summaryKey]*samples)
	for _, record := range records {
		if record.Phase != SampleMeasured {
			continue
		}
		if !record.Config.valid() || !validTransition(record.Transition) {
			return nil, errors.New("measured sample has unknown config or transition")
		}
		key := summaryKey{record.Config, record.Transition}
		group := groups[key]
		if group == nil {
			group = &samples{iterations: make(map[uint64]struct{})}
			groups[key] = group
		}
		if scientific {
			if record.Iteration >= uint64(expectedMeasuredIterations) {
				return nil, fmt.Errorf("scientific iteration %d out of range for %s/%s", record.Iteration, record.Config, record.Transition)
			}
			if _, duplicate := group.iterations[record.Iteration]; duplicate {
				return nil, fmt.Errorf("duplicate scientific iteration %d for %s/%s", record.Iteration, record.Config, record.Transition)
			}
			group.iterations[record.Iteration] = struct{}{}
			if !record.Success {
				return nil, fmt.Errorf("failed scientific iteration %d for %s/%s: %s", record.Iteration, record.Config, record.Transition, record.ErrorMessage)
			}
		}
		if !record.Success {
			continue
		}
		// Fixed message size is the current registered assumption. It must be
		// revisited when the professor-approved serializer and crypto backend are
		// known; variable sizes must never be silently averaged.
		if group.messageBytes == nil {
			value := record.MessageBytes
			group.messageBytes = &value
		} else if *group.messageBytes != record.MessageBytes {
			return nil, fmt.Errorf("inconsistent message_bytes for %s/%s", record.Config, record.Transition)
		}
		group.timings = append(group.timings, record.RTTMS)
	}
	if len(groups) == 0 {
		return nil, errors.New("no successful measured samples")
	}
	rows := make([]SummaryRow, 0, len(groups))
	for key, group := range groups {
		if scientific && len(group.iterations) != expectedMeasuredIterations {
			return nil, fmt.Errorf(
				"scientific iteration count for %s/%s = %d, want %d",
				key.configuration,
				key.transition,
				len(group.iterations),
				expectedMeasuredIterations,
			)
		}
		statistics, err := CalculateTimingStatistics(group.timings)
		if err != nil {
			return nil, fmt.Errorf("summarize %s/%s: %w", key.configuration, key.transition, err)
		}
		rows = append(rows, SummaryRow{
			Config:       key.configuration,
			Transition:   key.transition,
			Scientific:   scientific,
			MessageBytes: *group.messageBytes,
			RTTMedianMS:  statistics.MedianMS,
			RTTP95MS:     statistics.P95MS,
			RTTP99MS:     statistics.P99MS,
		})
	}
	return rows, nil
}

func ValidateCompleteSummaries(rows []SummaryRow) error {
	if len(rows) != len(configurations)*len(finalTransitionOrder) {
		return fmt.Errorf("final E2 dataset must contain exactly %d rows", len(configurations)*len(finalTransitionOrder))
	}
	seen := make(map[summaryKey]struct{}, len(rows))
	for _, row := range rows {
		if !row.Scientific {
			return errors.New("final E2 dataset contains non-scientific summary data")
		}
		if !row.Config.valid() || !validTransition(row.Transition) {
			return errors.New("final E2 dataset contains unknown config or transition")
		}
		key := summaryKey{row.Config, row.Transition}
		if _, exists := seen[key]; exists {
			return fmt.Errorf("duplicate final E2 row for %s/%s", row.Config, row.Transition)
		}
		seen[key] = struct{}{}
	}
	for _, configuration := range configurations {
		for _, transition := range finalTransitionOrder {
			if _, exists := seen[summaryKey{configuration, transition}]; !exists {
				return fmt.Errorf("missing final E2 row for %s/%s", configuration, transition)
			}
		}
	}
	return nil
}

func validTransition(value TransitionType) bool {
	for _, transition := range finalTransitionOrder {
		if value == transition {
			return true
		}
	}
	return false
}

func WriteFinalCSV(writer io.Writer, rows []SummaryRow) error {
	if err := ValidateCompleteSummaries(rows); err != nil {
		return err
	}
	byKey := make(map[summaryKey]SummaryRow, len(rows))
	for _, row := range rows {
		byKey[summaryKey{row.Config, row.Transition}] = row
	}
	csvWriter := csv.NewWriter(writer)
	if err := csvWriter.Write(FinalCSVFields); err != nil {
		return err
	}
	for _, configuration := range configurations {
		for _, transition := range finalTransitionOrder {
			row := byKey[summaryKey{configuration, transition}]
			if err := csvWriter.Write([]string{
				string(row.Config),
				string(row.Transition),
				strconv.Itoa(row.MessageBytes),
				formatFloat(row.RTTMedianMS),
				formatFloat(row.RTTP95MS),
			}); err != nil {
				return err
			}
		}
	}
	csvWriter.Flush()
	return csvWriter.Error()
}

func WriteRawCSV(writer io.Writer, records []RawMeasurementRecord) error {
	csvWriter := csv.NewWriter(writer)
	if err := csvWriter.Write(RawMeasurementFields); err != nil {
		return err
	}
	for _, record := range records {
		if err := csvWriter.Write([]string{
			string(record.Config),
			string(record.Transition),
			strconv.FormatUint(record.Iteration, 10),
			string(record.Phase),
			strconv.FormatBool(record.Scientific),
			strconv.Itoa(record.MessageBytes),
			formatFloat(record.RTTMS),
			strconv.FormatBool(record.Success),
			record.ErrorMessage,
		}); err != nil {
			return err
		}
	}
	csvWriter.Flush()
	return csvWriter.Error()
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', 6, 64)
}
