package e2

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

const (
	CanonicalFormatVersion     uint8 = 1
	ChannelIDBytes                   = sha256.Size
	CryptoLengthPrefixBytes          = 2
	MaximumCryptoSlots               = 2
	CanonicalSemanticBodyBytes       = 114

	AlgorithmEd25519Compact  = "Ed25519-compact-aggregate-size"
	AlgorithmMLDSA65         = "ML-DSA-65"
	AlgorithmFalconPadded512 = "Falcon-padded-512"

	MLDSA65PublicKeyBytes         = 1952
	MLDSA65SignatureBytes         = 3309
	FalconPadded512PublicKeyBytes = 897
	FalconPadded512SignatureBytes = 666
)

// AlgorithmMetadata describes raw wire material. The Ed25519 entry represents
// the professor-directed MuSig2-aggregated size case; it does not claim that
// Ed25519 itself performs MuSig2 aggregation.
type AlgorithmMetadata struct {
	Name           string
	PublicKeyBytes int
	SignatureBytes int
}

var algorithmMetadata = map[string]AlgorithmMetadata{
	AlgorithmEd25519Compact: {
		Name: AlgorithmEd25519Compact, PublicKeyBytes: ed25519.PublicKeySize, SignatureBytes: ed25519.SignatureSize,
	},
	AlgorithmMLDSA65: {
		Name: AlgorithmMLDSA65, PublicKeyBytes: MLDSA65PublicKeyBytes, SignatureBytes: MLDSA65SignatureBytes,
	},
	AlgorithmFalconPadded512: {
		Name: AlgorithmFalconPadded512, PublicKeyBytes: FalconPadded512PublicKeyBytes, SignatureBytes: FalconPadded512SignatureBytes,
	},
}

func AlgorithmMetadataFor(name string) (AlgorithmMetadata, error) {
	metadata, ok := algorithmMetadata[name]
	if !ok {
		return AlgorithmMetadata{}, fmt.Errorf("unsupported E2 signature algorithm %q", name)
	}
	return metadata, nil
}

type TransitionLayer string

const (
	LayerChannel TransitionLayer = "channel"
	LayerL1      TransitionLayer = "l1"
)

// LayerForTransition reflects the E2 model: channel setup and state/HTLC
// operations are off-chain, while funding and every close/penalty operation
// settle through Layer 1.
func LayerForTransition(transition TransitionType) (TransitionLayer, error) {
	switch transition {
	case TransitionChannelSetup, TransitionCommitmentUpdate, TransitionHTLCAdd, TransitionHTLCSettle:
		return LayerChannel, nil
	case TransitionFunding, TransitionCoopClose, TransitionForceClose, TransitionPenalty:
		return LayerL1, nil
	default:
		return "", fmt.Errorf("unsupported E2 transition %q", transition)
	}
}

func AlgorithmFor(configuration Configuration, transition TransitionType) (AlgorithmMetadata, error) {
	if !configuration.valid() {
		return AlgorithmMetadata{}, ErrInvalidConfiguration
	}
	if _, err := LayerForTransition(transition); err != nil {
		return AlgorithmMetadata{}, err
	}
	name := AlgorithmMLDSA65
	switch configuration {
	case ConfigurationClassical:
		name = AlgorithmEd25519Compact
	case ConfigurationUniformMLDSA:
		name = AlgorithmMLDSA65
	case ConfigurationLayerAware:
		layer, _ := LayerForTransition(transition)
		if layer == LayerChannel {
			name = AlgorithmFalconPadded512
		}
	}
	return AlgorithmMetadataFor(name)
}

// CanonicalChannelID maps the logical identifier to exactly 32 bytes. SHA-256
// is used only as deterministic identifier canonicalization, not as signature
// work.
func CanonicalChannelID(logicalID string) ([ChannelIDBytes]byte, error) {
	if logicalID == "" {
		return [ChannelIDBytes]byte{}, errors.New("channel identifier is required")
	}
	return sha256.Sum256([]byte(logicalID)), nil
}

