package e7

import (
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
)

var RawEvidenceFields = []string{
	"config", "run_id", "phase", "phase_idx", "sample_idx", "trace_row_idx",
	"trace_timestamp", "trace_row_sha256", "logical_trade_id", "direction", "amount_kwh",
	"payer", "payee", "start_monotonic_ns", "end_monotonic_ns", "latency_ms", "scientific",
	"success", "error", "request_id", "transaction_id", "completion_status", "channel_id",
	"htlc_id", "add_algorithm", "settle_algorithm", "add_serialized_bytes",
	"settle_serialized_bytes", "add_serialized_sha256", "settle_serialized_sha256",
	"authorization_verified", "receiver_verified", "committed", "verified",
	"validation_status", "validation_code", "payload_sha256",
	"sets_ecdsa_algorithm", "sets_signed_preimage_sha256", "sets_public_key_sha256",
	"sets_signature_sha256", "sets_signature_verified", "sets_controller_eligible",
	"sets_controller_balance_sufficient",
}

type RawSample struct {
	Config           Configuration
	RunID            string
	Phase            Phase
	PhaseIndex       int
	SampleIndex      int
	TraceRowIndex    int
	TraceTimestamp   string
	TraceRowSHA256   string
	LogicalTradeID   string
	Direction        Direction
	AmountKWh        string
	Payer            string
	Payee            string
	StartMonotonicNS int64
	EndMonotonicNS   int64
	LatencyMS        float64
	Scientific       bool
	Success          bool
	Error            string
	CompletionEvidence
}

func RawSampleFrom(sample TradeSample, config Configuration, scientific bool, completion CompletionEvidence) RawSample {
	return RawSample{
		Config: config, RunID: sample.RunID, Phase: sample.Phase, PhaseIndex: sample.PhaseIndex,
		SampleIndex: sample.SampleIndex, TraceRowIndex: sample.TraceRowIndex,
		TraceTimestamp: sample.Timestamp, TraceRowSHA256: sample.TraceRowSHA256,
		LogicalTradeID: sample.LogicalTradeID, Direction: sample.Direction,
		AmountKWh: sample.AmountText, Payer: sample.Payer, Payee: sample.Payee,
		Scientific: scientific, CompletionEvidence: completion,
	}
}

func ValidateRawSample(record RawSample) error {
	if !record.Config.Valid() || record.RunID != RunID || record.TraceRowIndex < 0 || record.TraceRowIndex >= TraceRowCount {
		return errors.New("invalid E7 raw evidence identity")
	}
	if record.LogicalTradeID == "" || record.TraceRowSHA256 == "" || record.TraceTimestamp == "" {
		return errors.New("missing E7 raw evidence trace identity")
	}
	if record.Phase == WarmupPhase {
		if record.TraceRowIndex >= WarmupRowCount || record.SampleIndex != -1 || record.PhaseIndex != record.TraceRowIndex {
			return errors.New("invalid E7 warm-up mapping")
		}
	} else if record.Phase == MeasuredPhase {
		want := record.TraceRowIndex - WarmupRowCount
		if record.TraceRowIndex < WarmupRowCount || record.SampleIndex != want || record.PhaseIndex != want {
			return errors.New("invalid E7 measured mapping")
		}
	} else {
		return errors.New("invalid E7 evidence phase")
	}
	if !record.Success || record.Error != "" || math.IsNaN(record.LatencyMS) || math.IsInf(record.LatencyMS, 0) || record.LatencyMS <= 0 {
		return errors.New("E7 evidence is not a successful positive-latency sample")
	}
	if record.EndMonotonicNS <= record.StartMonotonicNS {
		return errors.New("invalid E7 monotonic timing markers")
	}
	if record.Config == ConfigurationSETSReimpl {
		if record.SETSECDSAAlgorithm != SETSECDSAAlgorithm ||
			!validSHA256Hex(record.SETSSignedPreimageSHA256) ||
			!validSHA256Hex(record.SETSPublicKeySHA256) ||
			!validSHA256Hex(record.SETSSignatureSHA256) ||
			!record.SETSSignatureVerified || !record.SETSControllerEligible ||
			!record.SETSControllerBalanceSufficient {
			return errors.New("invalid SETS ECDSA/controller evidence")
		}
	}
	return nil
}

func validSHA256Hex(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size && value == strings.ToLower(value)
}

func WriteRawEvidence(writer io.Writer, records []RawSample) error {
	csvWriter := csv.NewWriter(writer)
	if err := csvWriter.Write(RawEvidenceFields); err != nil {
		return err
	}
	for _, record := range records {
		if err := csvWriter.Write(rawSampleFields(record)); err != nil {
			return err
		}
	}
	csvWriter.Flush()
	return csvWriter.Error()
}

func rawSampleFields(r RawSample) []string {
	return []string{
		string(r.Config), r.RunID, string(r.Phase), strconv.Itoa(r.PhaseIndex), strconv.Itoa(r.SampleIndex), strconv.Itoa(r.TraceRowIndex),
		r.TraceTimestamp, r.TraceRowSHA256, r.LogicalTradeID, string(r.Direction), r.AmountKWh, r.Payer, r.Payee,
		strconv.FormatInt(r.StartMonotonicNS, 10), strconv.FormatInt(r.EndMonotonicNS, 10), fmt.Sprintf("%.9f", r.LatencyMS),
		strconv.FormatBool(r.Scientific), strconv.FormatBool(r.Success), r.Error, r.RequestID, r.TransactionID, r.CompletionStatus,
		r.ChannelID, r.HTLCID, r.AddAlgorithm, r.SettleAlgorithm, strconv.Itoa(r.AddSerializedBytes), strconv.Itoa(r.SettleSerializedBytes),
		r.AddSerializedSHA256, r.SettleSerializedSHA256, strconv.FormatBool(r.AuthorizationVerified), strconv.FormatBool(r.ReceiverVerified),
		strconv.FormatBool(r.Committed), strconv.FormatBool(r.Verified), r.ValidationStatus, strconv.Itoa(r.ValidationCode), r.PayloadSHA256,
		r.SETSECDSAAlgorithm, r.SETSSignedPreimageSHA256, r.SETSPublicKeySHA256,
		r.SETSSignatureSHA256, strconv.FormatBool(r.SETSSignatureVerified),
		strconv.FormatBool(r.SETSControllerEligible), strconv.FormatBool(r.SETSControllerBalanceSufficient),
	}
}

type RunManifest struct {
	Schema                string            `json:"schema"`
	Config                Configuration     `json:"config"`
	RunID                 string            `json:"run_id"`
	TraceSHA256           string            `json:"trace_sha256"`
	TraceProvenanceSHA256 string            `json:"trace_provenance_sha256"`
	GitCommit             string            `json:"git_commit"`
	GitDirty              bool              `json:"git_dirty"`
	WarmupRows            int               `json:"warmup_rows"`
	MeasuredRows          int               `json:"measured_rows"`
	TimerDefinition       string            `json:"timer_definition"`
	ProcessResetMethod    string            `json:"process_reset_method"`
	CodeHashes            map[string]string `json:"code_hashes"`
	RuntimeVersions       map[string]string `json:"runtime_versions"`
	ScientificEnvironment map[string]any    `json:"scientific_environment"`
}
