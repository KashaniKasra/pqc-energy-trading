package e2

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/binary"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/open-quantum-safe/liboqs-go/oqs"
)

func transitionByType(t *testing.T, configuration Configuration, kind TransitionType) Transition {
	t.Helper()
	scenario, err := BuildMeasurementScenario(configuration)
	if err != nil {
		t.Fatal(err)
	}
	for _, transition := range scenario.Transitions {
		if transition.Type == kind {
			return transition
		}
	}
	t.Fatalf("transition %q not found", kind)
	return Transition{}
}

func authorizedWithTestMaterial(t *testing.T, configuration Configuration, transition Transition) AuthorizedTransition {
	t.Helper()
	metadata, err := AlgorithmFor(configuration, transition.Type)
	if err != nil {
		t.Fatal(err)
	}
	count := len(transition.RequiredSigners)
	if configuration == ConfigurationClassical {
		count = 1
	}
	materials := make([]CryptoMaterial, count)
	for index := range materials {
		materials[index] = CryptoMaterial{
			Algorithm: metadata.Name,
			Signature: bytes.Repeat([]byte{byte(0x30 + index)}, metadata.SignatureBytes),
			PublicKey: bytes.Repeat([]byte{byte(0x60 + index)}, metadata.PublicKeyBytes),
		}
	}
	return AuthorizedTransition{
		Transition: transition,
		Authorization: Authorization{
			Configuration: configuration,
			Signers:       append([]PartyID(nil), transition.RequiredSigners...),
			Materials:     materials,
		},
	}
}

func TestPinnedLibOQSSignatureMetadata(t *testing.T) {
	tests := []struct {
		name      string
		publicKey int
		signature int
	}{
		{AlgorithmFalconPadded512, FalconPadded512PublicKeyBytes, FalconPadded512SignatureBytes},
		{AlgorithmMLDSA65, MLDSA65PublicKeyBytes, MLDSA65SignatureBytes},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var signature oqs.Signature
			if err := signature.Init(test.name, nil); err != nil {
				t.Fatalf("pinned liboqs does not provide %s: %v", test.name, err)
			}
			defer signature.Clean()
			details := signature.Details()
			if details.LengthPublicKey != test.publicKey || details.MaxLengthSignature != test.signature {
				t.Fatalf("%s details = %+v, want public key %d and signature %d", test.name, details, test.publicKey, test.signature)
			}
			publicKey, err := signature.GenerateKeyPair()
			if err != nil {
				t.Fatal(err)
			}
			signed, err := signature.Sign([]byte("e2-size-validation"))
			if err != nil {
				t.Fatal(err)
			}
			if len(publicKey) != test.publicKey || len(signed) != test.signature {
				t.Fatalf("%s emitted public key/signature = %d/%d, want %d/%d", test.name, len(publicKey), len(signed), test.publicKey, test.signature)
			}
		})
	}
	if ed25519.PublicKeySize != 32 || ed25519.SignatureSize != 64 {
		t.Fatalf("Ed25519 sizes = %d/%d", ed25519.PublicKeySize, ed25519.SignatureSize)
	}
}

func TestLayerAndAlgorithmPolicy(t *testing.T) {
	channelTransitions := map[TransitionType]bool{
		TransitionChannelSetup: true, TransitionCommitmentUpdate: true,
		TransitionHTLCAdd: true, TransitionHTLCSettle: true,
	}
	for _, transition := range finalTransitionOrder {
		layer, err := LayerForTransition(transition)
		if err != nil {
			t.Fatal(err)
		}
		wantLayer := LayerL1
		if channelTransitions[transition] {
			wantLayer = LayerChannel
		}
		if layer != wantLayer {
			t.Fatalf("%s layer = %s, want %s", transition, layer, wantLayer)
		}
		for _, configuration := range Configurations() {
			metadata, err := AlgorithmFor(configuration, transition)
			if err != nil {
				t.Fatal(err)
			}
			want := AlgorithmMLDSA65
			switch configuration {
			case ConfigurationClassical:
				want = AlgorithmEd25519Compact
			case ConfigurationLayerAware:
				if layer == LayerChannel {
					want = AlgorithmFalconPadded512
				}
			}
			if metadata.Name != want {
				t.Fatalf("%s/%s algorithm = %s, want %s", configuration, transition, metadata.Name, want)
			}
		}
	}
}

