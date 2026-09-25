// Package e2 implements the configuration-independent E2 channel state machine.
//
// The state machine is independent of the canonical scientific transaction
// encoding and concrete cryptographic backends.
package e2

import (
	"errors"
	"fmt"
)

type Configuration string

const (
	ConfigurationClassical    Configuration = "classical"
	ConfigurationUniformMLDSA Configuration = "uniform_mldsa"
	ConfigurationLayerAware   Configuration = "layer_aware"
)

var configurations = []Configuration{
	ConfigurationClassical,
	ConfigurationUniformMLDSA,
	ConfigurationLayerAware,
}

func Configurations() []Configuration {
	return append([]Configuration(nil), configurations...)
}

func (c Configuration) valid() bool {
	for _, candidate := range configurations {
		if c == candidate {
			return true
		}
	}
	return false
}

type PartyID string

type Balances struct {
	A uint64 `json:"bal_a"`
	B uint64 `json:"bal_b"`
}

// ApplicationPayload is identical for every E2 cryptographic configuration.
type ApplicationPayload struct {
	ChannelID string `json:"channel_id"`
	BalA      uint64 `json:"bal_a"`
	BalB      uint64 `json:"bal_b"`
}

var canonicalPayload = ApplicationPayload{
	ChannelID: "c1",
	BalA:      50,
	BalB:      50,
}

func CanonicalApplicationPayload() ApplicationPayload {
	return canonicalPayload
}

type TransitionType string

const (
	TransitionChannelSetup     TransitionType = "channel_setup"
	TransitionCommitmentUpdate TransitionType = "commitment_update"
	TransitionHTLCAdd          TransitionType = "htlc_add"
	TransitionHTLCSettle       TransitionType = "htlc_settle"
	TransitionFunding          TransitionType = "funding"
	TransitionCoopClose        TransitionType = "coop_close"
	TransitionForceClose       TransitionType = "force_close"
	TransitionPenalty          TransitionType = "penalty"
)

type RevocationEvidence struct {
	RevokedState uint64
	SupersededBy uint64
}

type Transition struct {
	Type               TransitionType
	ChannelID          string
	Actor              PartyID
	Counterparty       PartyID
	StateNumber        uint64
	Balances           Balances
	HTLCID             string
	ReferencedState    *uint64
	Revocation         *RevocationEvidence
	RequiredSigners    []PartyID
	ApplicationPayload ApplicationPayload
}

type Commitment struct {
	Number    uint64
	Balances  Balances
	Revoked   bool
	RevokedBy *uint64
}

type HTLCStatus string

const (
	HTLCPending HTLCStatus = "pending"
	HTLCSettled HTLCStatus = "settled"
)

type HTLC struct {
	ID     string
	Status HTLCStatus
}

type CloseType string

const (
	CloseNone        CloseType = ""
	CloseCooperative CloseType = "cooperative"
	CloseForce       CloseType = "force"
	ClosePenalty     CloseType = "penalty"
)

type FundingState struct {
	Funded          bool
	RequiredSigners [2]PartyID
}

type forceCloseState struct {
	Broadcaster PartyID
	StateNumber uint64
}

type Channel struct {
	configuration Configuration
	parties       [2]PartyID
	payload       ApplicationPayload
	setup         bool
	funding       FundingState
	commitments   map[uint64]Commitment
	currentState  uint64
	hasCommitment bool
	htlcs         map[string]HTLC
	closeType     CloseType
	forceClose    *forceCloseState
}

var (
	ErrInvalidConfiguration    = errors.New("invalid E2 configuration")
	ErrInvalidParty            = errors.New("party is not a channel participant")
	ErrInvalidTransition       = errors.New("invalid channel transition")
	ErrNonScientificSerializer = errors.New(
		"test-only serializer cannot produce scientific E2 message_bytes",
	)
)

func NewChannel(configuration Configuration, partyA, partyB PartyID) (*Channel, error) {
	if !configuration.valid() {
		return nil, ErrInvalidConfiguration
	}
	if partyA == "" || partyB == "" || partyA == partyB {
		return nil, fmt.Errorf("%w: two distinct participants are required", ErrInvalidParty)
	}
	return &Channel{
		configuration: configuration,
		parties:       [2]PartyID{partyA, partyB},
		payload:       CanonicalApplicationPayload(),
		commitments:   make(map[uint64]Commitment),
		htlcs:         make(map[string]HTLC),
	}, nil
}

