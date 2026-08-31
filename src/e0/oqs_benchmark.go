package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"

	oqs "github.com/open-quantum-safe/liboqs-go/oqs"
)

const benchmarkMessageSeed = "pqc-energy-trading-e0-message-v1"

var benchmarkMessage = sha256.Sum256([]byte(benchmarkMessageSeed))

func newOQSSignature(scheme string) (*oqs.Signature, error) {
	liboqsScheme := scheme

	if scheme == "SPHINCS+-SHA2-128s" {
		liboqsScheme = "SPHINCS+-SHA2-128s-simple"
	}

	var sig oqs.Signature
	if err := sig.Init(liboqsScheme, nil); err != nil {
		return nil, fmt.Errorf("failed to initialize signature scheme %s: %w", scheme, err)
	}

	return &sig, nil
}

func newOQSKEM(scheme string) (*oqs.KeyEncapsulation, error) {
	var kem oqs.KeyEncapsulation

	if err := kem.Init(scheme, nil); err != nil {
		return nil, fmt.Errorf("failed to initialize KEM scheme %s: %w", scheme, err)
	}

	return &kem, nil
}

func benchmarkOQSSignatureKeygen(scheme string, warmup int, nIter int) ([]float64, error) {
	sig, err := newOQSSignature(scheme)
	if err != nil {
		return nil, err
	}
	defer sig.Clean()

	var publicKey []byte
	var operationErr error

	operation := func() {
		publicKey, operationErr = sig.GenerateKeyPair()
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("key generation failed for %s: %w", scheme, operationErr)
		}

		if len(publicKey) == 0 {
			return fmt.Errorf("key generation returned empty public key for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOQSSignatureSign(scheme string, warmup int, nIter int) ([]float64, error) {
	sig, err := newOQSSignature(scheme)
	if err != nil {
		return nil, err
	}
	defer sig.Clean()

	if _, err := sig.GenerateKeyPair(); err != nil {
		return nil, fmt.Errorf("setup key generation failed for %s: %w", scheme, err)
	}

	var signature []byte
	var operationErr error

	operation := func() {
		signature, operationErr = sig.Sign(benchmarkMessage[:])
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("signing failed for %s: %w", scheme, operationErr)
		}

		if len(signature) == 0 {
			return fmt.Errorf("signing returned empty signature for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOQSSignatureVerify(scheme string, warmup int, nIter int) ([]float64, error) {
	sig, err := newOQSSignature(scheme)
	if err != nil {
		return nil, err
	}
	defer sig.Clean()

	publicKey, err := sig.GenerateKeyPair()
	if err != nil {
		return nil, fmt.Errorf("setup key generation failed for %s: %w", scheme, err)
	}

	signature, err := sig.Sign(benchmarkMessage[:])
	if err != nil {
		return nil, fmt.Errorf("setup signing failed for %s: %w", scheme, err)
	}

	var valid bool
	var operationErr error

	operation := func() {
		valid, operationErr = sig.Verify(benchmarkMessage[:], signature, publicKey)
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("verification failed for %s: %w", scheme, operationErr)
		}

		if !valid {
			return fmt.Errorf("signature is invalid for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOQSKEMKeygen(scheme string, warmup int, nIter int) ([]float64, error) {
	kem, err := newOQSKEM(scheme)
	if err != nil {
		return nil, err
	}
	defer kem.Clean()

	var publicKey []byte
	var operationErr error

	operation := func() {
		publicKey, operationErr = kem.GenerateKeyPair()
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("key generation failed for %s: %w", scheme, operationErr)
		}

		if len(publicKey) == 0 {
			return fmt.Errorf("key generation returned empty public key for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOQSKEMEncaps(scheme string, warmup int, nIter int) ([]float64, error) {
	kem, err := newOQSKEM(scheme)
	if err != nil {
		return nil, err
	}
	defer kem.Clean()

	publicKey, err := kem.GenerateKeyPair()
	if err != nil {
		return nil, fmt.Errorf("setup key generation failed for %s: %w", scheme, err)
	}

	var ciphertext []byte
	var sharedSecret []byte
	var operationErr error

	operation := func() {
		ciphertext, sharedSecret, operationErr = kem.EncapSecret(publicKey)
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("encapsulation failed for %s: %w", scheme, operationErr)
		}

		if len(ciphertext) == 0 {
			return fmt.Errorf("encapsulation returned empty ciphertext for %s", scheme)
		}

		if len(sharedSecret) == 0 {
			return fmt.Errorf("encapsulation returned empty shared secret for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOQSKEMDecaps(scheme string, warmup int, nIter int) ([]float64, error) {
	kem, err := newOQSKEM(scheme)
	if err != nil {
		return nil, err
	}
	defer kem.Clean()

	publicKey, err := kem.GenerateKeyPair()
	if err != nil {
		return nil, fmt.Errorf("setup key generation failed for %s: %w", scheme, err)
	}

	ciphertext, sharedSecretEncap, err := kem.EncapSecret(publicKey)
	if err != nil {
		return nil, fmt.Errorf("setup encapsulation failed for %s: %w", scheme, err)
	}

	var sharedSecretDecap []byte
	var operationErr error

	operation := func() {
		sharedSecretDecap, operationErr = kem.DecapSecret(ciphertext)
	}

	postOperation := func() error {
		if operationErr != nil {
			return fmt.Errorf("decapsulation failed for %s: %w", scheme, operationErr)
		}

		if !bytes.Equal(sharedSecretEncap, sharedSecretDecap) {
			return fmt.Errorf("shared secrets do not match for %s", scheme)
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}
