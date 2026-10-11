package e7

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"

	e2 "pqc-energy-trading/e2"
)

const E7ChannelID = "c1"

type receiverRequest struct {
	sample TradeSample
	done   chan error
}

// L2Executor keeps exact E7 amount state separate from frozen E2. Frozen E2
// has no amount-bearing HTLC field or mutable channel balance. The unchanged
// deterministic HTLC ID is passed to AddHTLC and SettleHTLC; E2's signed
// canonical body authenticates SHA-256(HTLCID), binding it to the exact E7
// trade identity.
type L2Executor struct {
	config          Configuration
	e2Config        e2.Configuration
	logicalState    *LogicalState
	clientConn      net.Conn
	receiverConn    net.Conn
	senderChannel   *e2.Channel
	receiverChannel *e2.Channel
	backend         *e2.RealCryptoBackend
	transport       *e2.PersistentRTTExecutor
	requests        chan receiverRequest
	receiverDone    chan struct{}
	prepareOnce     sync.Once
	prepareErr      error
	closeOnce       sync.Once
	closed          atomic.Bool
}

func NewL2Executor(
	config Configuration,
	clientConn, receiverConn net.Conn,
	state *LogicalState,
) (*L2Executor, error) {
	var e2Config e2.Configuration
	switch config {
	case ConfigurationClassicalL2:
		e2Config = e2.ConfigurationClassical
	case ConfigurationLayerAwareL2:
		e2Config = e2.ConfigurationLayerAware
	default:
		return nil, errors.New("L2 executor requires classical_l2 or layer_aware_l2")
	}
	if clientConn == nil || receiverConn == nil || state == nil {
		return nil, errors.New("L2 executor requires two established connection ends and logical state")
	}
	return &L2Executor{
		config: config, e2Config: e2Config, logicalState: state,
		clientConn: clientConn, receiverConn: receiverConn,
		requests: make(chan receiverRequest), receiverDone: make(chan struct{}),
	}, nil
}

func (executor *L2Executor) Configuration() Configuration { return executor.config }

// PrepareRun performs E2 setup/funding, participant key generation, and
// persistent-connection initialization before the first E7 sample timer.
func (executor *L2Executor) PrepareRun(context.Context) error {
	if executor.closed.Load() {
		return errors.New("L2 executor is closed")
	}
	executor.prepareOnce.Do(func() { executor.prepareErr = executor.prepare() })
	return executor.prepareErr
}

func (executor *L2Executor) prepare() error {
	parties := []e2.PartyID{e2.PartyID(Prosumer), e2.PartyID(Counterparty)}
	var err error
	executor.senderChannel, err = e2.NewChannel(executor.e2Config, parties[0], parties[1])
	if err != nil {
		return err
	}
	executor.receiverChannel, err = e2.NewChannel(executor.e2Config, parties[0], parties[1])
	if err != nil {
		return err
	}
	for _, channel := range []*e2.Channel{executor.senderChannel, executor.receiverChannel} {
		if _, err = channel.Setup(parties[0]); err != nil {
			return err
		}
		if _, err = channel.Fund(parties[0]); err != nil {
			return err
		}
	}
	executor.backend, err = e2.NewRealCryptoBackend(executor.e2Config, parties)
	if err != nil {
		return err
	}
	executor.transport, err = e2.NewPersistentRTTExecutor(executor.clientConn)
	if err != nil {
		return err
	}
	go executor.receiverLoop()
	return nil
}

func actorsFor(sample TradeSample) (e2.PartyID, e2.PartyID) {
	if sample.Direction == BalancedTransfer {
		return e2.PartyID(Prosumer), e2.PartyID(Counterparty)
	}
	return e2.PartyID(sample.Payer), e2.PartyID(sample.Payee)
}

