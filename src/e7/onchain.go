package e7

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
)

type FabricCompletion struct {
	TransactionID    string
	Committed        bool
	Verified         bool
	ValidationStatus string
	ValidationCode   int
	BlockNumber      uint64
}

// FabricSubmitter is the narrow integration seam for Fabric Gateway submit and
// commit-status semantics. The foundation deliberately adds no new Fabric
// dependency and never substitutes a fake submitter in scientific execution.
type FabricSubmitter interface {
	PrepareRun(context.Context, Configuration) error
	SubmitAndAwaitCommit(context.Context, string, []byte) (FabricCompletion, error)
	Close() error
}

type OnChainExecutor struct {
	config              Configuration
	state               *LogicalState
	submitter           FabricSubmitter
	random              io.Reader
	setsPrivate         *ecdsa.PrivateKey
	setsPublicKeySHA256 string
}

func NewOnChainExecutor(config Configuration, state *LogicalState, submitter FabricSubmitter) (*OnChainExecutor, error) {
	if config != ConfigurationL1Only && config != ConfigurationSETSReimpl {
		return nil, errors.New("on-chain executor requires l1_only or sets_reimpl")
	}
	if state == nil || submitter == nil {
		return nil, errors.New("on-chain executor requires logical state and Fabric submitter")
	}
	return &OnChainExecutor{config: config, state: state, submitter: submitter, random: rand.Reader}, nil
}

func (executor *OnChainExecutor) Configuration() Configuration { return executor.config }

func (executor *OnChainExecutor) PrepareRun(ctx context.Context) error {
	if executor.config == ConfigurationSETSReimpl {
		key, err := ecdsa.GenerateKey(elliptic.P256(), executor.random)
		if err != nil {
			return fmt.Errorf("generate SETS P-256 identity: %w", err)
		}
		executor.setsPrivate = key
		// elliptic.Marshal is the standard uncompressed SEC1 public-key encoding.
		publicKey := elliptic.Marshal(elliptic.P256(), key.PublicKey.X, key.PublicKey.Y)
		publicKeyDigest := sha256.Sum256(publicKey)
		executor.setsPublicKeySHA256 = hex.EncodeToString(publicKeyDigest[:])
	}
	return executor.submitter.PrepareRun(ctx, executor.config)
}

