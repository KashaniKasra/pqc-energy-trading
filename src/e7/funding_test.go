package e7

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"path/filepath"
	"testing"
	"time"

	common "github.com/hyperledger/fabric-protos-go-apiv2/common"
	msp "github.com/hyperledger/fabric-protos-go-apiv2/msp"
	peer "github.com/hyperledger/fabric-protos-go-apiv2/peer"
	oqs "github.com/open-quantum-safe/liboqs-go/oqs"
	"google.golang.org/protobuf/proto"
)

type testIdentity struct {
	serialized []byte
	ecdsaKey   *ecdsa.PrivateKey
	oqsSigner  *oqs.Signature
}

func newTestIdentity(t *testing.T, mspID, algorithm string) testIdentity {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: mspID}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature}
	result := testIdentity{ecdsaKey: key}
	if algorithm == FundingLayerAwarePeerAlgorithm {
		signer := &oqs.Signature{}
		if err := signer.Init("ML-DSA-65", nil); err != nil {
			t.Fatal(err)
		}
		public, err := signer.GenerateKeyPair()
		if err != nil {
			t.Fatal(err)
		}
		extension, err := asn1.Marshal(pqIdentityExtension{Algorithm: "ML-DSA-65", PublicKey: public})
		if err != nil {
			t.Fatal(err)
		}
		template.ExtraExtensions = []pkix.Extension{{Id: pqIdentityExtensionOID, Value: extension}}
		result.oqsSigner = signer
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	serialized, err := proto.Marshal(&msp.SerializedIdentity{Mspid: mspID, IdBytes: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})})
	if err != nil {
		t.Fatal(err)
	}
	result.serialized = serialized
	t.Cleanup(func() {
		if result.oqsSigner != nil {
			result.oqsSigner.Clean()
		}
	})
	return result
}

