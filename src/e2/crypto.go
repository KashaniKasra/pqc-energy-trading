package e2

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"fmt"
	"sync"

	"github.com/open-quantum-safe/liboqs-go/oqs"
)

const (
	PinnedLiboqsVersion = "0.15.0"
	PinnedLiboqsCommit  = "97f6b86b1b6d109cfd43cf276ae39c2e776aed80"
)

type cryptoIdentity struct {
	publicKey []byte
	edPrivate ed25519.PrivateKey
	oqsSigner *oqs.Signature
}

// RealCryptoBackend owns reusable participant identities for one E2
// configuration. Key generation happens once when the context is constructed,
// outside transition execution and any future RTT timing interval.
type RealCryptoBackend struct {
	mu            sync.Mutex
	configuration Configuration
	parties       []PartyID
	identities    map[string]map[PartyID]*cryptoIdentity
	closed        bool
}

func NewRealCryptoBackend(configuration Configuration, parties []PartyID) (*RealCryptoBackend, error) {
	if !configuration.valid() {
		return nil, ErrInvalidConfiguration
	}
	if len(parties) != 2 || parties[0] == "" || parties[1] == "" || parties[0] == parties[1] {
		return nil, errors.New("real E2 backend requires two distinct participants")
	}
	if err := ValidatePinnedCryptoEnvironment(); err != nil {
		return nil, err
	}
	backend := &RealCryptoBackend{
		configuration: configuration,
		parties:       append([]PartyID(nil), parties...),
		identities:    make(map[string]map[PartyID]*cryptoIdentity),
	}
	for _, algorithm := range algorithmsForConfiguration(configuration) {
		backend.identities[algorithm] = make(map[PartyID]*cryptoIdentity, len(parties))
		for _, party := range parties {
			identity, err := generateIdentity(algorithm)
			if err != nil {
				backend.Close()
				return nil, fmt.Errorf("generate %s identity for %s: %w", algorithm, party, err)
			}
			backend.identities[algorithm][party] = identity
		}
	}
	return backend, nil
}

func ValidatePinnedCryptoEnvironment() error {
	if oqs.LiboqsVersion() != PinnedLiboqsVersion {
		return fmt.Errorf("liboqs version = %q, want %q", oqs.LiboqsVersion(), PinnedLiboqsVersion)
	}
	if ed25519.PublicKeySize != 32 || ed25519.SignatureSize != 64 {
		return fmt.Errorf("Ed25519 sizes = public key %d signature %d, want 32/64", ed25519.PublicKeySize, ed25519.SignatureSize)
	}
	for _, algorithm := range []string{AlgorithmMLDSA65, AlgorithmFalconPadded512} {
		metadata, err := AlgorithmMetadataFor(algorithm)
		if err != nil {
			return err
		}
		var signer oqs.Signature
		if err := signer.Init(algorithm, nil); err != nil {
			return fmt.Errorf("initialize pinned %s: %w", algorithm, err)
		}
		details := signer.Details()
		signer.Clean()
		if details.LengthPublicKey != metadata.PublicKeyBytes || details.MaxLengthSignature != metadata.SignatureBytes {
			return fmt.Errorf(
				"%s metadata = public key %d signature %d, want %d/%d",
				algorithm,
				details.LengthPublicKey,
				details.MaxLengthSignature,
				metadata.PublicKeyBytes,
				metadata.SignatureBytes,
			)
		}
	}
	return nil
}

func algorithmsForConfiguration(configuration Configuration) []string {
	switch configuration {
	case ConfigurationClassical:
		return []string{AlgorithmEd25519Compact}
	case ConfigurationUniformMLDSA:
		return []string{AlgorithmMLDSA65}
	case ConfigurationLayerAware:
		return []string{AlgorithmFalconPadded512, AlgorithmMLDSA65}
	default:
		return nil
	}
}