type FieldContribution struct {
	Name         string
	Encoding     string
	Fixed        bool
	PrefixBytes  int
	PayloadBytes int
	TotalBytes   int
}

type ByteAccounting struct {
	SerializedTotalBytes  int
	SignaturePayloadBytes int
	PublicKeyPayloadBytes int
	T0Bytes               int
}

type cryptoSection struct {
	signatureCount uint16
	signatures     [MaximumCryptoSlots][]byte
	publicKeyCount uint16
	publicKeys     [MaximumCryptoSlots][]byte
}

// CanonicalSerializer implements the professor-approved, deterministic binary
// E2 format. It uses network byte order and no variable-width integers.
type CanonicalSerializer struct{}

func (CanonicalSerializer) Name() string     { return "e2-canonical-binary-v1" }
func (CanonicalSerializer) Scientific() bool { return true }

func (serializer CanonicalSerializer) SigningBytes(transition Transition) ([]byte, error) {
	encoded, _, err := serializer.encodeBody(transition)
	return encoded, err
}

func (serializer CanonicalSerializer) Serialize(authorized AuthorizedTransition) ([]byte, error) {
	encoded, _, _, err := serializer.SerializeWithBreakdown(authorized)
	return encoded, err
}

func (serializer CanonicalSerializer) FieldTable(authorized AuthorizedTransition) ([]FieldContribution, error) {
	_, fields, _, err := serializer.SerializeWithBreakdown(authorized)
	return fields, err
}

func (serializer CanonicalSerializer) SerializeWithBreakdown(
	authorized AuthorizedTransition,
) ([]byte, []FieldContribution, ByteAccounting, error) {
	if len(authorized.Authorization.WireMaterial) != 0 {
		return nil, nil, ByteAccounting{}, errors.New("canonical serializer rejects opaque wire material")
	}
	configuration := authorized.Authorization.Configuration
	if !configuration.valid() {
		return nil, nil, ByteAccounting{}, ErrInvalidConfiguration
	}
	if !equalPartyIDs(authorized.Authorization.Signers, authorized.Transition.RequiredSigners) {
		return nil, nil, ByteAccounting{}, errors.New("authorization signers do not match transition requirements")
	}

	body, fields, err := serializer.encodeBody(authorized.Transition)
	if err != nil {
		return nil, nil, ByteAccounting{}, err
	}
	materials := authorized.Authorization.Materials
	expectedCount := len(authorized.Transition.RequiredSigners)
	if configuration == ConfigurationClassical {
		expectedCount = 1
	}
	if len(materials) != expectedCount {
		return nil, nil, ByteAccounting{}, fmt.Errorf("crypto material count = %d, want %d", len(materials), expectedCount)
	}
	if len(materials) > MaximumCryptoSlots {
		return nil, nil, ByteAccounting{}, fmt.Errorf("crypto material count exceeds %d canonical slots", MaximumCryptoSlots)
	}
	expectedAlgorithm, err := AlgorithmFor(configuration, authorized.Transition.Type)
	if err != nil {
		return nil, nil, ByteAccounting{}, err
	}
	for index, material := range materials {
		expectedSigner := authorized.Authorization.Signers[index]
		if configuration == ConfigurationClassical {
			expectedSigner = authorized.Authorization.Signers[0]
		}
		if material.Signer != expectedSigner {
			return nil, nil, ByteAccounting{}, fmt.Errorf("material %d signer = %q, want %q", index, material.Signer, expectedSigner)
		}
		if material.Algorithm != expectedAlgorithm.Name {
			return nil, nil, ByteAccounting{}, fmt.Errorf("material %d algorithm = %q, want %q", index, material.Algorithm, expectedAlgorithm.Name)
		}
		if len(material.Signature) != expectedAlgorithm.SignatureBytes {
			return nil, nil, ByteAccounting{}, fmt.Errorf("material %d signature length = %d, want %d", index, len(material.Signature), expectedAlgorithm.SignatureBytes)
		}
		if len(material.PublicKey) != expectedAlgorithm.PublicKeyBytes {
			return nil, nil, ByteAccounting{}, fmt.Errorf("material %d public-key length = %d, want %d", index, len(material.PublicKey), expectedAlgorithm.PublicKeyBytes)
		}
		if len(material.Signature) > math.MaxUint16 || len(material.PublicKey) > math.MaxUint16 {
			return nil, nil, ByteAccounting{}, fmt.Errorf("material %d exceeds uint16 length prefix", index)
		}
	}

	section, err := buildCryptoSection(materials)
	if err != nil {
		return nil, nil, ByteAccounting{}, err
	}
	buffer := bytes.NewBuffer(make([]byte, 0, len(body)))
	buffer.Write(body)
	signaturePayloadBytes, publicKeyPayloadBytes, err := encodeCryptoSection(buffer, &fields, section)
	if err != nil {
		return nil, nil, ByteAccounting{}, err
	}
	accounting := ByteAccounting{
		SerializedTotalBytes:  buffer.Len(),
		SignaturePayloadBytes: signaturePayloadBytes,
		PublicKeyPayloadBytes: publicKeyPayloadBytes,
	}
	accounting.T0Bytes = accounting.SerializedTotalBytes - accounting.SignaturePayloadBytes - accounting.PublicKeyPayloadBytes
	if sumFieldBytes(fields) != buffer.Len() {
		return nil, nil, ByteAccounting{}, errors.New("internal field accounting mismatch")
	}
	return buffer.Bytes(), fields, accounting, nil
}

