package main

import (
	"testing"

	"github.com/golang/protobuf/proto"
	common "github.com/hyperledger/fabric-protos-go/common"
	peer "github.com/hyperledger/fabric-protos-go/peer"
)

func buildTestBlock(t *testing.T, dataBytes, signatureBytes int) ([]byte, int, int) {
	t.Helper()
	actionPayload := &peer.ChaincodeActionPayload{
		Action: &peer.ChaincodeEndorsedAction{
			ProposalResponsePayload: make([]byte, dataBytes),
			Endorsements:            []*peer.Endorsement{{}, {}},
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
	signature := make([]byte, signatureBytes)
	envelopeBytes, err := proto.Marshal(&common.Envelope{
		Payload:   payloadBytes,
		Signature: signature,
	})
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
	return blockBytes, len(envelopeBytes), len(payloadBytes) + len(signature)
}

func TestInspectBlockUsesExactEnvelopeLengthAndCountsEndorsements(t *testing.T) {
	blockBytes, envelopeBytes, ordererMessageBytes := buildTestBlock(t, 0, 70)

	rows, err := inspectBlock(blockBytes)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	row := rows[0]
	if row.envelopeBytes != envelopeBytes {
		t.Fatalf("envelope length %d, want %d", row.envelopeBytes, envelopeBytes)
	}
	if row.ordererMessageBytes != ordererMessageBytes {
		t.Fatalf("orderer message length %d, want %d", row.ordererMessageBytes, ordererMessageBytes)
	}
	if row.endorsementCount != 2 {
		t.Fatalf("endorsements %d, want 2", row.endorsementCount)
	}
	if row.transactionID != "test-transaction" || row.validationName != "VALID" {
		t.Fatalf("unexpected row: %#v", row)
	}
}

func TestOrdererMessageBytesDoNotDependOnEnvelopeFramingLength(t *testing.T) {
	var overheads []int
	for _, dataBytes := range []int{100, 20000} {
		blockBytes, envelopeBytes, ordererMessageBytes := buildTestBlock(t, dataBytes, 70)
		rows, err := inspectBlock(blockBytes)
		if err != nil {
			t.Fatal(err)
		}
		if rows[0].ordererMessageBytes != ordererMessageBytes {
			t.Fatalf("orderer message length %d, want %d", rows[0].ordererMessageBytes, ordererMessageBytes)
		}
		overheads = append(overheads, envelopeBytes-ordererMessageBytes)
	}
	if overheads[0] == overheads[1] {
		t.Fatalf("test did not cross a protobuf framing boundary: overheads=%v", overheads)
	}
}