func TestCanonicalChannelID(t *testing.T) {
	got, err := CanonicalChannelID("c1")
	if err != nil {
		t.Fatal(err)
	}
	want := sha256.Sum256([]byte("c1"))
	if len(got) != 32 || got != want {
		t.Fatalf("canonical channel ID = %x, want %x", got, want)
	}
	if _, err := CanonicalChannelID(""); err == nil {
		t.Fatal("empty channel ID accepted")
	}
}

func TestCanonicalSerializationDeterminismOrderAndByteOrder(t *testing.T) {
	serializer := CanonicalSerializer{}
	transition := transitionByType(t, ConfigurationClassical, TransitionCommitmentUpdate)
	authorized := authorizedWithTestMaterial(t, ConfigurationClassical, transition)
	first, fields, accounting, err := serializer.SerializeWithBreakdown(authorized)
	if err != nil {
		t.Fatal(err)
	}
	second, _, _, err := serializer.SerializeWithBreakdown(authorized)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("same input did not serialize identically")
	}
	wantOrder := []string{
		"format_version", "transition_type", "channel_id", "state_number", "balance_a", "balance_b",
		"htlc_id", "referenced_state", "revoked_state", "superseded_by", "signature_count",
		"signature[0]", "signature[1]", "public_key_count", "public_key[0]", "public_key[1]",
	}
	gotOrder := make([]string, len(fields))
	for index, field := range fields {
		gotOrder[index] = field.Name
	}
	if !reflect.DeepEqual(gotOrder, wantOrder) {
		t.Fatalf("field order = %v, want %v", gotOrder, wantOrder)
	}
	if first[0] != CanonicalFormatVersion || first[1] != 2 {
		t.Fatalf("version/type bytes = %x", first[:2])
	}
	channelID := sha256.Sum256([]byte("c1"))
	if !bytes.Equal(first[2:34], channelID[:]) {
		t.Fatalf("serialized channel ID = %x", first[2:34])
	}
	if binary.BigEndian.Uint64(first[34:42]) != transition.StateNumber ||
		binary.BigEndian.Uint64(first[42:50]) != 50 || binary.BigEndian.Uint64(first[50:58]) != 50 {
		t.Fatal("uint64 fields are not in expected network byte order")
	}
	if binary.BigEndian.Uint16(first[114:116]) != 1 || binary.BigEndian.Uint16(first[116:118]) != 64 {
		t.Fatal("counts/lengths are not fixed-width big-endian uint16")
	}
	if accounting.SerializedTotalBytes != len(first) || sumFieldBytes(fields) != len(first) {
		t.Fatal("field table does not sum to serialized length")
	}
}

func TestFixedWidthBodyAndSameSemanticLayoutAcrossConfigurations(t *testing.T) {
	serializer := CanonicalSerializer{}
	for _, transitionType := range finalTransitionOrder {
		var first []byte
		for _, configuration := range Configurations() {
			transition := transitionByType(t, configuration, transitionType)
			body, err := serializer.SigningBytes(transition)
			if err != nil {
				t.Fatal(err)
			}
			if len(body) != 114 {
				t.Fatalf("%s body length = %d, want 114", transitionType, len(body))
			}
			if first == nil {
				first = body
			} else if !bytes.Equal(first, body) {
				t.Fatalf("non-crypto payload differs by configuration for %s", transitionType)
			}
		}
	}
	transition := transitionByType(t, ConfigurationClassical, TransitionForceClose)
	transition.StateNumber = 0x0102030405060708
	transition.ReferencedState = uint64Pointer(transition.StateNumber)
	body, err := serializer.SigningBytes(transition)
	if err != nil {
		t.Fatal(err)
	}
	if len(body) != 114 || !bytes.Equal(body[34:42], []byte{1, 2, 3, 4, 5, 6, 7, 8}) {
		t.Fatal("numeric width changed or is not big-endian")
	}
}

