package e2

import (
	"bytes"
	"errors"
	"reflect"
	"testing"

	"github.com/open-quantum-safe/liboqs-go/oqs"
)

func newRealBackendForTest(t *testing.T, configuration Configuration) *RealCryptoBackend {
	t.Helper()
	backend, err := NewRealCryptoBackend(configuration, []PartyID{alice, bob})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(backend.Close)
	return backend
}

func TestPinnedEnvironmentMetadata(t *testing.T) {
	if got := oqs.LiboqsVersion(); got != PinnedLiboqsVersion {
		t.Fatalf("liboqs version = %q, want %q", got, PinnedLiboqsVersion)
	}
	if err := ValidatePinnedCryptoEnvironment(); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		algorithm string
		publicKey int
		signature int
	}{
		{AlgorithmEd25519Compact, 32, 64},
		{AlgorithmMLDSA65, 1952, 3309},
		{AlgorithmFalconPadded512, 897, 666},
	}
	for _, test := range tests {
		metadata, err := AlgorithmMetadataFor(test.algorithm)
		if err != nil {
			t.Fatal(err)
		}
		if metadata.PublicKeyBytes != test.publicKey || metadata.SignatureBytes != test.signature {
			t.Fatalf("%s metadata = %+v", test.algorithm, metadata)
		}
	}
}

func TestRealKeygenSignVerifyByAlgorithm(t *testing.T) {
	tests := []struct {
		name          string
		configuration Configuration
		transition    TransitionType
		wantAlgorithm string
		wantKey       int
		wantSignature int
	}{
		{"Ed25519", ConfigurationClassical, TransitionCommitmentUpdate, AlgorithmEd25519Compact, 32, 64},
		{"ML-DSA-65", ConfigurationUniformMLDSA, TransitionCommitmentUpdate, AlgorithmMLDSA65, 1952, 3309},
		{"Falcon-padded-512", ConfigurationLayerAware, TransitionHTLCAdd, AlgorithmFalconPadded512, 897, 666},
	}
	message := []byte("real-e2-signing-test")
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			backend := newRealBackendForTest(t, test.configuration)
			publicKey, err := backend.PublicKey(alice, test.transition)
			if err != nil {
				t.Fatal(err)
			}
			signature, err := backend.Sign(alice, test.transition, message)
			if err != nil {
				t.Fatal(err)
			}
			if len(publicKey) != test.wantKey || len(signature) != test.wantSignature {
				t.Fatalf("actual key/signature lengths = %d/%d, want %d/%d", len(publicKey), len(signature), test.wantKey, test.wantSignature)
			}
			metadata, err := AlgorithmFor(test.configuration, test.transition)
			if err != nil || metadata.Name != test.wantAlgorithm {
				t.Fatalf("algorithm metadata = %+v, %v", metadata, err)
			}
			valid, err := backend.Verify(alice, test.transition, message, signature, publicKey)
			if err != nil || !valid {
				t.Fatalf("real signature verification = %v, %v", valid, err)
			}
		})
	}
}

func TestParticipantKeysAreReusedWithinRunContext(t *testing.T) {
	uniform := newRealBackendForTest(t, ConfigurationUniformMLDSA)
	updateKey, err := uniform.PublicKey(alice, TransitionCommitmentUpdate)
	if err != nil {
		t.Fatal(err)
	}
	fundingKey, err := uniform.PublicKey(alice, TransitionFunding)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(updateKey, fundingKey) {
		t.Fatal("uniform ML-DSA participant key changed between transitions")
	}

	layerAware := newRealBackendForTest(t, ConfigurationLayerAware)
	falconUpdate, err := layerAware.PublicKey(alice, TransitionCommitmentUpdate)
	if err != nil {
		t.Fatal(err)
	}
	falconHTLC, err := layerAware.PublicKey(alice, TransitionHTLCAdd)
	if err != nil {
		t.Fatal(err)
	}
	mlFunding, err := layerAware.PublicKey(alice, TransitionFunding)
	if err != nil {
		t.Fatal(err)
	}
	mlClose, err := layerAware.PublicKey(alice, TransitionForceClose)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(falconUpdate, falconHTLC) || !bytes.Equal(mlFunding, mlClose) {
		t.Fatal("layer-aware participant key changed within an algorithm role")
	}
	if bytes.Equal(falconUpdate, mlFunding) {
		t.Fatal("layer-aware Falcon and ML-DSA identities unexpectedly share a public key")
	}
}

