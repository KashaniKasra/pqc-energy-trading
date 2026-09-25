package e2

import (
	"bytes"
	"errors"
	"io"
	"net"
	"reflect"
	"testing"
	"time"
)

type recordingSerializer struct {
	testOnlySerializer
	events *[]string
}

func (serializer recordingSerializer) SigningBytes(transition Transition) ([]byte, error) {
	*serializer.events = append(*serializer.events, "signing-preimage")
	return serializer.testOnlySerializer.SigningBytes(transition)
}

func (serializer recordingSerializer) Serialize(authorized AuthorizedTransition) ([]byte, error) {
	*serializer.events = append(*serializer.events, "serialize")
	return serializer.testOnlySerializer.Serialize(authorized)
}

type recordingBackend struct {
	testBackend
	events *[]string
}

func (backend recordingBackend) Authorize(transition Transition, message []byte) (Authorization, error) {
	*backend.events = append(*backend.events, "sign")
	return backend.testBackend.Authorize(transition, message)
}

func (backend recordingBackend) VerifyAuthorization(
	transition Transition,
	message []byte,
	authorization Authorization,
) (bool, error) {
	*backend.events = append(*backend.events, "preflight-verify")
	return backend.testBackend.VerifyAuthorization(transition, message, authorization)
}

type recordingExecutor struct {
	events        *[]string
	serializedLen *int
}

func (recordingExecutor) Scientific() bool { return false }
func (executor recordingExecutor) Execute(prepared PreparedTransition) (float64, error) {
	if len(prepared.Serialized) == 0 {
		return 0, errors.New("serialized bytes missing")
	}
	if executor.serializedLen != nil {
		*executor.serializedLen = len(prepared.Serialized)
	}
	*executor.events = append(*executor.events, "timer-start/send/ack/timer-stop")
	return 20, nil
}

func TestSigningAndSerializationPrecedeExecutorTimer(t *testing.T) {
	events := []string{}
	serializedLen := 0
	records, err := RunConditionMeasurements(
		ConfigurationClassical,
		TransitionPenalty,
		recordingBackend{testBackend: testBackend{configuration: ConfigurationClassical}, events: &events},
		recordingSerializer{events: &events},
		recordingExecutor{events: &events, serializedLen: &serializedLen},
		RunOptions{MeasuredIterations: 1},
	)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"signing-preimage", "sign", "preflight-verify", "serialize", "timer-start/send/ack/timer-stop"}
	if !reflect.DeepEqual(events, want) {
		t.Fatalf("operation order = %v, want %v", events, want)
	}
	if len(records) != 1 || records[0].MessageBytes == 0 || !records[0].Success {
		t.Fatalf("record = %+v", records)
	}
	if records[0].MessageBytes != serializedLen {
		t.Fatalf("message_bytes = %d, actual serialized length = %d", records[0].MessageBytes, serializedLen)
	}
}

type verifierFunc func([]byte) error

func (function verifierFunc) Verify(serialized []byte) error { return function(serialized) }

func cloneAuthorizedForRTTTest(source AuthorizedTransition) AuthorizedTransition {
	clone := source
	clone.Authorization.Signers = append([]PartyID(nil), source.Authorization.Signers...)
	clone.Authorization.Materials = make([]CryptoMaterial, len(source.Authorization.Materials))
	for index, material := range source.Authorization.Materials {
		clone.Authorization.Materials[index] = material
		clone.Authorization.Materials[index].Signature = append([]byte(nil), material.Signature...)
		clone.Authorization.Materials[index].PublicKey = append([]byte(nil), material.PublicKey...)
	}
	return clone
}

func TestTimerIncludesVerificationAndACK(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	events := make(chan string, 3)
	verifier := verifierFunc(func(serialized []byte) error {
		events <- "verify"
		return nil
	})
	serverResult := make(chan error, 1)
	go func() { serverResult <- ServeVerification(server, verifier, 1) }()
	executor, err := NewPersistentRTTExecutor(client)
	if err != nil {
		t.Fatal(err)
	}
	clockValues := []time.Time{time.Unix(0, 0), time.Unix(0, 25_000_000)}
	executor.clock = func() time.Time {
		events <- "clock"
		value := clockValues[0]
		clockValues = clockValues[1:]
		return value
	}
	rtt, err := executor.Execute(PreparedTransition{Serialized: []byte("already-signed-and-serialized")})
	if err != nil {
		t.Fatal(err)
	}
	if rtt != 25 {
		t.Fatalf("RTT = %f, want 25", rtt)
	}
	if err := <-serverResult; err != nil {
		t.Fatal(err)
	}
	got := []string{<-events, <-events, <-events}
	if !reflect.DeepEqual(got, []string{"clock", "verify", "clock"}) {
		t.Fatalf("timed event order = %v", got)
	}
}