func TestFieldAccountingAndT0(t *testing.T) {
	serializer := CanonicalSerializer{}
	for _, configuration := range Configurations() {
		for _, transitionType := range finalTransitionOrder {
			t.Run(string(configuration)+"/"+string(transitionType), func(t *testing.T) {
				authorized := authorizedWithTestMaterial(t, configuration, transitionByType(t, configuration, transitionType))
				encoded, fields, accounting, err := serializer.SerializeWithBreakdown(authorized)
				if err != nil {
					t.Fatal(err)
				}
				wantTotal := expectedSerializedSize(t, configuration, transitionType)
				if len(encoded) != wantTotal || accounting.SerializedTotalBytes != wantTotal || accounting.T0Bytes != 126 {
					t.Fatalf("length/accounting = %d/%+v, want total %d T0 126", len(encoded), accounting, wantTotal)
				}
				if accounting.T0Bytes != accounting.SerializedTotalBytes-accounting.SignaturePayloadBytes-accounting.PublicKeyPayloadBytes {
					t.Fatal("T0 equation does not hold")
				}
				prefixBytes := 0
				for _, field := range fields {
					prefixBytes += field.PrefixBytes
				}
				if prefixBytes != 4*CryptoLengthPrefixBytes || accounting.T0Bytes != 118+prefixBytes {
					t.Fatalf("T0 did not retain crypto length prefixes: prefixes=%d T0=%d", prefixBytes, accounting.T0Bytes)
				}
				if sumFieldBytes(fields) != len(encoded) {
					t.Fatalf("field total = %d, serialized length = %d", sumFieldBytes(fields), len(encoded))
				}
			})
		}
	}
}

func expectedSerializedSize(t *testing.T, configuration Configuration, transition TransitionType) int {
	t.Helper()
	metadata, err := AlgorithmFor(configuration, transition)
	if err != nil {
		t.Fatal(err)
	}
	count := 2
	if configuration == ConfigurationClassical || transition == TransitionForceClose || transition == TransitionPenalty {
		count = 1
	}
	return 126 + count*(metadata.SignatureBytes+metadata.PublicKeyBytes)
}

func TestCanonicalCryptoSlotOccupancy(t *testing.T) {
	serializer := CanonicalSerializer{}
	tests := []struct {
		name          string
		configuration Configuration
		transition    TransitionType
		wantCount     uint16
	}{
		{"one pair classical", ConfigurationClassical, TransitionCommitmentUpdate, 1},
		{"one pair unilateral", ConfigurationUniformMLDSA, TransitionPenalty, 1},
		{"two pairs", ConfigurationLayerAware, TransitionHTLCAdd, 2},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			authorized := authorizedWithTestMaterial(t, test.configuration, transitionByType(t, test.configuration, test.transition))
			encoded, err := serializer.Serialize(authorized)
			if err != nil {
				t.Fatal(err)
			}
			signatureCount, signatures, publicKeyCount, publicKeys := decodeCryptoSlots(t, encoded)
			if signatureCount != test.wantCount || publicKeyCount != test.wantCount {
				t.Fatalf("counts = %d/%d, want %d", signatureCount, publicKeyCount, test.wantCount)
			}
			for index := 0; index < MaximumCryptoSlots; index++ {
				wantOccupied := index < int(test.wantCount)
				if (len(signatures[index]) != 0) != wantOccupied || (len(publicKeys[index]) != 0) != wantOccupied {
					t.Fatalf("slot %d occupancy signatures=%d keys=%d want occupied=%v", index+1, len(signatures[index]), len(publicKeys[index]), wantOccupied)
				}
			}
		})
	}
}

func TestActualSerializedSizeExamples(t *testing.T) {
	serializer := CanonicalSerializer{}
	tests := []struct {
		name          string
		configuration Configuration
		transition    TransitionType
		wantBytes     int
	}{
		{"classical one pair", ConfigurationClassical, TransitionCommitmentUpdate, 222},
		{"uniform ML-DSA bilateral", ConfigurationUniformMLDSA, TransitionCommitmentUpdate, 10648},
		{"uniform ML-DSA one pair", ConfigurationUniformMLDSA, TransitionPenalty, 5387},
		{"layer-aware Falcon bilateral", ConfigurationLayerAware, TransitionHTLCAdd, 3252},
		{"layer-aware ML-DSA bilateral", ConfigurationLayerAware, TransitionFunding, 10648},
		{"layer-aware ML-DSA one pair", ConfigurationLayerAware, TransitionPenalty, 5387},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			authorized := authorizedWithTestMaterial(t, test.configuration, transitionByType(t, test.configuration, test.transition))
			encoded, err := serializer.Serialize(authorized)
			if err != nil {
				t.Fatal(err)
			}
			if len(encoded) != test.wantBytes {
				t.Fatalf("actual serialized length = %d, want %d", len(encoded), test.wantBytes)
			}
		})
	}
}