func generateIdentity(algorithm string) (*cryptoIdentity, error) {
	metadata, err := AlgorithmMetadataFor(algorithm)
	if err != nil {
		return nil, err
	}
	if algorithm == AlgorithmEd25519Compact {
		publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return nil, err
		}
		if len(publicKey) != metadata.PublicKeyBytes || len(privateKey) != ed25519.PrivateKeySize {
			return nil, errors.New("generated Ed25519 key has unexpected length")
		}
		return &cryptoIdentity{
			publicKey: publicKey,
			edPrivate: privateKey,
		}, nil
	}
	var signer oqs.Signature
	if err := signer.Init(algorithm, nil); err != nil {
		return nil, err
	}
	publicKey, err := signer.GenerateKeyPair()
	if err != nil {
		signer.Clean()
		return nil, err
	}
	if len(publicKey) != metadata.PublicKeyBytes {
		signer.Clean()
		return nil, fmt.Errorf("generated %s public key length = %d, want %d", algorithm, len(publicKey), metadata.PublicKeyBytes)
	}
	return &cryptoIdentity{publicKey: append([]byte(nil), publicKey...), oqsSigner: &signer}, nil
}

func (backend *RealCryptoBackend) Scientific() bool { return true }

func (backend *RealCryptoBackend) scientificBackendMarker() {}

func (backend *RealCryptoBackend) Configuration() Configuration {
	return backend.configuration
}

func (backend *RealCryptoBackend) PublicKey(party PartyID, transition TransitionType) ([]byte, error) {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	identity, _, err := backend.identityLocked(party, transition)
	if err != nil {
		return nil, err
	}
	return append([]byte(nil), identity.publicKey...), nil
}

func (backend *RealCryptoBackend) Sign(party PartyID, transition TransitionType, message []byte) ([]byte, error) {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if len(message) == 0 {
		return nil, errors.New("signing preimage is empty")
	}
	identity, metadata, err := backend.identityLocked(party, transition)
	if err != nil {
		return nil, err
	}
	var signature []byte
	if metadata.Name == AlgorithmEd25519Compact {
		signature = ed25519.Sign(identity.edPrivate, message)
	} else {
		signature, err = identity.oqsSigner.Sign(message)
		if err != nil {
			return nil, err
		}
	}
	if len(signature) != metadata.SignatureBytes {
		return nil, fmt.Errorf("%s signature length = %d, want %d", metadata.Name, len(signature), metadata.SignatureBytes)
	}
	return append([]byte(nil), signature...), nil
}

func (backend *RealCryptoBackend) Verify(
	party PartyID,
	transition TransitionType,
	message, signature, publicKey []byte,
) (bool, error) {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if len(message) == 0 {
		return false, errors.New("verification preimage is empty")
	}
	identity, metadata, err := backend.identityLocked(party, transition)
	if err != nil {
		return false, err
	}
	if len(signature) != metadata.SignatureBytes {
		return false, fmt.Errorf("%s signature length = %d, want %d", metadata.Name, len(signature), metadata.SignatureBytes)
	}
	if len(publicKey) != metadata.PublicKeyBytes {
		return false, fmt.Errorf("%s public-key length = %d, want %d", metadata.Name, len(publicKey), metadata.PublicKeyBytes)
	}
	if metadata.Name == AlgorithmEd25519Compact {
		return ed25519.Verify(ed25519.PublicKey(publicKey), message, signature), nil
	}
	return identity.oqsSigner.Verify(message, signature, publicKey)
}

func (backend *RealCryptoBackend) Authorize(transition Transition, message []byte) (Authorization, error) {
	materialSigners, err := backend.materialSigners(transition)
	if err != nil {
		return Authorization{}, err
	}
	metadata, err := AlgorithmFor(backend.configuration, transition.Type)
	if err != nil {
		return Authorization{}, err
	}
	materials := make([]CryptoMaterial, 0, len(materialSigners))
	for _, party := range materialSigners {
		publicKey, err := backend.PublicKey(party, transition.Type)
		if err != nil {
			return Authorization{}, err
		}
		signature, err := backend.Sign(party, transition.Type, message)
		if err != nil {
			return Authorization{}, err
		}
		valid, err := backend.Verify(party, transition.Type, message, signature, publicKey)
		if err != nil {
			return Authorization{}, err
		}
		if !valid {
			return Authorization{}, fmt.Errorf("fresh %s signature from %s did not verify", metadata.Name, party)
		}
		materials = append(materials, CryptoMaterial{
			Algorithm: metadata.Name,
			Signer:    party,
			Signature: signature,
			PublicKey: publicKey,
		})
	}
	return Authorization{
		Configuration: backend.configuration,
		Signers:       append([]PartyID(nil), transition.RequiredSigners...),
		Materials:     materials,
		Scientific:    true,
	}, nil
}

