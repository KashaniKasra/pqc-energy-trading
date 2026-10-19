package e7

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"crypto/x509"
	"encoding/asn1"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"sort"

	common "github.com/hyperledger/fabric-protos-go-apiv2/common"
	msp "github.com/hyperledger/fabric-protos-go-apiv2/msp"
	peer "github.com/hyperledger/fabric-protos-go-apiv2/peer"
	oqs "github.com/open-quantum-safe/liboqs-go/oqs"
	"google.golang.org/protobuf/proto"
)

var pqIdentityExtensionOID = asn1.ObjectIdentifier{1, 3, 6, 1, 3, 9999, 1}

type pqIdentityExtension struct {
	Algorithm string
	PublicKey []byte
}

type FundingIdentityAudit struct {
	MSPID               string `json:"msp_id"`
	CertificateSHA256   string `json:"certificate_sha256"`
	Algorithm           string `json:"algorithm"`
	SignedMessageSHA256 string `json:"signed_message_sha256"`
	SignatureSHA256     string `json:"signature_sha256"`
	SignatureVerified   bool   `json:"signature_verified"`
}

type FundingTransactionAudit struct {
	Schema                string                 `json:"schema"`
	TransactionID         string                 `json:"transaction_id"`
	TransactionIDVerified bool                   `json:"transaction_id_verified"`
	BlockNumber           uint64                 `json:"block_number"`
	BlockSHA256           string                 `json:"block_sha256"`
	BlockDataHashVerified bool                   `json:"block_data_hash_verified"`
	Channel               string                 `json:"channel"`
	Chaincode             string                 `json:"chaincode"`
	Function              string                 `json:"function"`
	ValidationStatus      string                 `json:"validation_status"`
	ValidationCode        int                    `json:"validation_code"`
	CommitmentKey         string                 `json:"commitment_key"`
	CommitmentSHA256      string                 `json:"commitment_sha256"`
	Creator               FundingIdentityAudit   `json:"gateway_client_creator"`
	Endorsements          []FundingIdentityAudit `json:"peer_endorsements"`
	RequiredMSPIDs        []string               `json:"required_endorsement_msp_ids"`
	AllSignaturesValid    bool                   `json:"all_signatures_verified"`
}

type parsedFabricIdentity struct {
	MSPID       string
	Certificate *x509.Certificate
	Algorithm   string
	PublicKey   []byte
}

func parseFabricIdentity(serialized []byte) (parsedFabricIdentity, error) {
	identity := &msp.SerializedIdentity{}
	if err := proto.Unmarshal(serialized, identity); err != nil {
		return parsedFabricIdentity{}, fmt.Errorf("decode Fabric serialized identity: %w", err)
	}
	block, rest := pem.Decode(identity.IdBytes)
	if block == nil || block.Type != "CERTIFICATE" || len(rest) != 0 {
		return parsedFabricIdentity{}, errors.New("Fabric identity must contain exactly one certificate")
	}
	certificate, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return parsedFabricIdentity{}, err
	}
	for _, extension := range certificate.Extensions {
		if extension.Id.Equal(pqIdentityExtensionOID) {
			var pq pqIdentityExtension
			rest, err := asn1.Unmarshal(extension.Value, &pq)
			if err != nil || len(rest) != 0 || pq.Algorithm != "ML-DSA-65" || len(pq.PublicKey) == 0 {
				return parsedFabricIdentity{}, errors.New("invalid ML-DSA-65 Fabric identity extension")
			}
			return parsedFabricIdentity{MSPID: identity.Mspid, Certificate: certificate, Algorithm: FundingLayerAwarePeerAlgorithm, PublicKey: pq.PublicKey}, nil
		}
	}
	public, ok := certificate.PublicKey.(*ecdsa.PublicKey)
	if !ok || public.Curve != elliptic.P256() {
		return parsedFabricIdentity{}, errors.New("Fabric identity is neither ML-DSA-65 nor ECDSA P-256")
	}
	return parsedFabricIdentity{MSPID: identity.Mspid, Certificate: certificate, Algorithm: FundingClientAlgorithm}, nil
}

func verifyFabricSignature(identity parsedFabricIdentity, message, signature []byte) (bool, error) {
	digest := sha256.Sum256(message)
	switch identity.Algorithm {
	case FundingClientAlgorithm:
		public, ok := identity.Certificate.PublicKey.(*ecdsa.PublicKey)
		if !ok {
			return false, errors.New("ECDSA identity public key is invalid")
		}
		return ecdsa.VerifyASN1(public, digest[:], signature), nil
	case FundingLayerAwarePeerAlgorithm:
		var verifier oqs.Signature
		if err := verifier.Init("ML-DSA-65", nil); err != nil {
			return false, err
		}
		defer verifier.Clean()
		return verifier.Verify(digest[:], signature, identity.PublicKey)
	default:
		return false, fmt.Errorf("unsupported Fabric signature algorithm %q", identity.Algorithm)
	}
}