func TestRealVerificationRejectsTamperingAndWrongOwnership(t *testing.T) {
	for _, configuration := range Configurations() {
		t.Run(string(configuration), func(t *testing.T) {
			backend := newRealBackendForTest(t, configuration)
			transitionType := TransitionCommitmentUpdate
			message := []byte("bound canonical content")
			publicKey, err := backend.PublicKey(alice, transitionType)
			if err != nil {
				t.Fatal(err)
			}
			signature, err := backend.Sign(alice, transitionType, message)
			if err != nil {
				t.Fatal(err)
			}

			modifiedMessage := append([]byte(nil), message...)
			modifiedMessage[0] ^= 1
			if valid, err := backend.Verify(alice, transitionType, modifiedMessage, signature, publicKey); err != nil || valid {
				t.Fatalf("modified message verification = %v, %v", valid, err)
			}
			modifiedSignature := append([]byte(nil), signature...)
			modifiedSignature[0] ^= 1
			if valid, err := backend.Verify(alice, transitionType, message, modifiedSignature, publicKey); err != nil || valid {
				t.Fatalf("modified signature verification = %v, %v", valid, err)
			}
			wrongPublicKey, err := backend.PublicKey(bob, transitionType)
			if err != nil {
				t.Fatal(err)
			}
			if valid, err := backend.Verify(alice, transitionType, message, signature, wrongPublicKey); err != nil || valid {
				t.Fatalf("wrong public-key verification = %v, %v", valid, err)
			}
			if _, err := backend.Verify(alice, transitionType, message, signature[:len(signature)-1], publicKey); err == nil {
				t.Fatal("wrong signature length accepted")
			}
			if _, err := backend.Verify(alice, transitionType, message, signature, publicKey[:len(publicKey)-1]); err == nil {
				t.Fatal("wrong public-key length accepted")
			}
		})
	}
}

func TestSigningPreimageIsCanonicalSemanticBody(t *testing.T) {
	serializer := CanonicalSerializer{}
	transition := transitionByType(t, ConfigurationLayerAware, TransitionHTLCAdd)
	preimage, err := serializer.SigningBytes(transition)
	if err != nil {
		t.Fatal(err)
	}
	if len(preimage) != 114 {
		t.Fatalf("signing preimage length = %d, want 114", len(preimage))
	}
	changedHTLC := transition
	changedHTLC.HTLCID = "h2"
	changedPreimage, err := serializer.SigningBytes(changedHTLC)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(preimage, changedPreimage) {
		t.Fatal("HTLC identifier change did not change signing preimage")
	}
	stateChanged := transition
	stateChanged.StateNumber++
	statePreimage, err := serializer.SigningBytes(stateChanged)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(preimage, statePreimage) {
		t.Fatal("state/revocation change did not change signing preimage")
	}
}

func TestAllConfigurationsTransitionsWithRealCrypto(t *testing.T) {
	serializer := CanonicalSerializer{}
	for _, configuration := range Configurations() {
		t.Run(string(configuration), func(t *testing.T) {
			backend := newRealBackendForTest(t, configuration)
			scenario, err := BuildMeasurementScenario(configuration)
			if err != nil {
				t.Fatal(err)
			}
			for _, transition := range scenario.Transitions {
				t.Run(string(transition.Type), func(t *testing.T) {
					authorized, err := AuthorizeTransition(serializer, backend, transition)
					if err != nil {
						t.Fatal(err)
					}
					if !authorized.Authorization.Scientific {
						t.Fatal("real authorization lacks scientific provenance")
					}
					wantSigners := transition.RequiredSigners
					if configuration == ConfigurationClassical {
						wantSigners = transition.RequiredSigners[:1]
					}
					gotSigners := make([]PartyID, len(authorized.Authorization.Materials))
					for index, material := range authorized.Authorization.Materials {
						gotSigners[index] = material.Signer
					}
					if !reflect.DeepEqual(gotSigners, wantSigners) {
						t.Fatalf("material signers = %v, want %v", gotSigners, wantSigners)
					}
					encoded, fields, accounting, err := serializer.SerializeWithBreakdown(authorized)
					if err != nil {
						t.Fatal(err)
					}
					wantBytes := expectedSerializedSize(t, configuration, transition.Type)
					if len(encoded) != wantBytes || accounting.SerializedTotalBytes != wantBytes {
						t.Fatalf("actual serialized bytes = %d/%d, want %d", len(encoded), accounting.SerializedTotalBytes, wantBytes)
					}
					if accounting.T0Bytes != 126 || sumFieldBytes(fields) != len(encoded) {
						t.Fatalf("accounting = %+v, field total %d", accounting, sumFieldBytes(fields))
					}
					messageBytes, err := ScientificMessageBytes(serializer, authorized)
					if err != nil || messageBytes != len(encoded) {
						t.Fatalf("ScientificMessageBytes = %d, %v; actual=%d", messageBytes, err, len(encoded))
					}
					signatureCount, signatures, publicKeyCount, publicKeys := decodeCryptoSlots(t, encoded)
					wantCount := uint16(len(wantSigners))
					if signatureCount != wantCount || publicKeyCount != wantCount {
						t.Fatalf("serialized counts = %d/%d, want %d", signatureCount, publicKeyCount, wantCount)
					}
					if wantCount == 1 && (len(signatures[1]) != 0 || len(publicKeys[1]) != 0) {
						t.Fatal("one-pair transition populated canonical slot two")
					}
				})
			}
		})
	}
}

