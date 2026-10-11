package e7

import (
	"bytes"
	"math"
	"strings"
	"testing"
)

func validRawSample(t *testing.T) RawSample {
	sample := sampleForNet(t, "-0.1")
	record := RawSampleFrom(sample, ConfigurationL1Only, true, CompletionEvidence{RequestID: sample.LogicalTradeID, CompletionStatus: "VALID"})
	record.Success = true
	record.StartMonotonicNS = 1
	record.EndMonotonicNS = 2
	record.LatencyMS = 0.000001
	return record
}

func TestRawEvidenceValidationAndWriter(t *testing.T) {
	record := validRawSample(t)
	if err := ValidateRawSample(record); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := WriteRawEvidence(&output, []RawSample{record}); err != nil {
		t.Fatal(err)
	}
	if !bytes.HasPrefix(output.Bytes(), []byte("config,run_id,phase,")) {
		t.Fatal("raw schema missing")
	}
	if len(rawSampleFields(record)) != len(RawEvidenceFields) {
		t.Fatal("raw schema and serialized row width differ")
	}
}

func TestSETSRawEvidenceRequiresProofFields(t *testing.T) {
	record := validRawSample(t)
	record.Config = ConfigurationSETSReimpl
	record.SETSECDSAAlgorithm = SETSECDSAAlgorithm
	record.SETSSignedPreimageSHA256 = "1" + strings.Repeat("0", 63)
	record.SETSPublicKeySHA256 = "2" + strings.Repeat("0", 63)
	record.SETSSignatureSHA256 = "3" + strings.Repeat("0", 63)
	record.SETSSignatureVerified = true
	record.SETSControllerEligible = true
	record.SETSControllerBalanceSufficient = true
	if err := ValidateRawSample(record); err != nil {
		t.Fatal(err)
	}
	record.SETSSignatureVerified = false
	if err := ValidateRawSample(record); err == nil {
		t.Fatal("unverified SETS signature accepted")
	}
}

func TestMalformedRawEvidenceFailsClosed(t *testing.T) {
	record := validRawSample(t)
	record.LogicalTradeID = ""
	if err := ValidateRawSample(record); err == nil {
		t.Fatal("missing identity accepted")
	}
	record = validRawSample(t)
	record.LatencyMS = math.NaN()
	if err := ValidateRawSample(record); err == nil {
		t.Fatal("NaN accepted")
	}
}
