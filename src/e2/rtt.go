package e2

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"time"
)

const (
	E2TargetRTTMS            = 20
	transportHeaderBytes     = 5
	transportTypeTransaction = 1
	transportTypeACK         = 2
	maximumTransportPayload  = 1 << 20
	ackVersion               = 1
	ackFailure               = 0
	ackSuccess               = 1
)

var successACK = []byte{ackVersion, ackSuccess}
var failureACK = []byte{ackVersion, ackFailure}

type SerializedTransactionVerifier interface {
	Verify(serialized []byte) error
}

func encodeTransportFrame(messageType byte, payload []byte) ([]byte, error) {
	if messageType != transportTypeTransaction && messageType != transportTypeACK {
		return nil, fmt.Errorf("unsupported E2 transport message type %d", messageType)
	}
	if len(payload) == 0 || len(payload) > maximumTransportPayload {
		return nil, fmt.Errorf("invalid E2 transport payload length %d", len(payload))
	}
	frame := make([]byte, transportHeaderBytes+len(payload))
	frame[0] = messageType
	binary.BigEndian.PutUint32(frame[1:5], uint32(len(payload)))
	copy(frame[transportHeaderBytes:], payload)
	return frame, nil
}

func readTransportFrame(reader io.Reader) (byte, []byte, error) {
	header := make([]byte, transportHeaderBytes)
	if _, err := io.ReadFull(reader, header); err != nil {
		return 0, nil, fmt.Errorf("read E2 transport header: %w", err)
	}
	messageType := header[0]
	if messageType != transportTypeTransaction && messageType != transportTypeACK {
		return 0, nil, fmt.Errorf("unsupported E2 transport message type %d", messageType)
	}
	length := binary.BigEndian.Uint32(header[1:5])
	if length == 0 || length > maximumTransportPayload {
		return 0, nil, fmt.Errorf("invalid E2 transport payload length %d", length)
	}
	payload := make([]byte, int(length))
	if _, err := io.ReadFull(reader, payload); err != nil {
		return 0, nil, fmt.Errorf("read E2 transport payload: %w", err)
	}
	return messageType, payload, nil
}

func writeAll(writer io.Writer, content []byte) error {
	for len(content) > 0 {
		written, err := writer.Write(content)
		if err != nil {
			return err
		}
		if written <= 0 {
			return io.ErrShortWrite
		}
		content = content[written:]
	}
	return nil
}

// PersistentRTTExecutor owns an already-established connection. Construction,
// TCP handshake, signing, and canonical serialization precede Execute.
type PersistentRTTExecutor struct {
	connection net.Conn
	clock      func() time.Time
	timeout    time.Duration
}

func NewPersistentRTTExecutor(connection net.Conn) (*PersistentRTTExecutor, error) {
	if connection == nil {
		return nil, errors.New("persistent E2 connection is required")
	}
	if tcp, ok := connection.(*net.TCPConn); ok {
		if err := tcp.SetNoDelay(true); err != nil {
			return nil, fmt.Errorf("enable TCP_NODELAY: %w", err)
		}
	}
	return &PersistentRTTExecutor{
		connection: connection,
		clock:      time.Now,
		timeout:    5 * time.Second,
	}, nil
}

func (executor *PersistentRTTExecutor) Scientific() bool          { return true }
func (executor *PersistentRTTExecutor) scientificExecutorMarker() {}

func (executor *PersistentRTTExecutor) Execute(prepared PreparedTransition) (float64, error) {
	if len(prepared.Serialized) == 0 {
		return 0, errors.New("prepared canonical transaction is empty")
	}
	frame, err := encodeTransportFrame(transportTypeTransaction, prepared.Serialized)
	if err != nil {
		return 0, err
	}
	if err := executor.connection.SetDeadline(time.Now().Add(executor.timeout)); err != nil {
		return 0, fmt.Errorf("set E2 transport deadline: %w", err)
	}
	started := executor.clock()
	if err := writeAll(executor.connection, frame); err != nil {
		return 0, fmt.Errorf("send prepared E2 transaction: %w", err)
	}
	messageType, acknowledgement, err := readTransportFrame(executor.connection)
	if err != nil {
		return 0, err
	}
	if messageType != transportTypeACK {
		return 0, fmt.Errorf("received message type %d instead of ACK", messageType)
	}
	if len(acknowledgement) != len(successACK) || acknowledgement[0] != ackVersion {
		return 0, errors.New("malformed E2 verification ACK")
	}
	if acknowledgement[1] != ackSuccess {
		return 0, errors.New("node B rejected transaction verification")
	}
	finished := executor.clock()
	if finished.Before(started) {
		return 0, errors.New("monotonic E2 RTT clock moved backwards")
	}
	return float64(finished.Sub(started).Nanoseconds()) / 1_000_000.0, nil
}

// ServeVerification exchanges exactly iterationCount non-pipelined requests.
// A success ACK is emitted only after verifier.Verify has returned nil.
func ServeVerification(
	connection net.Conn,
	verifier SerializedTransactionVerifier,
	iterationCount int,
) error {
	if connection == nil || verifier == nil || iterationCount < 0 {
		return errors.New("invalid E2 verification server parameters")
	}
	if tcp, ok := connection.(*net.TCPConn); ok {
		if err := tcp.SetNoDelay(true); err != nil {
			return err
		}
	}
	for iteration := 0; iteration < iterationCount; iteration++ {
		messageType, serialized, err := readTransportFrame(connection)
		if err != nil {
			return err
		}
		if messageType != transportTypeTransaction {
			return fmt.Errorf("received message type %d instead of transaction", messageType)
		}
		acknowledgement := successACK
		if err := verifier.Verify(serialized); err != nil {
			acknowledgement = failureACK
		}
		frame, err := encodeTransportFrame(transportTypeACK, acknowledgement)
		if err != nil {
			return err
		}
		if err := writeAll(connection, frame); err != nil {
			return err
		}
	}
	return nil
}
