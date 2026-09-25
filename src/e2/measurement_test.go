package e2

import (
	"bytes"
	"encoding/csv"
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"
)

type deterministicExecutor struct {
	fail TransitionType
}

func (deterministicExecutor) Scientific() bool { return false }

func (executor deterministicExecutor) Execute(authorized AuthorizedTransition) (float64, error) {
	if authorized.Transition.Type == executor.fail {
		return 7.5, errors.New("deterministic execution failure")
	}
	for index, transition := range finalTransitionOrder {
		if authorized.Transition.Type == transition {
			return float64(index + 1), nil
		}
	}
	return 0, errors.New("unknown transition")
}

type scientificValidationSerializer struct{ testOnlySerializer }

func (scientificValidationSerializer) Scientific() bool { return true }

func TestScientificIterationGuard(t *testing.T) {
	nonScientific := testOnlySerializer{}
	if err := ValidateRunOptions(RunOptions{MeasuredIterations: 999, Scientific: true}, nonScientific); err == nil {
		t.Fatal("scientific run below 1000 iterations was accepted")
	}
	serializer := scientificValidationSerializer{}
	if err := ValidateRunOptions(RunOptions{MeasuredIterations: 999, Scientific: true}, serializer); err == nil {
		t.Fatal("scientific run below 1000 iterations was accepted with scientific marker")
	}
	if err := ValidateRunOptions(RunOptions{WarmupIterations: 1, MeasuredIterations: 1000, Scientific: true}, serializer); err != nil {
		t.Fatalf("valid scientific options rejected: %v", err)
	}
	if err := ValidateRunOptions(RunOptions{MeasuredIterations: 2}, nonScientific); err != nil {
		t.Fatalf("small non-scientific run rejected: %v", err)
	}
}

func TestMeasurementScenarioCompletenessAndBranches(t *testing.T) {
	scenario, err := BuildMeasurementScenario(ConfigurationLayerAware)
	if err != nil {
		t.Fatal(err)
	}
	if scenario.CooperativeChannelClose != CloseCooperative {
		t.Fatalf("cooperative branch close = %q", scenario.CooperativeChannelClose)
	}
	if scenario.PenaltyChannelClose != ClosePenalty {
		t.Fatalf("penalty branch close = %q", scenario.PenaltyChannelClose)
	}
	seen := make(map[TransitionType]Transition)
	for _, transition := range scenario.Transitions {
		if _, duplicate := seen[transition.Type]; duplicate {
			t.Fatalf("duplicate transition %q", transition.Type)
		}
		seen[transition.Type] = transition
	}
	for _, transition := range finalTransitionOrder {
		if _, exists := seen[transition]; !exists {
			t.Fatalf("missing transition %q", transition)
		}
	}
	penalty := seen[TransitionPenalty]
	forceClose := seen[TransitionForceClose]
	if forceClose.ReferencedState == nil || *forceClose.ReferencedState != 1 {
		t.Fatalf("force close is not stale state 1: %+v", forceClose)
	}
	if penalty.ReferencedState == nil || *penalty.ReferencedState != 1 || penalty.Revocation == nil ||
		penalty.Revocation.RevokedState != 1 || penalty.Revocation.SupersededBy != 2 {
		t.Fatalf("penalty did not derive from revoked stale state: %+v", penalty)
	}
}