func decodeCryptoSlots(t *testing.T, encoded []byte) (uint16, [MaximumCryptoSlots][]byte, uint16, [MaximumCryptoSlots][]byte) {
	t.Helper()
	offset := 114
	readUint16 := func() uint16 {
		if offset+2 > len(encoded) {
			t.Fatal("truncated crypto section")
		}
		value := binary.BigEndian.Uint16(encoded[offset : offset+2])
		offset += 2
		return value
	}
	readSlots := func() [MaximumCryptoSlots][]byte {
		var slots [MaximumCryptoSlots][]byte
		for index := range slots {
			length := int(readUint16())
			if offset+length > len(encoded) {
				t.Fatal("truncated crypto payload")
			}
			slots[index] = encoded[offset : offset+length]
			offset += length
		}
		return slots
	}
	signatureCount := readUint16()
	signatures := readSlots()
	publicKeyCount := readUint16()
	publicKeys := readSlots()
	if offset != len(encoded) {
		t.Fatalf("unparsed crypto bytes = %d", len(encoded)-offset)
	}
	return signatureCount, signatures, publicKeyCount, publicKeys
}

func TestMalformedCryptoSlotsRejected(t *testing.T) {
	occupied := []byte{1}
	tests := []struct {
		name    string
		section cryptoSection
	}{
		{"count one with signature slot two populated", cryptoSection{signatureCount: 1, signatures: [2][]byte{occupied, occupied}, publicKeyCount: 1, publicKeys: [2][]byte{occupied, nil}}},
		{"count one with public-key slot two populated", cryptoSection{signatureCount: 1, signatures: [2][]byte{occupied, nil}, publicKeyCount: 1, publicKeys: [2][]byte{occupied, occupied}}},
		{"count two with signature slot two empty", cryptoSection{signatureCount: 2, signatures: [2][]byte{occupied, nil}, publicKeyCount: 2, publicKeys: [2][]byte{occupied, occupied}}},
		{"count two with public-key slot two empty", cryptoSection{signatureCount: 2, signatures: [2][]byte{occupied, occupied}, publicKeyCount: 2, publicKeys: [2][]byte{occupied, nil}}},
		{"zero count", cryptoSection{}},
		{"count above maximum", cryptoSection{signatureCount: 3, publicKeyCount: 3}},
		{"signature gap", cryptoSection{signatureCount: 1, signatures: [2][]byte{nil, occupied}, publicKeyCount: 1, publicKeys: [2][]byte{occupied, nil}}},
		{"public-key gap", cryptoSection{signatureCount: 1, signatures: [2][]byte{occupied, nil}, publicKeyCount: 1, publicKeys: [2][]byte{nil, occupied}}},
		{"mismatched counts", cryptoSection{signatureCount: 1, signatures: [2][]byte{occupied, nil}, publicKeyCount: 2, publicKeys: [2][]byte{occupied, occupied}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var buffer bytes.Buffer
			var fields []FieldContribution
			if _, _, err := encodeCryptoSection(&buffer, &fields, test.section); err == nil {
				t.Fatal("malformed crypto-slot section accepted")
			}
		})
	}
}

func TestAllTransitionsSerializeAndMessageBytesUseActualBytes(t *testing.T) {
	serializer := CanonicalSerializer{}
	for _, configuration := range Configurations() {
		for _, transitionType := range finalTransitionOrder {
			authorized := authorizedWithTestMaterial(t, configuration, transitionByType(t, configuration, transitionType))
			encoded, err := serializer.Serialize(authorized)
			if err != nil {
				t.Fatalf("%s/%s: %v", configuration, transitionType, err)
			}
			messageBytes, err := ScientificMessageBytes(serializer, authorized)
			if err != nil {
				t.Fatal(err)
			}
			if messageBytes != len(encoded) {
				t.Fatalf("%s/%s message_bytes = %d, actual serialized length %d", configuration, transitionType, messageBytes, len(encoded))
			}
		}
	}
}

