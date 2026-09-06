package main

import (
	"testing"

	"github.com/golang/protobuf/proto"
	common "github.com/hyperledger/fabric-protos-go/common"
	peer "github.com/hyperledger/fabric-protos-go/peer"
)

func TestInspectBlockUsesExactEnvelopeLengthAndCountsEndorsements(t *testing.T) {
	actionPayload := &peer.ChaincodeActionPayload{
		Action: &peer.ChaincodeEndorsedAction{
			Endorsements: []*peer.Endorsement{{}, {}},
		},
	}
	actionPayloadBytes, err := proto.Marshal(actionPayload)
	if err != nil {
		t.Fatal(err)
	}
	transactionBytes, err := proto.Marshal(&peer.Transaction{
		Actions: []*peer.TransactionAction{{Payload: actionPayloadBytes}},
	})
	if err != nil {
		t.Fatal(err)
	}
	channelHeaderBytes, err := proto.Marshal(&common.ChannelHeader{
		Type: int32(common.HeaderType_ENDORSER_TRANSACTION),
		TxId: "test-transaction",
	})
	if err != nil {
		t.Fatal(err)
	}
	payloadBytes, err := proto.Marshal(&common.Payload{
		Header: &common.Header{ChannelHeader: channelHeaderBytes},
		Data:   transactionBytes,
	})
	if err != nil {
		t.Fatal(err)
	}
	envelopeBytes, err := proto.Marshal(&common.Envelope{Payload: payloadBytes})
	if err != nil {
		t.Fatal(err)
	}
	blockBytes, err := proto.Marshal(&common.Block{
		Data: &common.BlockData{Data: [][]byte{envelopeBytes}},
		Metadata: &common.BlockMetadata{Metadata: [][]byte{
			nil,
			nil,
			{byte(peer.TxValidationCode_VALID)},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	rows, err := inspectBlock(blockBytes)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	row := rows[0]
	if row.envelopeBytes != len(envelopeBytes) {
		t.Fatalf("envelope length %d, want %d", row.envelopeBytes, len(envelopeBytes))
	}
	if row.endorsementCount != 2 {
		t.Fatalf("endorsements %d, want 2", row.endorsementCount)
	}
	if row.transactionID != "test-transaction" || row.validationName != "VALID" {
		t.Fatalf("unexpected row: %#v", row)
	}
}