func TestFailedVerificationPreventsSuccessACK(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	serverResult := make(chan error, 1)
	go func() {
		serverResult <- ServeVerification(server, verifierFunc(func([]byte) error {
			return errors.New("invalid signature")
		}), 1)
	}()
	executor, err := NewPersistentRTTExecutor(client)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := executor.Execute(PreparedTransition{Serialized: []byte("bad")}); err == nil {
		t.Fatal("verification failure produced a success ACK")
	}
	if err := <-serverResult; err != nil {
		t.Fatal(err)
	}
}

type chunkReader struct {
	content []byte
	limit   int
}

func (reader *chunkReader) Read(output []byte) (int, error) {
	if len(reader.content) == 0 {
		return 0, io.EOF
	}
	count := len(output)
	if count > reader.limit {
		count = reader.limit
	}
	if count > len(reader.content) {
		count = len(reader.content)
	}
	copy(output, reader.content[:count])
	reader.content = reader.content[count:]
	return count, nil
}

func TestTransportPartialReadAndMalformedFrames(t *testing.T) {
	frame, err := encodeTransportFrame(transportTypeTransaction, []byte("payload"))
	if err != nil {
		t.Fatal(err)
	}
	messageType, payload, err := readTransportFrame(&chunkReader{content: frame, limit: 1})
	if err != nil || messageType != transportTypeTransaction || !bytes.Equal(payload, []byte("payload")) {
		t.Fatalf("partial frame = %d %q %v", messageType, payload, err)
	}
	for name, malformed := range map[string][]byte{
		"truncated header":  {transportTypeTransaction, 0},
		"truncated payload": {transportTypeTransaction, 0, 0, 0, 5, 1},
		"unknown type":      {99, 0, 0, 0, 1, 1},
		"oversize":          {transportTypeTransaction, 0xff, 0xff, 0xff, 0xff},
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := readTransportFrame(bytes.NewReader(malformed)); err == nil {
				t.Fatal("malformed frame accepted")
			}
		})
	}
}

func TestCanonicalVerifierChecksEveryRealSignature(t *testing.T) {
	serializer := CanonicalSerializer{}
	for _, configuration := range Configurations() {
		backend := newRealBackendForTest(t, configuration)
		defer backend.Close()
		scenario, err := BuildMeasurementScenario(configuration)
		if err != nil {
			t.Fatal(err)
		}
		for _, transition := range scenario.Transitions {
			authorized, err := AuthorizeTransition(serializer, backend, transition)
			if err != nil {
				t.Fatal(err)
			}
			serialized, err := ScientificSerializedTransaction(serializer, authorized)
			if err != nil {
				t.Fatal(err)
			}
			if len(serialized) != expectedSerializedSize(t, configuration, transition.Type) {
				t.Fatalf("%s/%s serialized length = %d", configuration, transition.Type, len(serialized))
			}
			verifier, err := NewCanonicalTransactionVerifier(configuration, transition)
			if err != nil {
				t.Fatal(err)
			}
			if err := verifier.Verify(serialized); err != nil {
				verifier.Close()
				t.Fatalf("%s/%s verification: %v", configuration, transition.Type, err)
			}
			verifier.Close()

			if len(authorized.Authorization.Materials) == 2 {
				tampered := cloneAuthorizedForRTTTest(authorized)
				tampered.Authorization.Materials[1].Signature[0] ^= 0x80
				tamperedBytes, err := serializer.Serialize(tampered)
				if err != nil {
					t.Fatal(err)
				}
				secondVerifier, err := NewCanonicalTransactionVerifier(configuration, transition)
				if err != nil {
					t.Fatal(err)
				}
				if err := secondVerifier.Verify(tamperedBytes); err == nil {
					secondVerifier.Close()
					t.Fatal("tampered second bilateral signature was not verified")
				}
				secondVerifier.Close()
			}
		}
	}
}

func TestACKAndTransportFramingDoNotChangeMessageBytes(t *testing.T) {
	serialized := bytes.Repeat([]byte{1}, 222)
	frame, err := encodeTransportFrame(transportTypeTransaction, serialized)
	if err != nil {
		t.Fatal(err)
	}
	ack, err := encodeTransportFrame(transportTypeACK, successACK)
	if err != nil {
		t.Fatal(err)
	}
	if len(serialized) != 222 || len(frame) == len(serialized) || len(ack) == 0 {
		t.Fatalf("serialized=%d frame=%d ack=%d", len(serialized), len(frame), len(ack))
	}
}