func TestScientificSerializationRejectsMalformedMaterialAndTransitions(t *testing.T) {
	serializer := CanonicalSerializer{}
	transition := transitionByType(t, ConfigurationLayerAware, TransitionHTLCAdd)
	valid := authorizedWithTestMaterial(t, ConfigurationLayerAware, transition)

	tests := []struct {
		name   string
		mutate func(*AuthorizedTransition)
	}{
		{"wrong signature length", func(value *AuthorizedTransition) {
			value.Authorization.Materials[0].Signature = value.Authorization.Materials[0].Signature[:665]
		}},
		{"wrong public-key length", func(value *AuthorizedTransition) {
			value.Authorization.Materials[0].PublicKey = value.Authorization.Materials[0].PublicKey[:896]
		}},
		{"wrong algorithm", func(value *AuthorizedTransition) { value.Authorization.Materials[0].Algorithm = AlgorithmMLDSA65 }},
		{"missing material", func(value *AuthorizedTransition) { value.Authorization.Materials = nil }},
		{"opaque material", func(value *AuthorizedTransition) { value.Authorization.WireMaterial = []byte("forbidden") }},
		{"wrong signers", func(value *AuthorizedTransition) { value.Authorization.Signers = []PartyID{alice} }},
		{"noncanonical payload", func(value *AuthorizedTransition) { value.Transition.ApplicationPayload.BalA++ }},
		{"unsupported transition", func(value *AuthorizedTransition) { value.Transition.Type = "unknown" }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			candidate := valid
			candidate.Authorization.Signers = append([]PartyID(nil), valid.Authorization.Signers...)
			candidate.Authorization.Materials = append([]CryptoMaterial(nil), valid.Authorization.Materials...)
			test.mutate(&candidate)
			if _, err := serializer.Serialize(candidate); err == nil {
				t.Fatal("malformed scientific transaction accepted")
			}
		})
	}
}

func TestFalconPaddedScientificLengthIsExact(t *testing.T) {
	serializer := CanonicalSerializer{}
	authorized := authorizedWithTestMaterial(t, ConfigurationLayerAware, transitionByType(t, ConfigurationLayerAware, TransitionHTLCSettle))
	if got := len(authorized.Authorization.Materials[0].Signature); got != 666 {
		t.Fatalf("test setup Falcon signature length = %d", got)
	}
	authorized.Authorization.Materials[0].Signature = append(authorized.Authorization.Materials[0].Signature, 0)
	if _, err := serializer.Serialize(authorized); err == nil {
		t.Fatal("667-byte Falcon-padded signature accepted")
	}
}

func TestNoFinalE2ArtifactsCreated(t *testing.T) {
	if _, err := os.Stat("../../data/e2_statemachine.csv"); !os.IsNotExist(err) {
		t.Fatalf("final E2 CSV must not be created at this checkpoint: %v", err)
	}
	if _, err := os.Stat("../../raw/e2"); !os.IsNotExist(err) {
		t.Fatalf("scientific raw E2 evidence must not be created at this checkpoint: %v", err)
	}
}

func TestUnusedFieldSentinelsAreFixedWidth(t *testing.T) {
	serializer := CanonicalSerializer{}
	setup := transitionByType(t, ConfigurationClassical, TransitionChannelSetup)
	body, err := serializer.SigningBytes(setup)
	if err != nil {
		t.Fatal(err)
	}
	for _, offset := range []int{90, 98, 106} {
		if binary.BigEndian.Uint64(body[offset:offset+8]) != math.MaxUint64 {
			t.Fatalf("unused uint64 field at %d lacks fixed sentinel", offset)
		}
	}
	if !bytes.Equal(body[58:90], make([]byte, 32)) {
		t.Fatal("unused HTLC identifier lacks fixed all-zero sentinel")
	}
}