func TestWarmupMeasuredLabelsAndIterationNumbering(t *testing.T) {
	records, err := RunMeasurements(
		ConfigurationClassical,
		testBackend{configuration: ConfigurationClassical},
		testOnlySerializer{},
		deterministicExecutor{},
		RunOptions{WarmupIterations: 1, MeasuredIterations: 2},
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 24 {
		t.Fatalf("record count = %d, want 24", len(records))
	}
	for index, record := range records {
		if index < 8 {
			if record.Phase != SampleWarmup || record.Iteration != 0 {
				t.Fatalf("warmup record %d = %+v", index, record)
			}
			continue
		}
		wantIteration := uint64((index - 8) / 8)
		if record.Phase != SampleMeasured || record.Iteration != wantIteration {
			t.Fatalf("measured record %d = %+v, want iteration %d", index, record, wantIteration)
		}
	}
}

func TestFailedSampleIsPreserved(t *testing.T) {
	records, err := RunMeasurements(
		ConfigurationUniformMLDSA,
		testBackend{configuration: ConfigurationUniformMLDSA},
		testOnlySerializer{},
		deterministicExecutor{fail: TransitionPenalty},
		RunOptions{MeasuredIterations: 1},
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 8 {
		t.Fatalf("record count = %d", len(records))
	}
	last := records[len(records)-1]
	if last.Transition != TransitionPenalty || last.Success || last.ErrorMessage != "deterministic execution failure" {
		t.Fatalf("failed sample not preserved: %+v", last)
	}
}

func TestScientificRunRejectsTestSerializer(t *testing.T) {
	_, err := RunMeasurements(
		ConfigurationClassical,
		testBackend{configuration: ConfigurationClassical},
		testOnlySerializer{},
		deterministicExecutor{},
		RunOptions{MeasuredIterations: 1000, Scientific: true},
	)
	if !errors.Is(err, ErrNonScientificSerializer) {
		t.Fatalf("RunMeasurements error = %v", err)
	}
}

func TestTimingStatisticsLinearInterpolation(t *testing.T) {
	tests := []struct {
		name   string
		values []float64
		median float64
		p95    float64
		p99    float64
	}{
		{"single", []float64{7}, 7, 7, 7},
		{"even", []float64{4, 1, 3, 2}, 2.5, 3.85, 3.97},
		{"odd", []float64{5, 1, 4, 2, 3}, 3, 4.8, 4.96},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			original := append([]float64(nil), test.values...)
			statistics, err := CalculateTimingStatistics(test.values)
			if err != nil {
				t.Fatal(err)
			}
			if !closeFloat(statistics.MedianMS, test.median) ||
				!closeFloat(statistics.P95MS, test.p95) || !closeFloat(statistics.P99MS, test.p99) {
				t.Fatalf("statistics = %+v", statistics)
			}
			if !reflect.DeepEqual(test.values, original) {
				t.Fatal("CalculateTimingStatistics mutated input")
			}
		})
	}
	if _, err := CalculateTimingStatistics(nil); err == nil {
		t.Fatal("empty sample set accepted")
	}
	if _, err := CalculateTimingStatistics([]float64{math.NaN()}); err == nil {
		t.Fatal("non-finite sample accepted")
	}
}

func TestMessageByteConsistency(t *testing.T) {
	records := []RawMeasurementRecord{
		{Config: ConfigurationClassical, Transition: TransitionFunding, Phase: SampleMeasured, MessageBytes: 100, RTTMS: 1, Success: true},
		{Config: ConfigurationClassical, Transition: TransitionFunding, Phase: SampleMeasured, MessageBytes: 100, RTTMS: 2, Success: true},
		{Config: ConfigurationClassical, Transition: TransitionFunding, Phase: SampleWarmup, MessageBytes: 999, RTTMS: 9, Success: true},
	}
	rows, err := SummarizeMeasurements(records, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].MessageBytes != 100 || rows[0].RTTMedianMS != 1.5 {
		t.Fatalf("summary = %+v", rows)
	}
	records[1].MessageBytes = 101
	if _, err := SummarizeMeasurements(records, 0); err == nil {
		t.Fatal("inconsistent message_bytes were averaged or accepted")
	}
}

