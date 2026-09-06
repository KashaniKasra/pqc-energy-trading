package main

import (
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"

	"github.com/golang/protobuf/proto"
	common "github.com/hyperledger/fabric-protos-go/common"
	peer "github.com/hyperledger/fabric-protos-go/peer"
)

type transactionEvidence struct {
	index            int
	headerType       int32
	transactionID    string
	envelopeBytes    int
	envelopeSHA256   string
	validationCode   int32
	validationName   string
	endorsementCount int
}

func inspectBlock(raw []byte) ([]transactionEvidence, error) {
	block := &common.Block{}
	if err := proto.Unmarshal(raw, block); err != nil {
		return nil, fmt.Errorf("decode block: %w", err)
	}
	if block.Data == nil {
		return nil, errors.New("block data is missing")
	}

	var validationCodes []byte
	if block.Metadata != nil && len(block.Metadata.Metadata) > int(common.BlockMetadataIndex_TRANSACTIONS_FILTER) {
		validationCodes = block.Metadata.Metadata[common.BlockMetadataIndex_TRANSACTIONS_FILTER]
	}
	if len(validationCodes) != 0 && len(validationCodes) != len(block.Data.Data) {
		return nil, errors.New("transaction validation-code count does not match block data count")
	}

	rows := make([]transactionEvidence, 0, len(block.Data.Data))
	for index, rawEnvelope := range block.Data.Data {
		envelope := &common.Envelope{}
		if err := proto.Unmarshal(rawEnvelope, envelope); err != nil {
			return nil, fmt.Errorf("transaction %d envelope: %w", index, err)
		}
		payload := &common.Payload{}
		if err := proto.Unmarshal(envelope.Payload, payload); err != nil {
			return nil, fmt.Errorf("transaction %d payload: %w", index, err)
		}
		if payload.Header == nil {
			return nil, fmt.Errorf("transaction %d header is missing", index)
		}
		channelHeader := &common.ChannelHeader{}
		if err := proto.Unmarshal(payload.Header.ChannelHeader, channelHeader); err != nil {
			return nil, fmt.Errorf("transaction %d channel header: %w", index, err)
		}

		endorsementCount := -1
		if channelHeader.Type == int32(common.HeaderType_ENDORSER_TRANSACTION) {
			transaction := &peer.Transaction{}
			if err := proto.Unmarshal(payload.Data, transaction); err != nil {
				return nil, fmt.Errorf("transaction %d endorser payload: %w", index, err)
			}
			endorsementCount = 0
			if len(transaction.Actions) == 0 {
				return nil, fmt.Errorf("transaction %d has no actions", index)
			}
			for actionIndex, action := range transaction.Actions {
				chaincodeAction := &peer.ChaincodeActionPayload{}
				if err := proto.Unmarshal(action.Payload, chaincodeAction); err != nil {
					return nil, fmt.Errorf("transaction %d action %d: %w", index, actionIndex, err)
				}
				if chaincodeAction.Action == nil {
					return nil, fmt.Errorf("transaction %d action %d has no endorsed action", index, actionIndex)
				}
				endorsementCount += len(chaincodeAction.Action.Endorsements)
			}
		}

		validationCode := int32(-1)
		validationName := "unavailable"
		if len(validationCodes) != 0 {
			validationCode = int32(validationCodes[index])
			validationName = peer.TxValidationCode(validationCode).String()
		}
		digest := sha256.Sum256(rawEnvelope)
		rows = append(rows, transactionEvidence{
			index:            index,
			headerType:       channelHeader.Type,
			transactionID:    channelHeader.TxId,
			envelopeBytes:    len(rawEnvelope),
			envelopeSHA256:   hex.EncodeToString(digest[:]),
			validationCode:   validationCode,
			validationName:   validationName,
			endorsementCount: endorsementCount,
		})
	}
	return rows, nil
}

func run() error {
	input := flag.String("input", "", "serialized common.Block protobuf")
	flag.Parse()
	if *input == "" {
		return errors.New("--input is required")
	}
	raw, err := os.ReadFile(*input)
	if err != nil {
		return err
	}
	rows, err := inspectBlock(raw)
	if err != nil {
		return err
	}

	writer := csv.NewWriter(os.Stdout)
	if err := writer.Write([]string{
		"tx_index", "channel_header_type", "tx_id", "envelope_bytes",
		"envelope_sha256", "validation_code", "validation_name", "endorsements",
	}); err != nil {
		return err
	}
	for _, row := range rows {
		if err := writer.Write([]string{
			strconv.Itoa(row.index),
			strconv.FormatInt(int64(row.headerType), 10),
			row.transactionID,
			strconv.Itoa(row.envelopeBytes),
			row.envelopeSHA256,
			strconv.FormatInt(int64(row.validationCode), 10),
			row.validationName,
			strconv.Itoa(row.endorsementCount),
		}); err != nil {
			return err
		}
	}
	writer.Flush()
	return writer.Error()
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}