func TestRealAuthorizationRejectsMutatedBundle(t *testing.T) {
	backend := newRealBackendForTest(t, ConfigurationLayerAware)
	serializer := CanonicalSerializer{}
	transition := transitionByType(t, ConfigurationLayerAware, TransitionHTLCAdd)
	preimage, err := serializer.SigningBytes(transition)
	if err != nil {
		t.Fatal(err)
	}
	authorization, err := backend.Authorize(transition, preimage)
	if err != nil {
		t.Fatal(err)
	}

	wrongAlgorithm := cloneAuthorization(authorization)
	wrongAlgorithm.Materials[0].Algorithm = AlgorithmMLDSA65
	if valid, err := backend.VerifyAuthorization(transition, preimage, wrongAlgorithm); err == nil || valid {
		t.Fatalf("wrong algorithm accepted: valid=%v err=%v", valid, err)
	}

	tampered := cloneAuthorization(authorization)
	tampered.Materials[0].Signature[0] ^= 1
	if valid, err := backend.VerifyAuthorization(transition, preimage, tampered); err != nil || valid {
		t.Fatalf("tampered authorization accepted: valid=%v err=%v", valid, err)
	}

	wrongOwner := cloneAuthorization(authorization)
	wrongOwner.Materials[0].PublicKey, err = backend.PublicKey(bob, transition.Type)
	if err != nil {
		t.Fatal(err)
	}
	if valid, err := backend.VerifyAuthorization(transition, preimage, wrongOwner); err == nil || valid {
		t.Fatalf("wrong owner key accepted: valid=%v err=%v", valid, err)
	}
}

func cloneAuthorization(source Authorization) Authorization {
	clone := source
	clone.Signers = append([]PartyID(nil), source.Signers...)
	clone.Materials = make([]CryptoMaterial, len(source.Materials))
	for index, material := range source.Materials {
		clone.Materials[index] = material
		clone.Materials[index].Signature = append([]byte(nil), material.Signature...)
		clone.Materials[index].PublicKey = append([]byte(nil), material.PublicKey...)
	}
	return clone
}

func TestPenaltyAuthorizationUsesPunisherFromStaleStatePath(t *testing.T) {
	backend := newRealBackendForTest(t, ConfigurationLayerAware)
	serializer := CanonicalSerializer{}
	scenario, err := BuildMeasurementScenario(ConfigurationLayerAware)
	if err != nil {
		t.Fatal(err)
	}
	var forceClose, penalty Transition
	for _, transition := range scenario.Transitions {
		switch transition.Type {
		case TransitionForceClose:
			forceClose = transition
		case TransitionPenalty:
			penalty = transition
		}
	}
	if forceClose.Actor != alice || penalty.Actor != bob || penalty.ReferencedState == nil || *penalty.ReferencedState != 1 ||
		penalty.Revocation == nil || penalty.Revocation.RevokedState != 1 || penalty.Revocation.SupersededBy != 2 {
		t.Fatalf("invalid stale-state scenario: force=%+v penalty=%+v", forceClose, penalty)
	}
	authorized, err := AuthorizeTransition(serializer, backend, penalty)
	if err != nil {
		t.Fatal(err)
	}
	if len(authorized.Authorization.Materials) != 1 || authorized.Authorization.Materials[0].Signer != bob {
		t.Fatalf("penalty material = %+v", authorized.Authorization.Materials)
	}
}

func TestScientificProvenanceRejectsFakeBackend(t *testing.T) {
	for _, backend := range []CryptoBackend{
		testBackend{configuration: ConfigurationClassical},
		claimingTestBackend{testBackend{configuration: ConfigurationClassical}},
	} {
		_, err := RunMeasurements(
			ConfigurationClassical,
			backend,
			CanonicalSerializer{},
			deterministicExecutor{},
			RunOptions{MeasuredIterations: MinimumScientificIterations, Scientific: true},
		)
		if !errors.Is(err, ErrNonScientificBackend) {
			t.Fatalf("fake scientific backend error = %v", err)
		}
	}
}

type claimingTestBackend struct{ testBackend }

func (claimingTestBackend) Scientific() bool { return true }

func TestScientificRunnerRejectsUnapprovedExecutor(t *testing.T) {
	backend := newRealBackendForTest(t, ConfigurationClassical)
	for _, executor := range []TransitionExecutor{deterministicExecutor{}, claimingTestExecutor{}} {
		_, err := RunMeasurements(
			ConfigurationClassical,
			backend,
			CanonicalSerializer{},
			executor,
			RunOptions{MeasuredIterations: MinimumScientificIterations, Scientific: true},
		)
		if !errors.Is(err, ErrNonScientificExecutor) {
			t.Fatalf("unapproved scientific executor error = %v", err)
		}
	}
}

type claimingTestExecutor struct{}

func (claimingTestExecutor) Scientific() bool { return true }
func (claimingTestExecutor) Execute(PreparedTransition) (float64, error) {
	return 1, nil
}

func TestBackendCleanupIsIdempotentAndDisablesUse(t *testing.T) {
	for iteration := 0; iteration < 4; iteration++ {
		backend, err := NewRealCryptoBackend(ConfigurationLayerAware, []PartyID{alice, bob})
		if err != nil {
			t.Fatal(err)
		}
		backend.Close()
		backend.Close()
		if _, err := backend.Sign(alice, TransitionHTLCAdd, []byte("after close")); err == nil {
			t.Fatal("closed backend accepted signing")
		}
	}
}