func (backend *RealCryptoBackend) VerifyAuthorization(
	transition Transition,
	message []byte,
	authorization Authorization,
) (bool, error) {
	if authorization.Configuration != backend.configuration || !authorization.Scientific {
		return false, errors.New("authorization configuration or provenance mismatch")
	}
	if len(authorization.WireMaterial) != 0 || !equalPartyIDs(authorization.Signers, transition.RequiredSigners) {
		return false, errors.New("authorization signers or material representation mismatch")
	}
	materialSigners, err := backend.materialSigners(transition)
	if err != nil {
		return false, err
	}
	if len(authorization.Materials) != len(materialSigners) {
		return false, fmt.Errorf("authorization material count = %d, want %d", len(authorization.Materials), len(materialSigners))
	}
	metadata, err := AlgorithmFor(backend.configuration, transition.Type)
	if err != nil {
		return false, err
	}
	for index, material := range authorization.Materials {
		if material.Signer != materialSigners[index] || material.Algorithm != metadata.Name {
			return false, fmt.Errorf("authorization material %d signer or algorithm mismatch", index)
		}
		registeredKey, err := backend.PublicKey(material.Signer, transition.Type)
		if err != nil {
			return false, err
		}
		if !bytes.Equal(registeredKey, material.PublicKey) {
			return false, fmt.Errorf("authorization material %d public key is not owned by %s", index, material.Signer)
		}
		valid, err := backend.Verify(material.Signer, transition.Type, message, material.Signature, material.PublicKey)
		if err != nil {
			return false, err
		}
		if !valid {
			return false, nil
		}
	}
	return true, nil
}

func (backend *RealCryptoBackend) materialSigners(transition Transition) ([]PartyID, error) {
	if err := validateTransitionForSerialization(transition); err != nil {
		return nil, err
	}
	if backend.configuration == ConfigurationClassical {
		// One real Ed25519 signer is the compact size representative. This is
		// not a MuSig2 protocol execution.
		return []PartyID{transition.RequiredSigners[0]}, nil
	}
	return append([]PartyID(nil), transition.RequiredSigners...), nil
}

func (backend *RealCryptoBackend) identityLocked(
	party PartyID,
	transition TransitionType,
) (*cryptoIdentity, AlgorithmMetadata, error) {
	if backend.closed {
		return nil, AlgorithmMetadata{}, errors.New("real E2 crypto backend is closed")
	}
	metadata, err := AlgorithmFor(backend.configuration, transition)
	if err != nil {
		return nil, AlgorithmMetadata{}, err
	}
	identities := backend.identities[metadata.Name]
	identity := identities[party]
	if identity == nil {
		return nil, AlgorithmMetadata{}, fmt.Errorf("no %s identity for participant %q", metadata.Name, party)
	}
	return identity, metadata, nil
}

// Close releases liboqs native objects and clears private key material. It is
// idempotent and must be called once the measurement context is no longer used.
func (backend *RealCryptoBackend) Close() {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if backend.closed {
		return
	}
	for _, byParty := range backend.identities {
		for _, identity := range byParty {
			if identity.oqsSigner != nil {
				identity.oqsSigner.Clean()
				identity.oqsSigner = nil
			}
			for index := range identity.edPrivate {
				identity.edPrivate[index] = 0
			}
			identity.edPrivate = nil
		}
	}
	backend.closed = true
}
