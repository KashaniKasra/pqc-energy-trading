package e2

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"testing"
)

const (
	alice PartyID = "alice"
	bob   PartyID = "bob"
)

func newTestChannel(t *testing.T, configuration Configuration) *Channel {
	t.Helper()
	channel, err := NewChannel(configuration, alice, bob)
	if err != nil {
		t.Fatalf("NewChannel: %v", err)
	}
	return channel
}

func setupAndFund(t *testing.T, channel *Channel) {
	t.Helper()
	setup, err := channel.Setup(alice)
	if err != nil {
		t.Fatalf("Setup: %v", err)
	}
	if setup.Type != TransitionChannelSetup {
		t.Fatalf("setup type = %q", setup.Type)
	}
	funding, err := channel.Fund(alice)
	if err != nil {
		t.Fatalf("Fund: %v", err)
	}
	if funding.Type != TransitionFunding || funding.StateNumber != 0 {
		t.Fatalf("unexpected funding transition: %+v", funding)
	}
	if !reflect.DeepEqual(funding.RequiredSigners, []PartyID{alice, bob}) {
		t.Fatalf("funding is not 2-of-2: %v", funding.RequiredSigners)
	}
}

func TestChannelSetupFundingAndCommitmentRevocation(t *testing.T) {
	channel := newTestChannel(t, ConfigurationClassical)
	setupAndFund(t, channel)

	state0, ok := channel.Commitment(0)
	if !ok || state0.Revoked {
		t.Fatalf("initial funded commitment = %+v, %v", state0, ok)
	}

	update1, err := channel.UpdateCommitment(alice)
	if err != nil {
		t.Fatalf("UpdateCommitment state 1: %v", err)
	}
	if update1.StateNumber != 1 || update1.Revocation == nil ||
		update1.Revocation.RevokedState != 0 || update1.Revocation.SupersededBy != 1 {
		t.Fatalf("state 1 revocation = %+v", update1)
	}

	update2, err := channel.UpdateCommitment(bob)
	if err != nil {
		t.Fatalf("UpdateCommitment state 2: %v", err)
	}
	if update2.StateNumber != 2 || !channel.IsRevoked(0) || !channel.IsRevoked(1) {
		t.Fatalf("state 2/revocation state is incorrect: %+v", update2)
	}
	current, ok := channel.CurrentCommitment()
	if !ok || current.Number != 2 || current.Revoked || channel.IsRevoked(2) {
		t.Fatalf("current state treated as revoked: %+v", current)
	}
}

func TestHTLCAddAndSettle(t *testing.T) {
	channel := newTestChannel(t, ConfigurationUniformMLDSA)
	setupAndFund(t, channel)

	added, err := channel.AddHTLC(alice, "h1")
	if err != nil || added.Type != TransitionHTLCAdd || added.HTLCID != "h1" {
		t.Fatalf("AddHTLC = %+v, %v", added, err)
	}
	if _, err := channel.AddHTLC(bob, "h2"); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("second pending HTLC error = %v", err)
	}
	settled, err := channel.SettleHTLC(bob, "h1")
	if err != nil || settled.Type != TransitionHTLCSettle {
		t.Fatalf("SettleHTLC = %+v, %v", settled, err)
	}
	if _, err := channel.SettleHTLC(bob, "h1"); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("duplicate settle error = %v", err)
	}
}

func TestCooperativeClosePreventsFurtherUpdates(t *testing.T) {
	channel := newTestChannel(t, ConfigurationLayerAware)
	setupAndFund(t, channel)
	closed, err := channel.CooperativeClose(alice)
	if err != nil || closed.Type != TransitionCoopClose || channel.CloseType() != CloseCooperative {
		t.Fatalf("CooperativeClose = %+v, %v", closed, err)
	}
	if _, err := channel.UpdateCommitment(bob); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("post-close update error = %v", err)
	}
}