func buildCryptoSection(materials []CryptoMaterial) (cryptoSection, error) {
	if len(materials) == 0 || len(materials) > MaximumCryptoSlots {
		return cryptoSection{}, fmt.Errorf("crypto material count must be 1..%d", MaximumCryptoSlots)
	}
	section := cryptoSection{
		signatureCount: uint16(len(materials)),
		publicKeyCount: uint16(len(materials)),
	}
	for index, material := range materials {
		section.signatures[index] = material.Signature
		section.publicKeys[index] = material.PublicKey
	}
	return section, nil
}

func encodeCryptoSection(
	buffer *bytes.Buffer,
	fields *[]FieldContribution,
	section cryptoSection,
) (int, int, error) {
	if err := validateCryptoSection(section); err != nil {
		return 0, 0, err
	}
	writeUint16Field(buffer, fields, "signature_count", section.signatureCount)
	signaturePayloadBytes := 0
	for index := 0; index < MaximumCryptoSlots; index++ {
		writeVariableField(buffer, fields, fmt.Sprintf("signature[%d]", index), section.signatures[index])
		signaturePayloadBytes += len(section.signatures[index])
	}
	writeUint16Field(buffer, fields, "public_key_count", section.publicKeyCount)
	publicKeyPayloadBytes := 0
	for index := 0; index < MaximumCryptoSlots; index++ {
		writeVariableField(buffer, fields, fmt.Sprintf("public_key[%d]", index), section.publicKeys[index])
		publicKeyPayloadBytes += len(section.publicKeys[index])
	}
	return signaturePayloadBytes, publicKeyPayloadBytes, nil
}

func validateCryptoSection(section cryptoSection) error {
	if section.signatureCount == 0 || section.signatureCount > MaximumCryptoSlots {
		return fmt.Errorf("signature count must be 1..%d", MaximumCryptoSlots)
	}
	if section.publicKeyCount == 0 || section.publicKeyCount > MaximumCryptoSlots {
		return fmt.Errorf("public-key count must be 1..%d", MaximumCryptoSlots)
	}
	if section.signatureCount != section.publicKeyCount {
		return errors.New("signature and public-key counts must match")
	}
	for index := 0; index < MaximumCryptoSlots; index++ {
		wantOccupied := index < int(section.signatureCount)
		if (len(section.signatures[index]) != 0) != wantOccupied {
			return fmt.Errorf("signature slot %d occupancy does not match count %d", index+1, section.signatureCount)
		}
		if (len(section.publicKeys[index]) != 0) != wantOccupied {
			return fmt.Errorf("public-key slot %d occupancy does not match count %d", index+1, section.publicKeyCount)
		}
	}
	return nil
}