func (c *Channel) Configuration() Configuration { return c.configuration }
func (c *Channel) Payload() ApplicationPayload  { return c.payload }
func (c *Channel) CloseType() CloseType         { return c.closeType }

func (c *Channel) CurrentCommitment() (Commitment, bool) {
	if !c.hasCommitment {
		return Commitment{}, false
	}
	return cloneCommitment(c.commitments[c.currentState]), true
}

func (c *Channel) Commitment(number uint64) (Commitment, bool) {
	commitment, ok := c.commitments[number]
	return cloneCommitment(commitment), ok
}

func (c *Channel) IsRevoked(number uint64) bool {
	commitment, ok := c.commitments[number]
	return ok && commitment.Revoked
}

func (c *Channel) Setup(actor PartyID) (Transition, error) {
	if err := c.requireParticipant(actor); err != nil {
		return Transition{}, err
	}
	if c.setup {
		return Transition{}, fmt.Errorf("%w: channel is already set up", ErrInvalidTransition)
	}
	c.setup = true
	transition := c.transition(TransitionChannelSetup, actor)
	transition.RequiredSigners = c.signers()
	return transition, nil
}

func (c *Channel) Fund(actor PartyID) (Transition, error) {
	if err := c.requireOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	if !c.setup || c.funding.Funded {
		return Transition{}, fmt.Errorf("%w: setup must precede one funding", ErrInvalidTransition)
	}
	c.funding = FundingState{
		Funded:          true,
		RequiredSigners: c.parties,
	}
	c.currentState = 0
	c.hasCommitment = true
	c.commitments[0] = Commitment{Number: 0, Balances: c.balances()}
	transition := c.transition(TransitionFunding, actor)
	transition.RequiredSigners = c.signers()
	return transition, nil
}

func (c *Channel) UpdateCommitment(actor PartyID) (Transition, error) {
	if err := c.requireFundedOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	previous := c.commitments[c.currentState]
	nextNumber := c.currentState + 1
	previous.Revoked = true
	previous.RevokedBy = uint64Pointer(nextNumber)
	c.commitments[c.currentState] = previous
	c.currentState = nextNumber
	c.commitments[nextNumber] = Commitment{
		Number:   nextNumber,
		Balances: c.balances(),
	}
	transition := c.transition(TransitionCommitmentUpdate, actor)
	transition.Revocation = &RevocationEvidence{
		RevokedState: previous.Number,
		SupersededBy: nextNumber,
	}
	transition.RequiredSigners = c.signers()
	return transition, nil
}

// In this minimal E2 model, htlc_add and htlc_settle are independent measured
// transitions and intentionally do not advance or revoke the commitment sequence.
// The stale-state penalty scenario uses explicit commitment_update transitions.
func (c *Channel) AddHTLC(actor PartyID, id string) (Transition, error) {
	if err := c.requireFundedOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	if id == "" {
		return Transition{}, fmt.Errorf("%w: HTLC identifier is required", ErrInvalidTransition)
	}
	if _, exists := c.htlcs[id]; exists {
		return Transition{}, fmt.Errorf("%w: HTLC already exists", ErrInvalidTransition)
	}
	for _, htlc := range c.htlcs {
		if htlc.Status == HTLCPending {
			return Transition{}, fmt.Errorf("%w: only one pending HTLC is supported", ErrInvalidTransition)
		}
	}
	c.htlcs[id] = HTLC{ID: id, Status: HTLCPending}
	transition := c.transition(TransitionHTLCAdd, actor)
	transition.HTLCID = id
	transition.RequiredSigners = c.signers()
	return transition, nil
}

func (c *Channel) SettleHTLC(actor PartyID, id string) (Transition, error) {
	if err := c.requireFundedOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	htlc, exists := c.htlcs[id]
	if !exists || htlc.Status != HTLCPending {
		return Transition{}, fmt.Errorf("%w: HTLC is not pending", ErrInvalidTransition)
	}
	htlc.Status = HTLCSettled
	c.htlcs[id] = htlc
	transition := c.transition(TransitionHTLCSettle, actor)
	transition.HTLCID = id
	transition.RequiredSigners = c.signers()
	return transition, nil
}

