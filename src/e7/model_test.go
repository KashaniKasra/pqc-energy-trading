package e7

import (
	"bytes"
	"math/big"
	"path/filepath"
	"testing"
)

func frozenTracePath(t *testing.T) string {
	t.Helper()
	return filepath.Join("..", "..", "env", "e7", "trace", "opsd_household_15min_e7.csv")
}

func sampleForNet(t *testing.T, netText string) TradeSample {
	t.Helper()
	net, err := parseRat(netText, "net")
	if err != nil {
		t.Fatal(err)
	}
	row := TraceRow{Index: 100, Timestamp: "2016-01-01T00:00:00+00:00", PVText: "1", DemandText: "1", NetText: netText, Net: net, RowSHA256: "row"}
	sample, err := SampleFromTraceRow(row)
	if err != nil {
		t.Fatal(err)
	}
	return sample
}

func TestLoadFrozenTraceAndMapping(t *testing.T) {
	samples, err := LoadFrozenSamples(frozenTracePath(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(samples) != TraceRowCount {
		t.Fatalf("rows=%d", len(samples))
	}
	if samples[0].Phase != WarmupPhase || samples[99].Phase != WarmupPhase || samples[100].SampleIndex != 0 || samples[1099].SampleIndex != 999 {
		t.Fatal("warm-up/measured mapping mismatch")
	}
	tiny := samples[918]
	if tiny.AmountText != "3.5527136788e-15" || tiny.Direction != ProsumerSale || tiny.Amount.Sign() <= 0 {
		t.Fatalf("tiny positive value was not preserved: %+v", tiny)
	}
}

func TestTradeDirectionsAndExactAmounts(t *testing.T) {
	sale := sampleForNet(t, "0.125")
	if sale.Direction != ProsumerSale || sale.Payer != Counterparty || sale.Payee != Prosumer || sale.Amount.Cmp(big.NewRat(1, 8)) != 0 {
		t.Fatal("sale mapping")
	}
	purchase := sampleForNet(t, "-0.125")
	if purchase.Direction != ProsumerPurchase || purchase.Payer != Prosumer || purchase.Payee != Counterparty || purchase.AmountText != "0.125" {
		t.Fatal("purchase mapping")
	}
	zero := sampleForNet(t, "0.000")
	if zero.Direction != BalancedTransfer || zero.AmountText != "0" || zero.Payer != NoParty || zero.Payee != NoParty {
		t.Fatal("zero mapping")
	}
}

func TestTradeIdentityIsDeterministicAndConfigIndependent(t *testing.T) {
	sample := sampleForNet(t, "0.5")
	first := append([]byte(nil), CanonicalTradeIdentity(sample)...)
	second := CanonicalTradeIdentity(sample)
	if !bytes.Equal(first, second) || sample.LogicalTradeID == "" || sample.HTLCID != TradeSchemaVersion+":"+sample.LogicalTradeID {
		t.Fatal("identity is not deterministic")
	}
	if err := ValidateSampleIdentity(sample); err != nil {
		t.Fatal(err)
	}
	tampered := sample
	tampered.AmountText = "0.6"
	if err := ValidateSampleIdentity(tampered); err == nil {
		t.Fatal("tampered identity accepted")
	}
}