func (CanonicalSerializer) encodeBody(transition Transition) ([]byte, []FieldContribution, error) {
	code, err := transitionCode(transition.Type)
	if err != nil {
		return nil, nil, err
	}
	if err := validateTransitionForSerialization(transition); err != nil {
		return nil, nil, err
	}
	channelID, err := CanonicalChannelID(transition.ChannelID)
	if err != nil {
		return nil, nil, err
	}
	htlcID := [sha256.Size]byte{}
	if transition.HTLCID != "" {
		htlcID = sha256.Sum256([]byte(transition.HTLCID))
	}
	referencedState := uint64(math.MaxUint64)
	if transition.ReferencedState != nil {
		referencedState = *transition.ReferencedState
	}
	revokedState := uint64(math.MaxUint64)
	supersededBy := uint64(math.MaxUint64)
	if transition.Revocation != nil {
		revokedState = transition.Revocation.RevokedState
		supersededBy = transition.Revocation.SupersededBy
	}

	buffer := bytes.NewBuffer(make([]byte, 0, CanonicalSemanticBodyBytes))
	fields := make([]FieldContribution, 0, 14)
	writeUint8Field(buffer, &fields, "format_version", CanonicalFormatVersion)
	writeUint8Field(buffer, &fields, "transition_type", code)
	writeFixedBytesField(buffer, &fields, "channel_id", "SHA-256 logical identifier", channelID[:])
	writeUint64Field(buffer, &fields, "state_number", transition.StateNumber)
	writeUint64Field(buffer, &fields, "balance_a", transition.Balances.A)
	writeUint64Field(buffer, &fields, "balance_b", transition.Balances.B)
	writeFixedBytesField(buffer, &fields, "htlc_id", "SHA-256 identifier or all-zero sentinel", htlcID[:])
	writeUint64Field(buffer, &fields, "referenced_state", referencedState)
	writeUint64Field(buffer, &fields, "revoked_state", revokedState)
	writeUint64Field(buffer, &fields, "superseded_by", supersededBy)
	return buffer.Bytes(), fields, nil
}

func validateTransitionForSerialization(transition Transition) error {
	if !validTransition(transition.Type) {
		return fmt.Errorf("unsupported E2 transition %q", transition.Type)
	}
	if transition.Actor == "" || transition.Counterparty == "" || transition.Actor == transition.Counterparty {
		return errors.New("transition requires distinct actor and counterparty")
	}
	if len(transition.RequiredSigners) == 0 {
		return errors.New("transition requires authorization signers")
	}
	if transition.Type == TransitionForceClose || transition.Type == TransitionPenalty {
		if len(transition.RequiredSigners) != 1 || transition.RequiredSigners[0] != transition.Actor {
			return errors.New("unilateral transition must be authorized by its actor")
		}
	} else if len(transition.RequiredSigners) != 2 || !containsParty(transition.RequiredSigners, transition.Actor) ||
		!containsParty(transition.RequiredSigners, transition.Counterparty) {
		return errors.New("bilateral transition requires both channel participants")
	}
	canonical := CanonicalApplicationPayload()
	if transition.ApplicationPayload != canonical || transition.ChannelID != canonical.ChannelID ||
		transition.Balances.A != canonical.BalA || transition.Balances.B != canonical.BalB {
		return errors.New("transition does not contain the canonical E2 application payload")
	}
	switch transition.Type {
	case TransitionHTLCAdd, TransitionHTLCSettle:
		if transition.HTLCID == "" {
			return errors.New("HTLC transition requires an identifier")
		}
	case TransitionCommitmentUpdate:
		if transition.Revocation == nil || transition.Revocation.SupersededBy != transition.StateNumber ||
			transition.Revocation.RevokedState >= transition.Revocation.SupersededBy {
			return errors.New("commitment update requires valid revocation evidence")
		}
	case TransitionForceClose:
		if transition.ReferencedState == nil || *transition.ReferencedState != transition.StateNumber {
			return errors.New("force close requires its referenced commitment state")
		}
	case TransitionPenalty:
		if transition.ReferencedState == nil || *transition.ReferencedState != transition.StateNumber || transition.Revocation == nil ||
			transition.Revocation.RevokedState != transition.StateNumber || transition.Revocation.SupersededBy <= transition.StateNumber {
			return errors.New("penalty requires stale-state revocation evidence")
		}
	}
	if transition.Type != TransitionHTLCAdd && transition.Type != TransitionHTLCSettle && transition.HTLCID != "" {
		return errors.New("non-HTLC transition contains an HTLC identifier")
	}
	if transition.Type != TransitionForceClose && transition.Type != TransitionPenalty && transition.ReferencedState != nil {
		return errors.New("transition contains an inapplicable referenced state")
	}
	if transition.Type != TransitionCommitmentUpdate && transition.Type != TransitionPenalty && transition.Revocation != nil {
		return errors.New("transition contains inapplicable revocation evidence")
	}
	if (transition.Type == TransitionChannelSetup || transition.Type == TransitionFunding) && transition.StateNumber != 0 {
		return errors.New("setup and initial funding require state zero")
	}
	return nil
}