func TestNonScientificRunCannotReachFinalCSV(t *testing.T) {
	var records []RawMeasurementRecord
	for _, configuration := range Configurations() {
		configurationRecords, err := RunMeasurements(
			configuration,
			testBackend{configuration: configuration},
			testOnlySerializer{},
			deterministicExecutor{},
			RunOptions{MeasuredIterations: 1},
		)
		if err != nil {
			t.Fatal(err)
		}
		records = append(records, configurationRecords...)
	}
	rows, err := SummarizeMeasurements(records, 0)
	if err != nil {
		t.Fatalf("non-scientific plumbing summary failed: %v", err)
	}
	if len(rows) != 24 {
		t.Fatalf("summary row count = %d", len(rows))
	}
	if err := WriteFinalCSV(&bytes.Buffer{}, rows); err == nil {
		t.Fatal("non-scientific summaries reached final CSV")
	}
}

func TestMixedMeasurementProvenanceRejected(t *testing.T) {
	records := []RawMeasurementRecord{
		{Config: ConfigurationClassical, Transition: TransitionFunding, Phase: SampleMeasured, Scientific: true, MessageBytes: 100, RTTMS: 1, Success: true},
		{Config: ConfigurationClassical, Transition: TransitionFunding, Phase: SampleMeasured, Scientific: false, MessageBytes: 100, RTTMS: 1, Success: true},
	}
	if _, err := SummarizeMeasurements(records, MinimumScientificIterations); err == nil {
		t.Fatal("mixed scientific/non-scientific records accepted")
	}
}

func TestScientificSummaryCompletenessPolicy(t *testing.T) {
	t.Run("failed sample", func(t *testing.T) {
		records := completeScientificRecords(MinimumScientificIterations)
		records[123].Success = false
		records[123].ErrorMessage = "failed"
		if _, err := SummarizeMeasurements(records, MinimumScientificIterations); err == nil {
			t.Fatal("failed scientific sample accepted")
		}
	})

	t.Run("missing iteration", func(t *testing.T) {
		records := completeScientificRecords(MinimumScientificIterations)
		records = records[:len(records)-1]
		if _, err := SummarizeMeasurements(records, MinimumScientificIterations); err == nil {
			t.Fatal("missing scientific iteration accepted")
		}
	})

	t.Run("duplicate iteration", func(t *testing.T) {
		records := completeScientificRecords(MinimumScientificIterations)
		records[len(records)-1].Iteration = records[len(records)-2].Iteration
		if _, err := SummarizeMeasurements(records, MinimumScientificIterations); err == nil {
			t.Fatal("duplicate scientific iteration accepted")
		}
	})

	t.Run("fewer than minimum", func(t *testing.T) {
		records := completeScientificRecords(MinimumScientificIterations - 1)
		if _, err := SummarizeMeasurements(records, MinimumScientificIterations-1); err == nil {
			t.Fatal("scientific summary below 1000 iterations accepted")
		}
	})

	t.Run("exactly complete with warmup excluded", func(t *testing.T) {
		records := completeScientificRecords(MinimumScientificIterations)
		records = append([]RawMeasurementRecord{{
			Config: ConfigurationClassical, Transition: TransitionFunding,
			Phase: SampleWarmup, Scientific: true, MessageBytes: 999,
			RTTMS: 999, Success: true,
		}}, records...)
		rows, err := SummarizeMeasurements(records, MinimumScientificIterations)
		if err != nil {
			t.Fatal(err)
		}
		if len(rows) != 1 || !rows[0].Scientific || rows[0].MessageBytes != 100 ||
			rows[0].RTTMedianMS != 5 || rows[0].RTTP95MS != 5 || rows[0].RTTP99MS != 5 {
			t.Fatalf("scientific summary = %+v", rows)
		}
	})
}

func TestCompleteSummaryValidation(t *testing.T) {
	rows := completeSummaryRows()
	if err := ValidateCompleteSummaries(rows); err != nil {
		t.Fatalf("complete summaries rejected: %v", err)
	}
	if err := ValidateCompleteSummaries(rows[:len(rows)-1]); err == nil {
		t.Fatal("missing combination accepted")
	}
	duplicate := append([]SummaryRow(nil), rows...)
	duplicate[len(duplicate)-1] = duplicate[0]
	if err := ValidateCompleteSummaries(duplicate); err == nil {
		t.Fatal("duplicate combination accepted")
	}
	unknown := append([]SummaryRow(nil), rows...)
	unknown[0].Config = Configuration("unknown")
	if err := ValidateCompleteSummaries(unknown); err == nil {
		t.Fatal("unknown configuration accepted")
	}
}