func TestStaleForceCloseProducesPenaltyWithRevocationEvidence(t *testing.T) {
	channel := newTestChannel(t, ConfigurationLayerAware)
	setupAndFund(t, channel)
	if _, err := channel.UpdateCommitment(alice); err != nil { // state 1; revoke 0
		t.Fatal(err)
	}
	if _, err := channel.UpdateCommitment(bob); err != nil { // state 2; revoke 1
		t.Fatal(err)
	}

	forceClose, err := channel.ForceClose(alice, 1)
	if err != nil || forceClose.Type != TransitionForceClose ||
		forceClose.ReferencedState == nil || *forceClose.ReferencedState != 1 {
		t.Fatalf("ForceClose = %+v, %v", forceClose, err)
	}
	penalty, err := channel.Penalty(bob)
	if err != nil {
		t.Fatalf("Penalty: %v", err)
	}
	if penalty.Type != TransitionPenalty || penalty.ReferencedState == nil ||
		*penalty.ReferencedState != 1 || penalty.Revocation == nil ||
		penalty.Revocation.RevokedState != 1 || penalty.Revocation.SupersededBy != 2 {
		t.Fatalf("penalty lacks stale-state revocation evidence: %+v", penalty)
	}
	if channel.CloseType() != ClosePenalty {
		t.Fatalf("close type = %q", channel.CloseType())
	}
}

func TestPenaltyRejectsCurrentNonRevokedState(t *testing.T) {
	channel := newTestChannel(t, ConfigurationClassical)
	setupAndFund(t, channel)
	if _, err := channel.UpdateCommitment(alice); err != nil {
		t.Fatal(err)
	}
	if _, err := channel.ForceClose(alice, 1); err != nil {
		t.Fatal(err)
	}
	if _, err := channel.Penalty(bob); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("penalty against current state error = %v", err)
	}
}

func TestInvalidTransitionOrdering(t *testing.T) {
	channel := newTestChannel(t, ConfigurationClassical)
	operations := []struct {
		name string
		call func() error
	}{
		{"fund before setup", func() error { _, err := channel.Fund(alice); return err }},
		{"update before funding", func() error { _, err := channel.UpdateCommitment(alice); return err }},
		{"htlc before funding", func() error { _, err := channel.AddHTLC(alice, "h1"); return err }},
		{"force-close before funding", func() error { _, err := channel.ForceClose(alice, 0); return err }},
		{"penalty before force-close", func() error { _, err := channel.Penalty(bob); return err }},
	}
	for _, operation := range operations {
		if err := operation.call(); !errors.Is(err, ErrInvalidTransition) {
			t.Fatalf("%s error = %v", operation.name, err)
		}
	}
}

func TestTransitionSignerSemantics(t *testing.T) {
	cooperative := newTestChannel(t, ConfigurationClassical)
	setup, err := cooperative.Setup(alice)
	if err != nil {
		t.Fatal(err)
	}
	funding, err := cooperative.Fund(alice)
	if err != nil {
		t.Fatal(err)
	}
	update, err := cooperative.UpdateCommitment(alice)
	if err != nil {
		t.Fatal(err)
	}
	htlcAdd, err := cooperative.AddHTLC(alice, "h1")
	if err != nil {
		t.Fatal(err)
	}
	htlcSettle, err := cooperative.SettleHTLC(bob, "h1")
	if err != nil {
		t.Fatal(err)
	}
	coopClose, err := cooperative.CooperativeClose(bob)
	if err != nil {
		t.Fatal(err)
	}

	punished := newTestChannel(t, ConfigurationClassical)
	setupAndFund(t, punished)
	if _, err := punished.UpdateCommitment(alice); err != nil {
		t.Fatal(err)
	}
	if _, err := punished.UpdateCommitment(bob); err != nil {
		t.Fatal(err)
	}
	forceClose, err := punished.ForceClose(alice, 1)
	if err != nil {
		t.Fatal(err)
	}
	penalty, err := punished.Penalty(bob)
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name       string
		transition Transition
		want       []PartyID
	}{
		{"channel_setup", setup, []PartyID{alice, bob}},
		{"funding", funding, []PartyID{alice, bob}},
		{"commitment_update", update, []PartyID{alice, bob}},
		{"htlc_add", htlcAdd, []PartyID{alice, bob}},
		{"htlc_settle", htlcSettle, []PartyID{alice, bob}},
		{"coop_close", coopClose, []PartyID{alice, bob}},
		{"force_close", forceClose, []PartyID{alice}},
		{"penalty", penalty, []PartyID{bob}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if !reflect.DeepEqual(test.transition.RequiredSigners, test.want) {
				t.Fatalf("RequiredSigners = %v, want %v", test.transition.RequiredSigners, test.want)
			}
		})
	}
}