func transitionCode(transition TransitionType) (uint8, error) {
	switch transition {
	case TransitionChannelSetup:
		return 1, nil
	case TransitionCommitmentUpdate:
		return 2, nil
	case TransitionHTLCAdd:
		return 3, nil
	case TransitionHTLCSettle:
		return 4, nil
	case TransitionFunding:
		return 5, nil
	case TransitionCoopClose:
		return 6, nil
	case TransitionForceClose:
		return 7, nil
	case TransitionPenalty:
		return 8, nil
	default:
		return 0, fmt.Errorf("unsupported E2 transition %q", transition)
	}
}

func writeUint8Field(buffer *bytes.Buffer, fields *[]FieldContribution, name string, value uint8) {
	buffer.WriteByte(value)
	*fields = append(*fields, FieldContribution{Name: name, Encoding: "uint8", Fixed: true, PayloadBytes: 1, TotalBytes: 1})
}

func writeUint16Field(buffer *bytes.Buffer, fields *[]FieldContribution, name string, value uint16) {
	var encoded [2]byte
	binary.BigEndian.PutUint16(encoded[:], value)
	buffer.Write(encoded[:])
	*fields = append(*fields, FieldContribution{Name: name, Encoding: "uint16 big-endian", Fixed: true, PayloadBytes: 2, TotalBytes: 2})
}

func writeUint64Field(buffer *bytes.Buffer, fields *[]FieldContribution, name string, value uint64) {
	var encoded [8]byte
	binary.BigEndian.PutUint64(encoded[:], value)
	buffer.Write(encoded[:])
	*fields = append(*fields, FieldContribution{Name: name, Encoding: "uint64 big-endian", Fixed: true, PayloadBytes: 8, TotalBytes: 8})
}

func writeFixedBytesField(buffer *bytes.Buffer, fields *[]FieldContribution, name, encoding string, value []byte) {
	buffer.Write(value)
	*fields = append(*fields, FieldContribution{Name: name, Encoding: encoding, Fixed: true, PayloadBytes: len(value), TotalBytes: len(value)})
}

func writeVariableField(buffer *bytes.Buffer, fields *[]FieldContribution, name string, value []byte) {
	var length [2]byte
	binary.BigEndian.PutUint16(length[:], uint16(len(value)))
	buffer.Write(length[:])
	buffer.Write(value)
	*fields = append(*fields, FieldContribution{
		Name: name, Encoding: "uint16 big-endian length + raw bytes", Fixed: false,
		PrefixBytes: CryptoLengthPrefixBytes, PayloadBytes: len(value), TotalBytes: CryptoLengthPrefixBytes + len(value),
	})
}

func sumFieldBytes(fields []FieldContribution) int {
	total := 0
	for _, field := range fields {
		total += field.TotalBytes
	}
	return total
}

func equalPartyIDs(left, right []PartyID) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func containsParty(parties []PartyID, candidate PartyID) bool {
	for _, party := range parties {
		if party == candidate {
			return true
		}
	}
	return false
}