func (executor *OnChainExecutor) Settle(ctx context.Context, sample TradeSample) (CompletionEvidence, error) {
	if err := ValidateSampleIdentity(sample); err != nil {
		return CompletionEvidence{}, err
	}
	common := CanonicalLogicalSettlement(sample)
	evidence := CompletionEvidence{RequestID: sample.LogicalTradeID}
	if executor.config == ConfigurationSETSReimpl {
		if executor.setsPrivate == nil {
			return CompletionEvidence{}, errors.New("SETS identity was not prepared")
		}
		digest := sha256.Sum256(common)
		signature, err := ecdsa.SignASN1(executor.random, executor.setsPrivate, digest[:])
		if err != nil {
			return CompletionEvidence{}, fmt.Errorf("sign SETS request: %w", err)
		}
		signatureDigest := sha256.Sum256(signature)
		evidence.SETSECDSAAlgorithm = SETSECDSAAlgorithm
		// digest is SHA-256 of the config-independent canonical logical
		// settlement; signatureDigest commits to the ASN.1 SignASN1 output.
		evidence.SETSSignedPreimageSHA256 = hex.EncodeToString(digest[:])
		evidence.SETSPublicKeySHA256 = executor.setsPublicKeySHA256
		evidence.SETSSignatureSHA256 = hex.EncodeToString(signatureDigest[:])
		evidence.SETSSignatureVerified = ecdsa.VerifyASN1(&executor.setsPrivate.PublicKey, digest[:], signature)
		evidence.SETSControllerEligible = validateTradeParties(sample) == nil
		evidence.SETSControllerBalanceSufficient = executor.state.Sufficient(sample)
		if !evidence.SETSSignatureVerified {
			return evidence, errors.New("SETS controller signature verification failed")
		}
		if !evidence.SETSControllerEligible || !evidence.SETSControllerBalanceSufficient {
			return evidence, errors.New("SETS controller rejected payer eligibility/balance")
		}
	}
	if err := executor.state.Reserve(sample); err != nil {
		return evidence, err
	}
	payload := CanonicalOnChainPayload(executor.config, common)
	payloadDigest := sha256.Sum256(payload)
	key := fmt.Sprintf("e7/%s/%s/%d", executor.config, sample.RunID, sample.TraceRowIndex)
	completion, err := executor.submitter.SubmitAndAwaitCommit(ctx, key, payload)
	if err != nil {
		_ = executor.state.Cancel(sample.LogicalTradeID)
		return evidence, err
	}
	if !completion.Committed || !completion.Verified || completion.ValidationStatus != "VALID" ||
		completion.ValidationCode != 0 || completion.TransactionID == "" {
		_ = executor.state.Cancel(sample.LogicalTradeID)
		return evidence, errors.New("Fabric settlement did not return exact committed VALID status")
	}
	if err := executor.state.Finalize(sample.LogicalTradeID); err != nil {
		return CompletionEvidence{}, err
	}
	evidence.TransactionID = completion.TransactionID
	evidence.CompletionStatus = completion.ValidationStatus
	evidence.Committed, evidence.Verified = true, true
	evidence.ValidationStatus, evidence.ValidationCode = completion.ValidationStatus, completion.ValidationCode
	evidence.FabricBlockNumber = completion.BlockNumber
	evidence.PayloadSHA256 = hex.EncodeToString(payloadDigest[:])
	return evidence, nil
}

func (executor *OnChainExecutor) Close() error { return executor.submitter.Close() }

// CanonicalLogicalSettlement is config-independent. Config and simplekv key
// framing are outside this subsection, so all four configurations receive the
// same logical trade terms.
func CanonicalLogicalSettlement(sample TradeSample) []byte {
	var builder strings.Builder
	appendIdentityField(&builder, "schema_version", TradeSchemaVersion)
	appendIdentityField(&builder, "trace_sha256", sample.TraceSHA256)
	appendIdentityField(&builder, "run_id", sample.RunID)
	appendIdentityField(&builder, "phase", string(sample.Phase))
	appendIdentityField(&builder, "trace_row_idx", strconv.Itoa(sample.TraceRowIndex))
	appendIdentityField(&builder, "sample_idx", strconv.Itoa(sample.SampleIndex))
	appendIdentityField(&builder, "timestamp", sample.Timestamp)
	appendIdentityField(&builder, "pv_kwh", sample.PVText)
	appendIdentityField(&builder, "demand_kwh", sample.DemandText)
	appendIdentityField(&builder, "net_kwh", sample.NetText)
	appendIdentityField(&builder, "direction", string(sample.Direction))
	appendIdentityField(&builder, "amount_kwh", sample.AmountText)
	appendIdentityField(&builder, "payer", sample.Payer)
	appendIdentityField(&builder, "payee", sample.Payee)
	appendIdentityField(&builder, "logical_trade_id", sample.LogicalTradeID)
	return []byte(builder.String())
}

func CanonicalOnChainPayload(config Configuration, common []byte) []byte {
	var builder strings.Builder
	appendIdentityField(&builder, "envelope", "e7-simplekv-v1")
	appendIdentityField(&builder, "config", string(config))
	appendIdentityField(&builder, "logical_settlement", string(common))
	return []byte(builder.String())
}

func VerifySETSSignature(public *ecdsa.PublicKey, preimage, signature []byte) bool {
	if public == nil {
		return false
	}
	digest := sha256.Sum256(preimage)
	return ecdsa.VerifyASN1(public, digest[:], signature)
}