func (c *Channel) CooperativeClose(actor PartyID) (Transition, error) {
	if err := c.requireFundedOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	for _, htlc := range c.htlcs {
		if htlc.Status == HTLCPending {
			return Transition{}, fmt.Errorf("%w: pending HTLC prevents cooperative close", ErrInvalidTransition)
		}
	}
	c.closeType = CloseCooperative
	transition := c.transition(TransitionCoopClose, actor)
	transition.RequiredSigners = c.signers()
	return transition, nil
}

func (c *Channel) ForceClose(actor PartyID, stateNumber uint64) (Transition, error) {
	if err := c.requireFundedOpenParticipant(actor); err != nil {
		return Transition{}, err
	}
	if _, exists := c.commitments[stateNumber]; !exists {
		return Transition{}, fmt.Errorf("%w: commitment state does not exist", ErrInvalidTransition)
	}
	c.closeType = CloseForce
	c.forceClose = &forceCloseState{Broadcaster: actor, StateNumber: stateNumber}
	transition := c.transition(TransitionForceClose, actor)
	transition.StateNumber = stateNumber
	transition.ReferencedState = uint64Pointer(stateNumber)
	transition.RequiredSigners = []PartyID{actor}
	return transition, nil
}

func (c *Channel) Penalty(actor PartyID) (Transition, error) {
	if err := c.requireParticipant(actor); err != nil {
		return Transition{}, err
	}
	if c.closeType != CloseForce || c.forceClose == nil {
		return Transition{}, fmt.Errorf("%w: penalty requires a force-close", ErrInvalidTransition)
	}
	if actor == c.forceClose.Broadcaster {
		return Transition{}, fmt.Errorf("%w: broadcaster cannot punish itself", ErrInvalidTransition)
	}
	commitment := c.commitments[c.forceClose.StateNumber]
	if !commitment.Revoked || commitment.RevokedBy == nil {
		return Transition{}, fmt.Errorf("%w: force-closed state is not revoked", ErrInvalidTransition)
	}
	evidence := RevocationEvidence{
		RevokedState: commitment.Number,
		SupersededBy: *commitment.RevokedBy,
	}
	c.closeType = ClosePenalty
	transition := c.transition(TransitionPenalty, actor)
	transition.StateNumber = commitment.Number
	transition.ReferencedState = uint64Pointer(commitment.Number)
	transition.Revocation = &evidence
	transition.RequiredSigners = []PartyID{actor}
	return transition, nil
}

func (c *Channel) transition(kind TransitionType, actor PartyID) Transition {
	stateNumber := uint64(0)
	if c.hasCommitment {
		stateNumber = c.currentState
	}
	return Transition{
		Type:               kind,
		ChannelID:          c.payload.ChannelID,
		Actor:              actor,
		Counterparty:       c.counterparty(actor),
		StateNumber:        stateNumber,
		Balances:           c.balances(),
		ApplicationPayload: c.payload,
	}
}

func (c *Channel) balances() Balances {
	return Balances{A: c.payload.BalA, B: c.payload.BalB}
}

func (c *Channel) signers() []PartyID {
	return []PartyID{c.parties[0], c.parties[1]}
}

func (c *Channel) counterparty(actor PartyID) PartyID {
	if actor == c.parties[0] {
		return c.parties[1]
	}
	if actor == c.parties[1] {
		return c.parties[0]
	}
	return ""
}

func (c *Channel) requireParticipant(actor PartyID) error {
	if actor != c.parties[0] && actor != c.parties[1] {
		return ErrInvalidParty
	}
	return nil
}

func (c *Channel) requireOpenParticipant(actor PartyID) error {
	if err := c.requireParticipant(actor); err != nil {
		return err
	}
	if c.closeType != CloseNone {
		return fmt.Errorf("%w: channel is closed", ErrInvalidTransition)
	}
	return nil
}

func (c *Channel) requireFundedOpenParticipant(actor PartyID) error {
	if err := c.requireOpenParticipant(actor); err != nil {
		return err
	}
	if !c.setup || !c.funding.Funded || !c.hasCommitment {
		return fmt.Errorf("%w: channel is not funded", ErrInvalidTransition)
	}
	return nil
}

