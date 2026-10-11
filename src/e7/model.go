package e7

import (
	"bytes"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	TraceSHA256         = "7e472c032317a7e8f0af587782238cab48f22ddc50f74200cff65e9681f8bd1c"
	TraceRowCount       = 1100
	WarmupRowCount      = 100
	MeasuredRowCount    = 1000
	RunID               = "1"
	TradeSchemaVersion  = "e7-v1"
	Prosumer            = "residential3"
	Counterparty        = "external-market-grid-counterparty"
	NoParty             = "none"
	InitialProsumerText = "104.346"
)

type Direction string

const (
	ProsumerSale     Direction = "prosumer_sale"
	ProsumerPurchase Direction = "prosumer_purchase"
	BalancedTransfer Direction = "balanced_no_transfer"
)

type Phase string

const (
	WarmupPhase   Phase = "warmup"
	MeasuredPhase Phase = "measured"
)

type TraceRow struct {
	Index      int
	Timestamp  string
	PVText     string
	DemandText string
	NetText    string
	PV         *big.Rat
	Demand     *big.Rat
	Net        *big.Rat
	RowSHA256  string
}

type TradeSample struct {
	TraceSHA256    string
	RunID          string
	TraceRowIndex  int
	Phase          Phase
	PhaseIndex     int
	SampleIndex    int
	Timestamp      string
	PVText         string
	DemandText     string
	NetText        string
	Direction      Direction
	AmountText     string
	Amount         *big.Rat
	Payer          string
	Payee          string
	LogicalTradeID string
	HTLCID         string
	TraceRowSHA256 string
}

func parseRat(text, field string) (*big.Rat, error) {
	if text == "" || strings.TrimSpace(text) != text {
		return nil, fmt.Errorf("%s must be a non-empty canonical decimal", field)
	}
	value, ok := new(big.Rat).SetString(text)
	if !ok {
		return nil, fmt.Errorf("%s is not an exact decimal: %q", field, text)
	}
	return value, nil
}

func LoadFrozenTrace(path string) ([]TraceRow, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read E7 trace: %w", err)
	}
	digest := sha256.Sum256(content)
	if hex.EncodeToString(digest[:]) != TraceSHA256 {
		return nil, errors.New("frozen E7 trace SHA-256 mismatch")
	}
	reader := csv.NewReader(bytes.NewReader(content))
	records, err := reader.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("parse E7 trace: %w", err)
	}
	if len(records) != TraceRowCount+1 {
		return nil, fmt.Errorf("E7 trace row count = %d, want %d", len(records)-1, TraceRowCount)
	}
	wantHeader := []string{"timestamp", "pv_kwh", "demand_kwh", "net_kwh"}
	if len(records[0]) != len(wantHeader) {
		return nil, errors.New("E7 trace schema mismatch")
	}
	for index := range wantHeader {
		if records[0][index] != wantHeader[index] {
			return nil, errors.New("E7 trace schema mismatch")
		}
	}
	rows := make([]TraceRow, 0, TraceRowCount)
	var previous time.Time
	for index, record := range records[1:] {
		if len(record) != len(wantHeader) {
			return nil, fmt.Errorf("E7 trace row %d field count mismatch", index)
		}
		timestamp, err := time.Parse(time.RFC3339, record[0])
		if err != nil {
			return nil, fmt.Errorf("E7 trace row %d timestamp: %w", index, err)
		}
		if index > 0 && timestamp.Sub(previous) != 15*time.Minute {
			return nil, fmt.Errorf("E7 trace row %d is not 15 minutes after its predecessor", index)
		}
		previous = timestamp
		pv, err := parseRat(record[1], "pv_kwh")
		if err != nil || pv.Sign() < 0 {
			return nil, fmt.Errorf("E7 trace row %d invalid pv_kwh", index)
		}
		demand, err := parseRat(record[2], "demand_kwh")
		if err != nil || demand.Sign() < 0 {
			return nil, fmt.Errorf("E7 trace row %d invalid demand_kwh", index)
		}
		net, err := parseRat(record[3], "net_kwh")
		if err != nil {
			return nil, fmt.Errorf("E7 trace row %d invalid net_kwh", index)
		}
		rowBytes := []byte(strings.Join(record, ",") + "\n")
		rowDigest := sha256.Sum256(rowBytes)
		rows = append(rows, TraceRow{
			Index: index, Timestamp: record[0], PVText: record[1], DemandText: record[2], NetText: record[3],
			PV: pv, Demand: demand, Net: net, RowSHA256: hex.EncodeToString(rowDigest[:]),
		})
	}
	return rows, nil
}