func identityAudit(identity parsedFabricIdentity, message, signature []byte, valid bool) FundingIdentityAudit {
	certificateDigest := sha256.Sum256(identity.Certificate.Raw)
	messageDigest := sha256.Sum256(message)
	signatureDigest := sha256.Sum256(signature)
	return FundingIdentityAudit{
		MSPID: identity.MSPID, CertificateSHA256: hex.EncodeToString(certificateDigest[:]),
		Algorithm: identity.Algorithm, SignedMessageSHA256: hex.EncodeToString(messageDigest[:]),
		SignatureSHA256: hex.EncodeToString(signatureDigest[:]), SignatureVerified: valid,
	}
}

// AuditFundingTransaction locates one exact transaction in a committed block,
// validates its simplekv.Set arguments, and cryptographically verifies the
// ECDSA Gateway envelope signature and every peer endorsement. ML-DSA peers are
// identified by and verified from the experimental E1 X.509 extension.
func AuditFundingTransaction(blockBytes []byte, transactionID, key string, payload []byte, expectedClientAlgorithm, expectedPeerAlgorithm string) (FundingTransactionAudit, error) {
	if transactionID == "" || key == "" || len(payload) == 0 {
		return FundingTransactionAudit{}, errors.New("funding audit inputs are incomplete")
	}
	block := &common.Block{}
	if err := proto.Unmarshal(blockBytes, block); err != nil {
		return FundingTransactionAudit{}, fmt.Errorf("decode block: %w", err)
	}
	if block.Header == nil || block.Data == nil || block.Metadata == nil || len(block.Metadata.Metadata) <= int(common.BlockMetadataIndex_TRANSACTIONS_FILTER) {
		return FundingTransactionAudit{}, errors.New("funding block is incomplete")
	}
	computedDataHash := sha256.Sum256(bytes.Join(block.Data.Data, nil))
	if !bytes.Equal(block.Header.DataHash, computedDataHash[:]) {
		return FundingTransactionAudit{}, errors.New("funding block data hash mismatch")
	}
	validation := block.Metadata.Metadata[common.BlockMetadataIndex_TRANSACTIONS_FILTER]
	if len(validation) != len(block.Data.Data) {
		return FundingTransactionAudit{}, errors.New("block validation filter length mismatch")
	}
	match := -1
	var envelope *common.Envelope
	var parsedPayload *common.Payload
	for index, raw := range block.Data.Data {
		candidate := &common.Envelope{}
		if err := proto.Unmarshal(raw, candidate); err != nil {
			return FundingTransactionAudit{}, err
		}
		candidatePayload := &common.Payload{}
		if err := proto.Unmarshal(candidate.Payload, candidatePayload); err != nil || candidatePayload.Header == nil {
			return FundingTransactionAudit{}, errors.New("malformed block envelope payload")
		}
		channelHeader := &common.ChannelHeader{}
		if err := proto.Unmarshal(candidatePayload.Header.ChannelHeader, channelHeader); err != nil {
			return FundingTransactionAudit{}, err
		}
		if channelHeader.TxId == transactionID {
			if channelHeader.Type != int32(common.HeaderType_ENDORSER_TRANSACTION) || channelHeader.ChannelId != FabricChannel {
				return FundingTransactionAudit{}, errors.New("funding transaction channel/type mismatch")
			}
			if match != -1 {
				return FundingTransactionAudit{}, errors.New("duplicate transaction ID in funding block")
			}
			match, envelope, parsedPayload = index, candidate, candidatePayload
		}
	}
	if match < 0 {
		return FundingTransactionAudit{}, errors.New("funding transaction not found in fetched block")
	}
	if validation[match] != byte(peer.TxValidationCode_VALID) {
		return FundingTransactionAudit{}, errors.New("funding transaction is not VALID in block metadata")
	}

	signatureHeader := &common.SignatureHeader{}
	if err := proto.Unmarshal(parsedPayload.Header.SignatureHeader, signatureHeader); err != nil {
		return FundingTransactionAudit{}, err
	}
	txIDInput := append(append([]byte(nil), signatureHeader.Nonce...), signatureHeader.Creator...)
	computedTxID := sha256.Sum256(txIDInput)
	if hex.EncodeToString(computedTxID[:]) != transactionID {
		return FundingTransactionAudit{}, errors.New("funding transaction ID does not match nonce and creator")
	}
	creator, err := parseFabricIdentity(signatureHeader.Creator)
	if err != nil {
		return FundingTransactionAudit{}, err
	}
	creatorValid, err := verifyFabricSignature(creator, envelope.Payload, envelope.Signature)
	if err != nil || !creatorValid || creator.Algorithm != expectedClientAlgorithm {
		return FundingTransactionAudit{}, errors.New("Gateway client creator signature/algorithm verification failed")
	}

	transaction := &peer.Transaction{}
	if err := proto.Unmarshal(parsedPayload.Data, transaction); err != nil {
		return FundingTransactionAudit{}, err
	}
	if len(transaction.Actions) != 1 {
		return FundingTransactionAudit{}, errors.New("funding transaction must contain exactly one action")
	}
	actionPayload := &peer.ChaincodeActionPayload{}
	if err := proto.Unmarshal(transaction.Actions[0].Payload, actionPayload); err != nil || actionPayload.Action == nil {
		return FundingTransactionAudit{}, errors.New("funding transaction endorsed action is malformed")
	}
	proposalPayload := &peer.ChaincodeProposalPayload{}
	if err := proto.Unmarshal(actionPayload.ChaincodeProposalPayload, proposalPayload); err != nil {
		return FundingTransactionAudit{}, err
	}
	invocation := &peer.ChaincodeInvocationSpec{}
	if err := proto.Unmarshal(proposalPayload.Input, invocation); err != nil || invocation.ChaincodeSpec == nil || invocation.ChaincodeSpec.Input == nil {
		return FundingTransactionAudit{}, errors.New("funding chaincode invocation is malformed")
	}
	if invocation.ChaincodeSpec.ChaincodeId == nil || invocation.ChaincodeSpec.ChaincodeId.Name != FabricChaincode {
		return FundingTransactionAudit{}, errors.New("funding transaction chaincode mismatch")
	}
	args := invocation.ChaincodeSpec.Input.Args
	if len(args) != 3 || string(args[0]) != "Set" || string(args[1]) != key || string(args[2]) != string(payload) {
		return FundingTransactionAudit{}, errors.New("funding transaction simplekv.Set arguments mismatch")
	}
	if len(actionPayload.Action.Endorsements) < 2 {
		return FundingTransactionAudit{}, errors.New("funding transaction requires endorsements from both organizations")
	}

	audit := FundingTransactionAudit{Schema: FundingAuditSchema, TransactionID: transactionID, TransactionIDVerified: true,
		BlockNumber: block.Header.Number, BlockDataHashVerified: true, Channel: FabricChannel,
		Chaincode: FabricChaincode, Function: "Set", ValidationStatus: "VALID", ValidationCode: 0,
		CommitmentKey: key, Creator: identityAudit(creator, envelope.Payload, envelope.Signature, true),
		RequiredMSPIDs: []string{"Org1MSP", "Org2MSP"}}
	blockDigest := sha256.Sum256(blockBytes)
	audit.BlockSHA256 = hex.EncodeToString(blockDigest[:])
	payloadDigest := sha256.Sum256(payload)
	audit.CommitmentSHA256 = hex.EncodeToString(payloadDigest[:])
	seenMSP := map[string]bool{}
	for _, endorsement := range actionPayload.Action.Endorsements {
		identity, err := parseFabricIdentity(endorsement.Endorser)
		if err != nil {
			return FundingTransactionAudit{}, err
		}
		message := append(append([]byte(nil), actionPayload.Action.ProposalResponsePayload...), endorsement.Endorser...)
		valid, err := verifyFabricSignature(identity, message, endorsement.Signature)
		if err != nil || !valid || identity.Algorithm != expectedPeerAlgorithm {
			return FundingTransactionAudit{}, errors.New("peer endorsement signature/algorithm verification failed")
		}
		seenMSP[identity.MSPID] = true
		audit.Endorsements = append(audit.Endorsements, identityAudit(identity, message, endorsement.Signature, true))
	}
	for _, required := range audit.RequiredMSPIDs {
		if !seenMSP[required] {
			return FundingTransactionAudit{}, fmt.Errorf("missing %s funding endorsement", required)
		}
	}
	sort.Slice(audit.Endorsements, func(i, j int) bool { return audit.Endorsements[i].MSPID < audit.Endorsements[j].MSPID })
	audit.AllSignaturesValid = true
	return audit, nil
}
