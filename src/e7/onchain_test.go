package e7

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"errors"
	"fmt"
	"testing"
)

type fakeFabric struct {
	prepared          bool
	keys              []string
	payloads          [][]byte
	fail              bool
	invalidCompletion bool
	completion        *FabricCompletion
}

func (f *fakeFabric) PrepareRun(context.Context, Configuration) error { f.prepared = true; return nil }
func (f *fakeFabric) SubmitAndAwaitCommit(_ context.Context, key string, value []byte) (FabricCompletion, error) {
	if f.fail {
		return FabricCompletion{}, errors.New("failed")
	}
	f.keys = append(f.keys, key)
	f.payloads = append(f.payloads, append([]byte(nil), value...))
	if f.invalidCompletion {
		return FabricCompletion{TransactionID: "tx", Committed: false, Verified: false, ValidationStatus: "INVALID"}, nil
	}
	if f.completion != nil {
		return *f.completion, nil
	}
	return FabricCompletion{TransactionID: "tx", Committed: true, Verified: true, ValidationStatus: "VALID"}, nil
}
func (f *fakeFabric) Close() error { return nil }

func TestCanonicalLogicalPayloadDeterministicAcrossConfigs(t *testing.T) {
	sample := sampleForNet(t, "-0.25")
	common1, common2 := CanonicalLogicalSettlement(sample), CanonicalLogicalSettlement(sample)
	if !bytes.Equal(common1, common2) {
		t.Fatal("common payload nondeterministic")
	}
	l1 := CanonicalOnChainPayload(ConfigurationL1Only, common1)
	sets := CanonicalOnChainPayload(ConfigurationSETSReimpl, common2)
	if bytes.Equal(l1, sets) {
		t.Fatal("config envelope missing")
	}
	if !bytes.Contains(l1, common1) || !bytes.Contains(sets, common1) {
		t.Fatal("common subsection changed")
	}
}

func TestSETSP256SignatureAndTamperDetection(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	preimage := CanonicalLogicalSettlement(sampleForNet(t, "0.5"))
	digest := sha256.Sum256(preimage)
	signature, err := ecdsa.SignASN1(rand.Reader, key, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	if !VerifySETSSignature(&key.PublicKey, preimage, signature) {
		t.Fatal("valid SETS signature rejected")
	}
	if VerifySETSSignature(&key.PublicKey, append(preimage, 'x'), signature) {
		t.Fatal("altered SETS preimage accepted")
	}
}

func TestOnChainExecutorRequiresCommittedValidatedStatus(t *testing.T) {
	state, _ := NewLogicalState("1", "1")
	fabric := &fakeFabric{}
	executor, _ := NewOnChainExecutor(ConfigurationSETSReimpl, state, fabric)
	if err := executor.PrepareRun(context.Background()); err != nil {
		t.Fatal(err)
	}
	sample := sampleForNet(t, "-0.25")
	result, err := executor.Settle(context.Background(), sample)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Committed || !result.Verified || len(fabric.payloads) != 1 {
		t.Fatal("commit evidence missing")
	}
	if result.SETSECDSAAlgorithm != SETSECDSAAlgorithm ||
		!validSHA256Hex(result.SETSSignedPreimageSHA256) ||
		!validSHA256Hex(result.SETSPublicKeySHA256) ||
		!validSHA256Hex(result.SETSSignatureSHA256) ||
		!result.SETSSignatureVerified || !result.SETSControllerEligible ||
		!result.SETSControllerBalanceSufficient {
		t.Fatal("SETS ECDSA/controller evidence missing")
	}
	wantPreimage := sha256.Sum256(CanonicalLogicalSettlement(sample))
	if result.SETSSignedPreimageSHA256 != fmt.Sprintf("%x", wantPreimage) {
		t.Fatal("SETS signed preimage hash mismatch")
	}
}

func assertLogicalStateRestored(t *testing.T, before, after BalanceSnapshot) {
	t.Helper()
	if before.Prosumer.Cmp(after.Prosumer) != 0 || before.Counterparty.Cmp(after.Counterparty) != 0 {
		t.Fatal("logical payer/payee balances were not restored")
	}
	if after.Reserved.Sign() != 0 || after.Pending != nil {
		t.Fatal("logical reservation was not cleared")
	}
}

func TestOnChainFailureRollsBackLogicalReservation(t *testing.T) {
	for _, invalidCompletion := range []bool{false, true} {
		name := "transport_failure"
		fabric := &fakeFabric{fail: true}
		if invalidCompletion {
			name = "invalid_completion"
			fabric = &fakeFabric{invalidCompletion: true}
		}
		t.Run(name, func(t *testing.T) {
			state, _ := NewLogicalState("1", "1")
			executor, _ := NewOnChainExecutor(ConfigurationL1Only, state, fabric)
			if err := executor.PrepareRun(context.Background()); err != nil {
				t.Fatal(err)
			}
			before := state.Snapshot()
			if _, err := executor.Settle(context.Background(), sampleForNet(t, "-0.25")); err == nil {
				t.Fatal("failed Fabric settlement returned success")
			}
			assertLogicalStateRestored(t, before, state.Snapshot())
		})
	}
}

func TestOnChainRejectsInexactFabricCompletion(t *testing.T) {
	valid := FabricCompletion{TransactionID: "tx", Committed: true, Verified: true, ValidationStatus: "VALID", ValidationCode: 0}
	tests := map[string]FabricCompletion{
		"nonzero validation code": func() FabricCompletion { value := valid; value.ValidationCode = 10; return value }(),
		"non-VALID status":        func() FabricCompletion { value := valid; value.ValidationStatus = "MVCC_READ_CONFLICT"; return value }(),
		"empty transaction ID":    func() FabricCompletion { value := valid; value.TransactionID = ""; return value }(),
	}
	for name, completion := range tests {
		t.Run(name, func(t *testing.T) {
			state, _ := NewLogicalState("1", "1")
			executor, _ := NewOnChainExecutor(ConfigurationL1Only, state, &fakeFabric{completion: &completion})
			if err := executor.PrepareRun(context.Background()); err != nil {
				t.Fatal(err)
			}
			before := state.Snapshot()
			if _, err := executor.Settle(context.Background(), sampleForNet(t, "-0.25")); err == nil {
				t.Fatal("invalid Fabric completion returned success")
			}
			assertLogicalStateRestored(t, before, state.Snapshot())
		})
	}
}