func TestFinalCSVSchemaAndOrder(t *testing.T) {
	rows := completeSummaryRows()
	for left, right := 0, len(rows)-1; left < right; left, right = left+1, right-1 {
		rows[left], rows[right] = rows[right], rows[left]
	}
	var output bytes.Buffer
	if err := WriteFinalCSV(&output, rows); err != nil {
		t.Fatal(err)
	}
	parsed, err := csv.NewReader(strings.NewReader(output.String())).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(parsed) != 25 {
		t.Fatalf("CSV row count = %d, want 25", len(parsed))
	}
	if !reflect.DeepEqual(parsed[0], FinalCSVFields) || len(parsed[0]) != 5 {
		t.Fatalf("header = %v", parsed[0])
	}
	row := 1
	for _, configuration := range Configurations() {
		for _, transition := range finalTransitionOrder {
			if parsed[row][0] != string(configuration) || parsed[row][1] != string(transition) {
				t.Fatalf("row %d order = %v", row, parsed[row])
			}
			row++
		}
	}
	if strings.Contains(parsed[0][0]+strings.Join(parsed[0], ","), "p99") {
		t.Fatal("p99 appeared in final CSV schema")
	}
}

func TestRawCSVFormatPreservesPhasesAndFailure(t *testing.T) {
	records := []RawMeasurementRecord{
		{Config: ConfigurationClassical, Transition: TransitionFunding, Iteration: 0, Phase: SampleWarmup, MessageBytes: 10, RTTMS: 1.25, Success: true},
		{Config: ConfigurationClassical, Transition: TransitionPenalty, Iteration: 0, Phase: SampleMeasured, MessageBytes: 11, RTTMS: 2.5, Success: false, ErrorMessage: "failed"},
	}
	var output bytes.Buffer
	if err := WriteRawCSV(&output, records); err != nil {
		t.Fatal(err)
	}
	parsed, err := csv.NewReader(strings.NewReader(output.String())).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	wantHeader := []string{"config", "transition", "iteration", "phase", "scientific", "message_bytes", "rtt_ms", "success", "error"}
	if !reflect.DeepEqual(parsed[0], wantHeader) || parsed[1][3] != "warmup" || parsed[2][3] != "measured" ||
		parsed[2][4] != "false" || parsed[2][7] != "false" || parsed[2][8] != "failed" {
		t.Fatalf("raw CSV = %v", parsed)
	}
}

func completeSummaryRows() []SummaryRow {
	rows := make([]SummaryRow, 0, len(configurations)*len(finalTransitionOrder))
	for configIndex, configuration := range Configurations() {
		for transitionIndex, transition := range finalTransitionOrder {
			rows = append(rows, SummaryRow{
				Config:       configuration,
				Transition:   transition,
				Scientific:   true,
				MessageBytes: 100 + configIndex*10 + transitionIndex,
				RTTMedianMS:  float64(transitionIndex + 1),
				RTTP95MS:     float64(transitionIndex + 2),
				RTTP99MS:     float64(transitionIndex + 3),
			})
		}
	}
	return rows
}

func completeScientificRecords(count int) []RawMeasurementRecord {
	records := make([]RawMeasurementRecord, count)
	for iteration := range records {
		records[iteration] = RawMeasurementRecord{
			Config: ConfigurationClassical, Transition: TransitionFunding,
			Iteration: uint64(iteration), Phase: SampleMeasured, Scientific: true,
			MessageBytes: 100, RTTMS: 5, Success: true,
		}
	}
	return records
}

func closeFloat(left, right float64) bool {
	return math.Abs(left-right) < 1e-12
}