func TestApplicationPayloadIsIdenticalAcrossConfigurations(t *testing.T) {
	expectedJSON := []byte(`{"channel_id":"c1","bal_a":50,"bal_b":50}`)
	var first ApplicationPayload
	for index, configuration := range Configurations() {
		channel := newTestChannel(t, configuration)
		if index == 0 {
			first = channel.Payload()
		} else if channel.Payload() != first {
			t.Fatalf("payload differs for %s: %+v != %+v", configuration, channel.Payload(), first)
		}
		encoded, err := json.Marshal(channel.Payload())
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(encoded, expectedJSON) {
			t.Fatalf("payload for %s = %s", configuration, encoded)
		}
	}
}

type testOnlySerializer struct{}

func (testOnlySerializer) Name() string     { return "TEST-ONLY/NON-SCIENTIFIC" }
func (testOnlySerializer) Scientific() bool { return false }
func (testOnlySerializer) SigningBytes(transition Transition) ([]byte, error) {
	return []byte(fmt.Sprintf("test:%s:%s:%d", transition.Type, transition.ChannelID, transition.StateNumber)), nil
}
func (testOnlySerializer) Serialize(authorized AuthorizedTransition) ([]byte, error) {
	return append([]byte("test-wire:"), authorized.Authorization.WireMaterial...), nil
}

type testBackend struct{ configuration Configuration }

func (b testBackend) Configuration() Configuration { return b.configuration }
func (b testBackend) PublicKey(party PartyID) ([]byte, error) {
	return []byte("test-key:" + party), nil
}
func (b testBackend) Sign(party PartyID, message []byte) ([]byte, error) {
	return append([]byte("test-signature:"+string(party)+":"), message...), nil
}
func (b testBackend) Verify(_ PartyID, _, _, _ []byte) (bool, error) { return true, nil }
func (b testBackend) Authorize(message []byte, signers []PartyID) (Authorization, error) {
	return Authorization{
		Configuration: b.configuration,
		Signers:       append([]PartyID(nil), signers...),
		WireMaterial:  append([]byte("test-authorization:"), message...),
	}, nil
}
func (b testBackend) VerifyAuthorization(_ []byte, authorization Authorization) (bool, error) {
	return authorization.Configuration == b.configuration, nil
}

func TestTestOnlySerializerCannotProduceScientificMessageBytes(t *testing.T) {
	channel := newTestChannel(t, ConfigurationUniformMLDSA)
	setupAndFund(t, channel)
	transition, err := channel.UpdateCommitment(alice)
	if err != nil {
		t.Fatal(err)
	}
	serializer := testOnlySerializer{}
	authorized, err := AuthorizeTransition(
		serializer,
		testBackend{configuration: ConfigurationUniformMLDSA},
		transition,
	)
	if err != nil {
		t.Fatalf("test plumbing authorization: %v", err)
	}
	if _, err := ScientificMessageBytes(serializer, authorized); !errors.Is(err, ErrNonScientificSerializer) {
		t.Fatalf("ScientificMessageBytes error = %v", err)
	}
}

func TestRawMeasurementRecordSupportsWarmupAndMeasuredSamples(t *testing.T) {
	warmup := RawMeasurementRecord{
		Config: ConfigurationClassical, Transition: TransitionFunding,
		Iteration: 0, Phase: SampleWarmup, RTTMS: 1.0, Success: true,
	}
	measured := RawMeasurementRecord{
		Config: ConfigurationClassical, Transition: TransitionFunding,
		Iteration: 0, Phase: SampleMeasured, MessageBytes: 123, RTTMS: 1.1, Success: true,
	}
	if warmup.Phase == measured.Phase || len(RawMeasurementFields) != 9 {
		t.Fatalf("measurement record does not distinguish phases")
	}
	wantFinalFields := []string{"config", "transition", "message_bytes", "rtt_median_ms", "rtt_p95_ms"}
	if !reflect.DeepEqual(FinalCSVFields, wantFinalFields) {
		t.Fatalf("final CSV schema changed: got %v, want %v", FinalCSVFields, wantFinalFields)
	}
}