func SampleFromTraceRow(row TraceRow) (TradeSample, error) {
	if row.Index < 0 || row.Index >= TraceRowCount || row.Net == nil {
		return TradeSample{}, errors.New("invalid E7 trace row")
	}
	sample := TradeSample{
		TraceSHA256: TraceSHA256, RunID: RunID, TraceRowIndex: row.Index,
		Timestamp: row.Timestamp, PVText: row.PVText, DemandText: row.DemandText,
		NetText: row.NetText, TraceRowSHA256: row.RowSHA256,
	}
	if row.Index < WarmupRowCount {
		sample.Phase, sample.PhaseIndex, sample.SampleIndex = WarmupPhase, row.Index, -1
	} else {
		sample.Phase, sample.PhaseIndex, sample.SampleIndex = MeasuredPhase, row.Index-WarmupRowCount, row.Index-WarmupRowCount
	}
	switch row.Net.Sign() {
	case 1:
		sample.Direction, sample.AmountText = ProsumerSale, row.NetText
		sample.Payer, sample.Payee = Counterparty, Prosumer
	case -1:
		sample.Direction = ProsumerPurchase
		sample.AmountText = strings.TrimPrefix(row.NetText, "-")
		sample.Payer, sample.Payee = Prosumer, Counterparty
	default:
		sample.Direction, sample.AmountText = BalancedTransfer, "0"
		sample.Payer, sample.Payee = NoParty, NoParty
	}
	amount, err := parseRat(sample.AmountText, "amount_kwh")
	if err != nil || amount.Sign() < 0 {
		return TradeSample{}, errors.New("invalid E7 trade amount")
	}
	sample.Amount = amount
	identity := CanonicalTradeIdentity(sample)
	digest := sha256.Sum256(identity)
	sample.LogicalTradeID = hex.EncodeToString(digest[:])
	sample.HTLCID = TradeSchemaVersion + ":" + sample.LogicalTradeID
	return sample, nil
}

func appendIdentityField(builder *strings.Builder, name, value string) {
	builder.WriteString(name)
	builder.WriteByte(':')
	builder.WriteString(strconv.Itoa(len([]byte(value))))
	builder.WriteByte(':')
	builder.WriteString(value)
	builder.WriteByte('\n')
}

// CanonicalTradeIdentity excludes configuration so every E7 path receives the
// same logical settlement. Length-prefixed UTF-8 fields make the text encoding
// unambiguous while preserving the frozen decimal amount exactly.
func CanonicalTradeIdentity(sample TradeSample) []byte {
	var builder strings.Builder
	appendIdentityField(&builder, "schema_version", TradeSchemaVersion)
	appendIdentityField(&builder, "trace_sha256", sample.TraceSHA256)
	appendIdentityField(&builder, "run_id", sample.RunID)
	appendIdentityField(&builder, "trace_row_idx", strconv.Itoa(sample.TraceRowIndex))
	appendIdentityField(&builder, "timestamp", sample.Timestamp)
	appendIdentityField(&builder, "direction", string(sample.Direction))
	appendIdentityField(&builder, "amount_kwh", sample.AmountText)
	appendIdentityField(&builder, "payer", sample.Payer)
	appendIdentityField(&builder, "payee", sample.Payee)
	return []byte(builder.String())
}

func LoadFrozenSamples(path string) ([]TradeSample, error) {
	rows, err := LoadFrozenTrace(path)
	if err != nil {
		return nil, err
	}
	samples := make([]TradeSample, 0, len(rows))
	for _, row := range rows {
		sample, err := SampleFromTraceRow(row)
		if err != nil {
			return nil, err
		}
		samples = append(samples, sample)
	}
	return samples, nil
}

func ValidateSampleIdentity(sample TradeSample) error {
	if sample.TraceSHA256 != TraceSHA256 || sample.RunID != RunID || sample.Amount == nil {
		return errors.New("E7 trade identity provenance mismatch")
	}
	digest := sha256.Sum256(CanonicalTradeIdentity(sample))
	wantTradeID := hex.EncodeToString(digest[:])
	if sample.LogicalTradeID != wantTradeID || sample.HTLCID != TradeSchemaVersion+":"+wantTradeID {
		return errors.New("E7 logical trade or HTLC identity mismatch")
	}
	return validateTradeParties(sample)
}