func cloneCommitment(commitment Commitment) Commitment {
	if commitment.RevokedBy != nil {
		commitment.RevokedBy = uint64Pointer(*commitment.RevokedBy)
	}
	return commitment
}

func uint64Pointer(value uint64) *uint64 {
	copy := value
	return &copy
}

// CryptoMaterial is one raw signature/public-key pair emitted by a backend.
// Algorithm identifies the exact representation whose lengths the scientific
// serializer must validate.
type CryptoMaterial struct {
	Algorithm string
	Signature []byte
	PublicKey []byte
}

// Authorization is opaque to the state machine. Materials is the structured
// input required by the canonical scientific serializer. WireMaterial remains
// available only to non-scientific plumbing serializers used by unit tests.
type Authorization struct {
	Configuration Configuration
	Signers       []PartyID
	Materials     []CryptoMaterial
	WireMaterial  []byte
}

type CryptoBackend interface {
	Configuration() Configuration
	PublicKey(party PartyID) ([]byte, error)
	Sign(party PartyID, message []byte) ([]byte, error)
	Verify(party PartyID, message, signature, publicKey []byte) (bool, error)
	Authorize(message []byte, requiredSigners []PartyID) (Authorization, error)
	VerifyAuthorization(message []byte, authorization Authorization) (bool, error)
}

type AuthorizedTransition struct {
	Transition    Transition
	Authorization Authorization
}

// Serializer separates transition logic from its wire encoding. Scientific is
// an explicit provenance/trust marker only; it does not independently prove an
// encoding authoritative. Final measurements must register only the canonical
// professor-approved scientific serializer.
type Serializer interface {
	Name() string
	Scientific() bool
	SigningBytes(transition Transition) ([]byte, error)
	Serialize(authorized AuthorizedTransition) ([]byte, error)
}

func AuthorizeTransition(
	serializer Serializer,
	backend CryptoBackend,
	transition Transition,
) (AuthorizedTransition, error) {
	message, err := serializer.SigningBytes(transition)
	if err != nil {
		return AuthorizedTransition{}, err
	}
	authorization, err := backend.Authorize(message, transition.RequiredSigners)
	if err != nil {
		return AuthorizedTransition{}, err
	}
	if authorization.Configuration != backend.Configuration() {
		return AuthorizedTransition{}, errors.New("authorization configuration mismatch")
	}
	valid, err := backend.VerifyAuthorization(message, authorization)
	if err != nil {
		return AuthorizedTransition{}, err
	}
	if !valid {
		return AuthorizedTransition{}, errors.New("authorization verification failed")
	}
	return AuthorizedTransition{Transition: transition, Authorization: authorization}, nil
}

// ScientificMessageBytes is the only message-size entry point intended for the
// future E2 measurement harness. Test-only serializers are rejected explicitly.
func ScientificMessageBytes(
	serializer Serializer,
	authorized AuthorizedTransition,
) (int, error) {
	if !serializer.Scientific() {
		return 0, ErrNonScientificSerializer
	}
	serialized, err := serializer.Serialize(authorized)
	if err != nil {
		return 0, err
	}
	if len(serialized) == 0 {
		return 0, errors.New("scientific serializer returned no bytes")
	}
	return len(serialized), nil
}

type SamplePhase string

const (
	SampleWarmup   SamplePhase = "warmup"
	SampleMeasured SamplePhase = "measured"
)

// RawMeasurementRecord is the future raw-sample shape. It is not a final CSV
// row and includes p99-capable per-iteration timing evidence.
type RawMeasurementRecord struct {
	Config       Configuration
	Transition   TransitionType
	Iteration    uint64
	Phase        SamplePhase
	Scientific   bool
	MessageBytes int
	RTTMS        float64
	Success      bool
	ErrorMessage string
}

var RawMeasurementFields = []string{
	"config",
	"transition",
	"iteration",
	"phase",
	"scientific",
	"message_bytes",
	"rtt_ms",
	"success",
	"error",
}

// FinalCSVFields is the professor-defined E2 deliverable schema. This package
// defines the schema but does not emit final rows until the cryptographic and
// serialization blockers documented above are resolved.
var FinalCSVFields = []string{
	"config",
	"transition",
	"message_bytes",
	"rtt_median_ms",
	"rtt_p95_ms",
}