func signTestIdentity(t *testing.T, identity testIdentity, message []byte) []byte {
	t.Helper()
	digest := sha256.Sum256(message)
	if identity.oqsSigner != nil {
		signature, err := identity.oqsSigner.Sign(digest[:])
		if err != nil {
			t.Fatal(err)
		}
		return signature
	}
	signature, err := ecdsa.SignASN1(rand.Reader, identity.ecdsaKey, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signature
}

func testFundingBlock(t *testing.T, key string, value []byte, peerAlgorithm string) ([]byte, string) {
	t.Helper()
	creator := newTestIdentity(t, "Org1MSP", FundingClientAlgorithm)
	invocation, _ := proto.Marshal(&peer.ChaincodeInvocationSpec{ChaincodeSpec: &peer.ChaincodeSpec{ChaincodeId: &peer.ChaincodeID{Name: FabricChaincode}, Input: &peer.ChaincodeInput{Args: [][]byte{[]byte("Set"), []byte(key), value}}}})
	proposal, _ := proto.Marshal(&peer.ChaincodeProposalPayload{Input: invocation})
	response := []byte("deterministic-proposal-response")
	endorsements := []*peer.Endorsement{}
	for _, mspID := range []string{"Org1MSP", "Org2MSP"} {
		identity := newTestIdentity(t, mspID, peerAlgorithm)
		message := append(append([]byte(nil), response...), identity.serialized...)
		endorsements = append(endorsements, &peer.Endorsement{Endorser: identity.serialized, Signature: signTestIdentity(t, identity, message)})
	}
	actionPayload, _ := proto.Marshal(&peer.ChaincodeActionPayload{ChaincodeProposalPayload: proposal, Action: &peer.ChaincodeEndorsedAction{ProposalResponsePayload: response, Endorsements: endorsements}})
	transaction, _ := proto.Marshal(&peer.Transaction{Actions: []*peer.TransactionAction{{Payload: actionPayload}}})
	nonce := []byte("deterministic-test-nonce")
	txInput := append(append([]byte(nil), nonce...), creator.serialized...)
	txDigest := sha256.Sum256(txInput)
	transactionID := fmt.Sprintf("%x", txDigest)
	channelHeader, _ := proto.Marshal(&common.ChannelHeader{Type: int32(common.HeaderType_ENDORSER_TRANSACTION), TxId: transactionID, ChannelId: FabricChannel})
	signatureHeader, _ := proto.Marshal(&common.SignatureHeader{Creator: creator.serialized, Nonce: nonce})
	payload, _ := proto.Marshal(&common.Payload{Header: &common.Header{ChannelHeader: channelHeader, SignatureHeader: signatureHeader}, Data: transaction})
	envelope, _ := proto.Marshal(&common.Envelope{Payload: payload, Signature: signTestIdentity(t, creator, payload)})
	dataHash := sha256.Sum256(envelope)
	block, _ := proto.Marshal(&common.Block{Header: &common.BlockHeader{Number: 7, DataHash: dataHash[:]}, Data: &common.BlockData{Data: [][]byte{envelope}}, Metadata: &common.BlockMetadata{Metadata: make([][]byte, int(common.BlockMetadataIndex_COMMIT_HASH)+1)}})
	decoded := &common.Block{}
	_ = proto.Unmarshal(block, decoded)
	decoded.Metadata.Metadata[common.BlockMetadataIndex_TRANSACTIONS_FILTER] = []byte{byte(peer.TxValidationCode_VALID)}
	block, _ = proto.Marshal(decoded)
	return block, transactionID
}

type fakeFundingClient struct {
	completion   FabricCompletion
	value, block []byte
	submits      int
	submitErr    error
}

func (f *fakeFundingClient) PrepareRun(context.Context, Configuration) error { return nil }
func (f *fakeFundingClient) SubmitAndAwaitCommit(context.Context, string, []byte) (FabricCompletion, error) {
	f.submits++
	return f.completion, f.submitErr
}
func (f *fakeFundingClient) ReadValue(context.Context, string) ([]byte, error)  { return f.value, nil }
func (f *fakeFundingClient) FetchBlock(context.Context, uint64) ([]byte, error) { return f.block, nil }
func (f *fakeFundingClient) Close() error                                       { return nil }

func TestFundingCommitmentAndNetworkModes(t *testing.T) {
	expectedDigests := map[Configuration]string{
		ConfigurationClassicalL2:  "334beff2565e2fe6b3835ea203c092473717b59a950a9351c870de075f820db8",
		ConfigurationLayerAwareL2: "20b55a655331ce9829682c0533701e816da36e8a0f5ea5a862125305490f7d7c",
	}
	for _, test := range []struct {
		target, network Configuration
		mode, algorithm string
	}{
		{ConfigurationClassicalL2, ConfigurationSETSReimpl, "ecdsa", FundingClassicalPeerAlgorithm},
		{ConfigurationLayerAwareL2, ConfigurationL1Only, "ml-dsa-65", FundingLayerAwarePeerAlgorithm},
	} {
		network, mode, algorithm, err := FundingNetworkConfiguration(test.target)
		if err != nil || network != test.network || mode != test.mode || algorithm != test.algorithm {
			t.Fatalf("mapping=%s %s %s err=%v", network, mode, algorithm, err)
		}
		key, first, err := FundingCommitment(test.target)
		if err != nil {
			t.Fatal(err)
		}
		_, second, _ := FundingCommitment(test.target)
		if key != "e7/funding/"+string(test.target)+"/1" || string(first) != string(second) {
			t.Fatal("funding commitment is not deterministic")
		}
		digest := sha256.Sum256(first)
		if fmt.Sprintf("%x", digest) != expectedDigests[test.target] {
			t.Fatal("funding commitment encoding diverged from finalizer")
		}
	}
}

func TestFundingTransactionAuditECDSAAndMLDSA(t *testing.T) {
	for _, algorithm := range []string{FundingClassicalPeerAlgorithm, FundingLayerAwarePeerAlgorithm} {
		t.Run(algorithm, func(t *testing.T) {
			key, value, _ := FundingCommitment(ConfigurationClassicalL2)
			block, txID := testFundingBlock(t, key, value, algorithm)
			audit, err := AuditFundingTransaction(block, txID, key, value, FundingClientAlgorithm, algorithm)
			if err != nil {
				t.Fatal(err)
			}
			if !audit.AllSignaturesValid || audit.Creator.Algorithm != FundingClientAlgorithm || len(audit.Endorsements) != 2 || audit.Endorsements[0].Algorithm != algorithm {
				t.Fatalf("audit=%+v", audit)
			}
			if !audit.TransactionIDVerified || !audit.BlockDataHashVerified || audit.Channel != FabricChannel || audit.Chaincode != FabricChaincode || audit.Function != "Set" || audit.Creator.SignedMessageSHA256 == "" || audit.Endorsements[0].SignedMessageSHA256 == "" {
				t.Fatalf("incomplete audit=%+v", audit)
			}
			if _, err := AuditFundingTransaction(block, txID, key, append(value, 'x'), FundingClientAlgorithm, algorithm); err == nil {
				t.Fatal("tampered payload accepted")
			}
		})
	}
}

func TestFabricSignatureVerifierUsesMSPHashedMessage(t *testing.T) {
	for _, algorithm := range []string{FundingClientAlgorithm, FundingLayerAwarePeerAlgorithm} {
		t.Run(algorithm, func(t *testing.T) {
			fixture := newTestIdentity(t, "Org1MSP", algorithm)
			message := []byte("proposal-response-payload || serialized-endorser")
			signature := signTestIdentity(t, fixture, message)
			identity, err := parseFabricIdentity(fixture.serialized)
			if err != nil {
				t.Fatal(err)
			}
			valid, err := verifyFabricSignature(identity, message, signature)
			if err != nil || !valid {
				t.Fatalf("valid %s MSP signature rejected: valid=%v err=%v", algorithm, valid, err)
			}
			valid, err = verifyFabricSignature(identity, append(message, '!'), signature)
			if err != nil || valid {
				t.Fatalf("altered %s MSP signed message accepted: valid=%v err=%v", algorithm, valid, err)
			}
		})
	}
}

func TestMeasureFundingOneSubmissionNoRetry(t *testing.T) {
	key, value, _ := FundingCommitment(ConfigurationClassicalL2)
	block, txID := testFundingBlock(t, key, value, FundingClassicalPeerAlgorithm)
	client := &fakeFundingClient{completion: FabricCompletion{TransactionID: txID, Committed: true, Verified: true, ValidationStatus: "VALID", ValidationCode: 0, BlockNumber: 7}, value: value, block: block}
	clockValues := []int64{1000, 2500}
	clockCalls := 0
	clock := func() (int64, error) { value := clockValues[clockCalls]; clockCalls++; return value, nil }
	result, err := MeasureFundingWithClock(context.Background(), ConfigurationClassicalL2, client, clock)
	if err != nil {
		t.Fatal(err)
	}
	if client.submits != 1 || clockCalls != 2 || result.StartMonotonicNS != 1000 || result.EndMonotonicNS != 2500 || result.FundingDurationNS != 1500 || !result.ReadbackMatched {
		t.Fatalf("result=%+v submits=%d", result, client.submits)
	}
	failing := &fakeFundingClient{submitErr: errors.New("injected")}
	if _, err := MeasureFundingWithClock(context.Background(), ConfigurationClassicalL2, failing, clockSequence(100, 200)); err == nil || failing.submits != 1 {
		t.Fatal("submission failure was retried or ignored")
	}
	invalid := &fakeFundingClient{completion: FabricCompletion{TransactionID: "tx1", Committed: true, Verified: true, ValidationStatus: "MVCC_READ_CONFLICT", ValidationCode: 11}}
	if _, err := MeasureFundingWithClock(context.Background(), ConfigurationClassicalL2, invalid, clockSequence(100, 200)); err == nil || invalid.submits != 1 {
		t.Fatal("invalid Fabric completion was accepted or retried")
	}
}

func clockSequence(values ...int64) MonotonicClock {
	index := 0
	return func() (int64, error) {
		value := values[index]
		index++
		return value, nil
	}
}

func TestFundingDirectoryRefusesCollision(t *testing.T) {
	root := t.TempDir()
	if _, err := CreateFundingRunDirectory(root, ConfigurationClassicalL2); err != nil {
		t.Fatal(err)
	}
	if _, err := CreateFundingRunDirectory(root, ConfigurationClassicalL2); err == nil {
		t.Fatal("funding evidence collision accepted")
	}
	smoke, err := CreateFundingRunDirectory(root, ConfigurationClassicalL2, FundingModeIntegrationSmoke)
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(root, "integration-smoke", string(ConfigurationClassicalL2), "run_1")
	if smoke != want {
		t.Fatalf("smoke directory=%s want=%s", smoke, want)
	}
}
