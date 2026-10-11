package e7

import (
	"context"
	"net"
	"testing"
)

func tcpPair(t *testing.T) (net.Conn, net.Conn) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	accepted := make(chan net.Conn, 1)
	errors := make(chan error, 1)
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			errors <- acceptErr
			return
		}
		accepted <- connection
	}()
	client, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	var server net.Conn
	select {
	case server = <-accepted:
	case err = <-errors:
		t.Fatal(err)
	}
	_ = listener.Close()
	return client, server
}

func TestL2PersistentReceiverVerifiesAddAndSettle(t *testing.T) {
	for _, configuration := range []Configuration{ConfigurationClassicalL2, ConfigurationLayerAwareL2} {
		t.Run(string(configuration), func(t *testing.T) {
			client, server := tcpPair(t)
			state, _ := NewTraceDerivedLogicalState()
			executor, err := NewL2Executor(configuration, client, server, state)
			if err != nil {
				t.Fatal(err)
			}
			defer executor.Close()
			if err := executor.PrepareRun(context.Background()); err != nil {
				t.Fatal(err)
			}
			samples, err := LoadFrozenSamples(frozenTracePath(t))
			if err != nil {
				t.Fatal(err)
			}
			for _, sample := range samples[100:102] {
				evidence, err := executor.Settle(context.Background(), sample)
				if err != nil {
					t.Fatal(err)
				}
				if !evidence.AuthorizationVerified || !evidence.ReceiverVerified || evidence.HTLCID == "" {
					t.Fatal("verification evidence missing")
				}
				if evidence.AddSerializedBytes != evidence.SettleSerializedBytes {
					t.Fatal("ADD/SETTLE size mismatch")
				}
				if configuration == ConfigurationClassicalL2 && evidence.AddSerializedBytes != 222 {
					t.Fatalf("classical bytes=%d", evidence.AddSerializedBytes)
				}
				if configuration == ConfigurationLayerAwareL2 && evidence.AddSerializedBytes != 3252 {
					t.Fatalf("layer-aware bytes=%d", evidence.AddSerializedBytes)
				}
			}
		})
	}
}

func TestL2TransportFailureRollsBackAndClosesExecutor(t *testing.T) {
	client, server := tcpPair(t)
	state, _ := NewTraceDerivedLogicalState()
	executor, err := NewL2Executor(ConfigurationClassicalL2, client, server, state)
	if err != nil {
		t.Fatal(err)
	}
	if err := executor.PrepareRun(context.Background()); err != nil {
		t.Fatal(err)
	}
	before := state.Snapshot()
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}
	samples, err := LoadFrozenSamples(frozenTracePath(t))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = executor.Settle(context.Background(), samples[100]); err == nil {
		t.Fatal("broken L2 transport returned success")
	}
	assertLogicalStateRestored(t, before, state.Snapshot())
	if _, err = executor.Settle(context.Background(), samples[101]); err == nil {
		t.Fatal("failed L2 executor attempted to continue")
	}
	if !executor.closed.Load() {
		t.Fatal("failed L2 executor is not closed")
	}
}
