package e7

import (
	"errors"
	"fmt"
	"math/big"
	"sync"
)

type SettlementStatus string

const (
	SettlementReserved SettlementStatus = "reserved"
	SettlementSettled  SettlementStatus = "settled"
)

type PendingSettlement struct {
	LogicalTradeID string
	Amount         *big.Rat
	Payer          string
	Payee          string
	Direction      Direction
	TraceRowIndex  int
	Status         SettlementStatus
}

type BalanceSnapshot struct {
	Prosumer     *big.Rat
	Counterparty *big.Rat
	Reserved     *big.Rat
	Pending      *PendingSettlement
}

type LogicalState struct {
	mu           sync.Mutex
	prosumer     *big.Rat
	counterparty *big.Rat
	reserved     *big.Rat
	pending      *PendingSettlement
	total        *big.Rat
}

func NewLogicalState(prosumerText, counterpartyText string) (*LogicalState, error) {
	prosumer, err := parseRat(prosumerText, "prosumer balance")
	if err != nil || prosumer.Sign() < 0 {
		return nil, errors.New("invalid prosumer logical balance")
	}
	counterparty, err := parseRat(counterpartyText, "counterparty balance")
	if err != nil || counterparty.Sign() < 0 {
		return nil, errors.New("invalid counterparty logical balance")
	}
	return &LogicalState{
		prosumer: new(big.Rat).Set(prosumer), counterparty: new(big.Rat).Set(counterparty),
		reserved: new(big.Rat), total: new(big.Rat).Add(prosumer, counterparty),
	}, nil
}

func NewTraceDerivedLogicalState() (*LogicalState, error) {
	return NewLogicalState(InitialProsumerText, "0")
}

func (state *LogicalState) Reserve(sample TradeSample) error {
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.pending != nil {
		return errors.New("an E7 settlement is already pending")
	}
	if sample.Amount == nil || sample.Amount.Sign() < 0 || sample.LogicalTradeID == "" {
		return errors.New("invalid E7 settlement reservation")
	}
	if err := validateTradeParties(sample); err != nil {
		return err
	}
	if sample.Amount.Sign() > 0 {
		payer := state.balanceLocked(sample.Payer)
		if payer == nil || payer.Cmp(sample.Amount) < 0 {
			return fmt.Errorf("insufficient %s logical balance", sample.Payer)
		}
		payer.Sub(payer, sample.Amount)
		state.reserved.Set(sample.Amount)
	}
	state.pending = &PendingSettlement{
		LogicalTradeID: sample.LogicalTradeID, Amount: new(big.Rat).Set(sample.Amount),
		Payer: sample.Payer, Payee: sample.Payee, Direction: sample.Direction,
		TraceRowIndex: sample.TraceRowIndex, Status: SettlementReserved,
	}
	return state.validateConservationLocked()
}

func (state *LogicalState) Finalize(logicalTradeID string) error {
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.pending == nil || state.pending.LogicalTradeID != logicalTradeID {
		return errors.New("E7 settlement is not the pending reservation")
	}
	if state.pending.Amount.Sign() > 0 {
		payee := state.balanceLocked(state.pending.Payee)
		if payee == nil {
			return errors.New("invalid E7 settlement payee")
		}
		payee.Add(payee, state.reserved)
	}
	state.pending.Status = SettlementSettled
	state.pending = nil
	state.reserved.SetInt64(0)
	return state.validateConservationLocked()
}

func (state *LogicalState) Cancel(logicalTradeID string) error {
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.pending == nil || state.pending.LogicalTradeID != logicalTradeID {
		return errors.New("E7 settlement is not the pending reservation")
	}
	if state.pending.Amount.Sign() > 0 {
		payer := state.balanceLocked(state.pending.Payer)
		payer.Add(payer, state.reserved)
	}
	state.pending = nil
	state.reserved.SetInt64(0)
	return state.validateConservationLocked()
}

func (state *LogicalState) Sufficient(sample TradeSample) bool {
	state.mu.Lock()
	defer state.mu.Unlock()
	if sample.Amount == nil || sample.Amount.Sign() < 0 || state.pending != nil {
		return false
	}
	if sample.Amount.Sign() == 0 {
		return true
	}
	payer := state.balanceLocked(sample.Payer)
	return payer != nil && payer.Cmp(sample.Amount) >= 0
}

func (state *LogicalState) Snapshot() BalanceSnapshot {
	state.mu.Lock()
	defer state.mu.Unlock()
	snapshot := BalanceSnapshot{
		Prosumer: new(big.Rat).Set(state.prosumer), Counterparty: new(big.Rat).Set(state.counterparty),
		Reserved: new(big.Rat).Set(state.reserved),
	}
	if state.pending != nil {
		copyPending := *state.pending
		copyPending.Amount = new(big.Rat).Set(state.pending.Amount)
		snapshot.Pending = &copyPending
	}
	return snapshot
}

func validateTradeParties(sample TradeSample) error {
	switch sample.Direction {
	case ProsumerSale:
		if sample.Payer != Counterparty || sample.Payee != Prosumer || sample.Amount.Sign() <= 0 {
			return errors.New("invalid prosumer-sale parties or amount")
		}
	case ProsumerPurchase:
		if sample.Payer != Prosumer || sample.Payee != Counterparty || sample.Amount.Sign() <= 0 {
			return errors.New("invalid prosumer-purchase parties or amount")
		}
	case BalancedTransfer:
		if sample.Payer != NoParty || sample.Payee != NoParty || sample.Amount.Sign() != 0 {
			return errors.New("invalid balanced settlement")
		}
	default:
		return errors.New("unknown E7 settlement direction")
	}
	return nil
}

func (state *LogicalState) balanceLocked(party string) *big.Rat {
	switch party {
	case Prosumer:
		return state.prosumer
	case Counterparty:
		return state.counterparty
	default:
		return nil
	}
}

func (state *LogicalState) validateConservationLocked() error {
	observed := new(big.Rat).Add(state.prosumer, state.counterparty)
	observed.Add(observed, state.reserved)
	if observed.Cmp(state.total) != 0 {
		return errors.New("E7 logical balance conservation failure")
	}
	if state.prosumer.Sign() < 0 || state.counterparty.Sign() < 0 || state.reserved.Sign() < 0 {
		return errors.New("negative E7 logical balance")
	}
	return nil
}