func (executor *L2Executor) Settle(ctx context.Context, sample TradeSample) (CompletionEvidence, error) {
	if executor.closed.Load() {
		return CompletionEvidence{}, errors.New("L2 executor is closed after completion or failure")
	}
	if executor.transport == nil || executor.backend == nil {
		return CompletionEvidence{}, errors.New("L2 executor was not prepared")
	}
	if err := ValidateSampleIdentity(sample); err != nil {
		return CompletionEvidence{}, err
	}
	if err := executor.logicalState.Reserve(sample); err != nil {
		return CompletionEvidence{}, err
	}
	request := receiverRequest{sample: sample, done: make(chan error, 1)}
	select {
	case executor.requests <- request:
	case <-ctx.Done():
		_ = executor.logicalState.Cancel(sample.LogicalTradeID)
		return CompletionEvidence{}, ctx.Err()
	}
	actor, counterparty := actorsFor(sample)
	add, err := executor.senderChannel.AddHTLC(actor, sample.HTLCID)
	if err != nil {
		return executor.abortSample(sample, err)
	}
	addBytes, err := executor.authorizeSerializeSend(add)
	if err != nil {
		return executor.abortSample(sample, err)
	}
	settle, err := executor.senderChannel.SettleHTLC(counterparty, sample.HTLCID)
	if err != nil {
		return executor.abortSample(sample, err)
	}
	settleBytes, err := executor.authorizeSerializeSend(settle)
	if err != nil {
		return executor.abortSample(sample, err)
	}
	select {
	case err = <-request.done:
	case <-ctx.Done():
		err = ctx.Err()
	}
	if err != nil {
		return executor.abortSample(sample, err)
	}
	if err := executor.logicalState.Finalize(sample.LogicalTradeID); err != nil {
		return CompletionEvidence{}, err
	}
	addAlgorithm, _ := e2.AlgorithmFor(executor.e2Config, e2.TransitionHTLCAdd)
	settleAlgorithm, _ := e2.AlgorithmFor(executor.e2Config, e2.TransitionHTLCSettle)
	addDigest, settleDigest := sha256.Sum256(addBytes), sha256.Sum256(settleBytes)
	return CompletionEvidence{
		RequestID: sample.LogicalTradeID, CompletionStatus: "l2_settled",
		ChannelID: E7ChannelID, HTLCID: sample.HTLCID,
		AddAlgorithm: addAlgorithm.Name, SettleAlgorithm: settleAlgorithm.Name,
		AddSerializedBytes: len(addBytes), SettleSerializedBytes: len(settleBytes),
		AddSerializedSHA256: hex.EncodeToString(addDigest[:]), SettleSerializedSHA256: hex.EncodeToString(settleDigest[:]),
		AuthorizationVerified: true, ReceiverVerified: true, Verified: true,
	}, nil
}

func (executor *L2Executor) authorizeSerializeSend(transition e2.Transition) ([]byte, error) {
	serializer := e2.CanonicalSerializer{}
	authorized, err := e2.AuthorizeTransition(serializer, executor.backend, transition)
	if err != nil {
		return nil, err
	}
	serialized, err := e2.ScientificSerializedTransaction(serializer, authorized)
	if err != nil {
		return nil, err
	}
	if _, err = executor.transport.Execute(e2.PreparedTransition{Authorized: authorized, Serialized: serialized}); err != nil {
		return nil, err
	}
	return serialized, nil
}

func (executor *L2Executor) receiverLoop() {
	defer close(executor.receiverDone)
	for request := range executor.requests {
		request.done <- executor.receiveSettlement(request.sample)
		close(request.done)
	}
}

func (executor *L2Executor) receiveSettlement(sample TradeSample) error {
	actor, counterparty := actorsFor(sample)
	add, err := executor.receiverChannel.AddHTLC(actor, sample.HTLCID)
	if err != nil {
		return err
	}
	if err := executor.receiveTransition(add); err != nil {
		return err
	}
	settle, err := executor.receiverChannel.SettleHTLC(counterparty, sample.HTLCID)
	if err != nil {
		return err
	}
	return executor.receiveTransition(settle)
}

func (executor *L2Executor) receiveTransition(transition e2.Transition) error {
	verifier, err := e2.NewCanonicalTransactionVerifier(executor.e2Config, transition)
	if err != nil {
		return err
	}
	defer verifier.Close()
	return e2.ServeVerification(executor.receiverConn, verifier, 1)
}

func (executor *L2Executor) abortSample(sample TradeSample, cause error) (CompletionEvidence, error) {
	_ = executor.logicalState.Cancel(sample.LogicalTradeID)
	_ = executor.Close()
	return CompletionEvidence{}, cause
}

func (executor *L2Executor) Close() error {
	var closeErr error
	executor.closeOnce.Do(func() {
		executor.closed.Store(true)
		close(executor.requests)
		if executor.clientConn != nil {
			_ = executor.clientConn.Close()
		}
		if executor.receiverConn != nil {
			_ = executor.receiverConn.Close()
		}
		if executor.backend != nil {
			executor.backend.Close()
		}
		if executor.receiverDone != nil && executor.transport != nil {
			<-executor.receiverDone
		}
	})
	return closeErr
}

func (executor *L2Executor) String() string {
	return fmt.Sprintf("E7 L2 executor %s over one persistent direct TCP connection", executor.config)
}
