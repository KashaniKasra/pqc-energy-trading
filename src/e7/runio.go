package e7

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

// PersistSamples retains warm-up, measured, and first-failure rows alike.
func PersistSamples(runDirectory string, records []RawSample) (FileEvidence, error) {
	path := filepath.Join(runDirectory, "samples.csv")
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return FileEvidence{}, err
	}
	writeErr := WriteRawEvidence(file, records)
	if syncErr := file.Sync(); writeErr == nil {
		writeErr = syncErr
	}
	if closeErr := file.Close(); writeErr == nil {
		writeErr = closeErr
	}
	if writeErr != nil {
		return FileEvidence{}, writeErr
	}
	return HashFile(path)
}

func PersistFabricAudit(runDirectory string, records []RawSample) (map[string]FileEvidence, error) {
	outputs := map[string]FileEvidence{}
	for _, specification := range []struct {
		name   string
		header []string
		row    func(RawSample) []string
	}{
		{"transactions.csv", []string{"trace_row_idx", "transaction_id", "validation_status", "validation_code", "committed", "verified"}, func(record RawSample) []string {
			return []string{strconv.Itoa(record.TraceRowIndex), record.TransactionID, record.ValidationStatus, strconv.Itoa(record.ValidationCode), strconv.FormatBool(record.Committed), strconv.FormatBool(record.Verified)}
		}},
		{"ledger_heights.csv", []string{"trace_row_idx", "transaction_id", "block_number"}, func(record RawSample) []string {
			return []string{strconv.Itoa(record.TraceRowIndex), record.TransactionID, strconv.FormatUint(record.FabricBlockNumber, 10)}
		}},
	} {
		path := filepath.Join(runDirectory, specification.name)
		file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
		if err != nil {
			return nil, err
		}
		writer := csv.NewWriter(file)
		_ = writer.Write(specification.header)
		for _, record := range records {
			_ = writer.Write(specification.row(record))
		}
		writer.Flush()
		writeErr := writer.Error()
		if syncErr := file.Sync(); writeErr == nil {
			writeErr = syncErr
		}
		if closeErr := file.Close(); writeErr == nil {
			writeErr = closeErr
		}
		if writeErr != nil {
			return nil, fmt.Errorf("write %s: %w", specification.name, writeErr)
		}
		evidence, err := HashFile(path)
		if err != nil {
			return nil, err
		}
		outputs[specification.name] = evidence
	}
	return outputs, nil
}
