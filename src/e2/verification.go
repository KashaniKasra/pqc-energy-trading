package e2

import (
	"bytes"
	"crypto/ed25519"
	"encoding/binary"
	"errors"
	"fmt"

	"github.com/open-quantum-safe/liboqs-go/oqs"
)

// CanonicalTransactionVerifier is node B's reusable verification context.
// Initialization is outside RTT timing; Verify performs every cryptographic
// verification actually represented by the received canonical transaction.
type CanonicalTransactionVerifier struct {
	configuration Configuration
	transition    Transition
	preimage      []byte
	metadata      AlgorithmMetadata
	materialCount int
	oqsVerifier   *oqs.Signature
}

func NewCanonicalTransactionVerifier(
	configuration Configuration,
	transition Transition,
) (*CanonicalTransactionVerifier, error) {
	if !configuration.valid() {
		return nil, ErrInvalidConfiguration
	}
	preimage, err := (CanonicalSerializer{}).SigningBytes(transition)
	if err != nil {
		return nil, err
	}
	metadata, err := AlgorithmFor(configuration, transition.Type)
	if err != nil {
		return nil, err
	}
	count := len(transition.RequiredSigners)
	if configuration == ConfigurationClassical {
		count = 1
	}
	verifier := &CanonicalTransactionVerifier{
		configuration: configuration,
		transition:    transition,
		preimage:      preimage,
		metadata:      metadata,
		materialCount: count,
	}
	if metadata.Name != AlgorithmEd25519Compact {
		var signature oqs.Signature
		if err := signature.Init(metadata.Name, nil); err != nil {
			return nil, fmt.Errorf("initialize %s verifier: %w", metadata.Name, err)
		}
		verifier.oqsVerifier = &signature
	}
	return verifier, nil
}

func (verifier *CanonicalTransactionVerifier) Close() {
	if verifier.oqsVerifier != nil {
		verifier.oqsVerifier.Clean()
		verifier.oqsVerifier = nil
	}
}

func (verifier *CanonicalTransactionVerifier) Verify(serialized []byte) error {
	if len(serialized) < CanonicalSemanticBodyBytes {
		return errors.New("canonical transaction is truncated before semantic body")
	}
	if !bytes.Equal(serialized[:CanonicalSemanticBodyBytes], verifier.preimage) {
		return errors.New("canonical semantic body does not match expected transition")
	}
	signatures, publicKeys, err := parseCanonicalCryptoSection(serialized[CanonicalSemanticBodyBytes:])
	if err != nil {
		return err
	}
	if len(signatures) != verifier.materialCount || len(publicKeys) != verifier.materialCount {
		return fmt.Errorf(
			"canonical material count = %d/%d, want %d",
			len(signatures), len(publicKeys), verifier.materialCount,
		)
	}
	for index := range signatures {
		if len(signatures[index]) != verifier.metadata.SignatureBytes {
			return fmt.Errorf("signature %d length = %d, want %d", index, len(signatures[index]), verifier.metadata.SignatureBytes)
		}
		if len(publicKeys[index]) != verifier.metadata.PublicKeyBytes {
			return fmt.Errorf("public key %d length = %d, want %d", index, len(publicKeys[index]), verifier.metadata.PublicKeyBytes)
		}
		var valid bool
		if verifier.metadata.Name == AlgorithmEd25519Compact {
			valid = ed25519.Verify(ed25519.PublicKey(publicKeys[index]), verifier.preimage, signatures[index])
		} else {
			if verifier.oqsVerifier == nil {
				return errors.New("post-quantum verifier is closed")
			}
			valid, err = verifier.oqsVerifier.Verify(verifier.preimage, signatures[index], publicKeys[index])
			if err != nil {
				return fmt.Errorf("verify %s material %d: %w", verifier.metadata.Name, index, err)
			}
		}
		if !valid {
			return fmt.Errorf("%s signature %d verification failed", verifier.metadata.Name, index)
		}
	}
	return nil
}

func parseCanonicalCryptoSection(encoded []byte) ([][]byte, [][]byte, error) {
	offset := 0
	readUint16 := func() (uint16, error) {
		if len(encoded)-offset < 2 {
			return 0, errors.New("canonical crypto section is truncated")
		}
		value := binary.BigEndian.Uint16(encoded[offset : offset+2])
		offset += 2
		return value, nil
	}
	readSlots := func(count uint16, kind string) ([][]byte, error) {
		if count == 0 || count > MaximumCryptoSlots {
			return nil, fmt.Errorf("%s count must be 1..%d", kind, MaximumCryptoSlots)
		}
		values := make([][]byte, 0, count)
		for slot := 0; slot < MaximumCryptoSlots; slot++ {
			length, err := readUint16()
			if err != nil {
				return nil, err
			}
			occupied := slot < int(count)
			if occupied != (length != 0) {
				return nil, fmt.Errorf("%s slot %d occupancy disagrees with count", kind, slot+1)
			}
			if int(length) > len(encoded)-offset {
				return nil, fmt.Errorf("%s slot %d payload is truncated", kind, slot+1)
			}
			if occupied {
				values = append(values, append([]byte(nil), encoded[offset:offset+int(length)]...))
			}
			offset += int(length)
		}
		return values, nil
	}
	signatureCount, err := readUint16()
	if err != nil {
		return nil, nil, err
	}
	signatures, err := readSlots(signatureCount, "signature")
	if err != nil {
		return nil, nil, err
	}
	publicKeyCount, err := readUint16()
	if err != nil {
		return nil, nil, err
	}
	if publicKeyCount != signatureCount {
		return nil, nil, errors.New("signature and public-key counts differ")
	}
	publicKeys, err := readSlots(publicKeyCount, "public key")
	if err != nil {
		return nil, nil, err
	}
	if offset != len(encoded) {
		return nil, nil, errors.New("canonical transaction has trailing bytes")
	}
	return signatures, publicKeys, nil
}
