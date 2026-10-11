package e7

import (
	"math/big"
	"testing"
)

func TestLogicalReservationSettlementAndConservation(t *testing.T) {
	state, _ := NewLogicalState("1", "1")
	sample := sampleForNet(t, "-0.25")
	if err := state.Reserve(sample); err != nil {
		t.Fatal(err)
	}
	reserved := state.Snapshot()
	if reserved.Prosumer.RatString() != "3/4" || reserved.Reserved.RatString() != "1/4" || reserved.Pending == nil {
		t.Fatal("reservation mismatch")
	}
	if err := state.Finalize(sample.LogicalTradeID); err != nil {
		t.Fatal(err)
	}
	settled := state.Snapshot()
	if settled.Prosumer.RatString() != "3/4" || settled.Counterparty.RatString() != "5/4" || settled.Reserved.Sign() != 0 || settled.Pending != nil {
		t.Fatal("settlement mismatch")
	}
}

func TestInsufficientLogicalBalanceRejected(t *testing.T) {
	state, _ := NewLogicalState("0.1", "0")
	if err := state.Reserve(sampleForNet(t, "-0.2")); err == nil {
		t.Fatal("insufficient balance accepted")
	}
}

func TestFullFrozenTraceLogicalReplayWithoutReplenishment(t *testing.T) {
	samples, err := LoadFrozenSamples(frozenTracePath(t))
	if err != nil {
		t.Fatal(err)
	}
	state, err := NewTraceDerivedLogicalState()
	if err != nil {
		t.Fatal(err)
	}
	var afterWarmup BalanceSnapshot
	for index, sample := range samples {
		if err := state.Reserve(sample); err != nil {
			t.Fatalf("reserve row %d: %v", index, err)
		}
		if err := state.Finalize(sample.LogicalTradeID); err != nil {
			t.Fatalf("settle row %d: %v", index, err)
		}
		if index == WarmupRowCount-1 {
			afterWarmup = state.Snapshot()
		}
	}
	final := state.Snapshot()
	if afterWarmup.Prosumer.Cmp(final.Prosumer) == 0 {
		t.Fatal("warm-up state was apparently reset before measured rows")
	}
	if new(big.Rat).Add(final.Prosumer, final.Counterparty).Cmp(big.NewRat(52173, 500)) != 0 {
		t.Fatal("total balance changed")
	}
}
