package main

/*
#cgo LDFLAGS: -lcrypto
#include <openssl/ec.h>
#include <openssl/obj_mac.h>
#include <openssl/sha.h>
*/
import "C"

import (
	"fmt"
	"unsafe"
)

type opensslECDSAKey struct {
	key *C.EC_KEY
}

func newOpenSSLECDSAP256Key() (*opensslECDSAKey, error) {
	key := C.EC_KEY_new_by_curve_name(C.NID_X9_62_prime256v1)
	if key == nil {
		return nil, fmt.Errorf("failed to create OpenSSL ECDSA P-256 key context")
	}

	return &opensslECDSAKey{key: key}, nil
}

func (k *opensslECDSAKey) clean() {
	if k != nil && k.key != nil {
		C.EC_KEY_free(k.key)
		k.key = nil
	}
}

func benchmarkOpenSSLECDSAP256Keygen(warmup int, nIter int) ([]float64, error) {
	key, err := newOpenSSLECDSAP256Key()
	if err != nil {
		return nil, err
	}
	defer key.clean()

	var result C.int

	operation := func() {
		result = C.EC_KEY_generate_key(key.key)
	}

	postOperation := func() error {
		if result != 1 {
			return fmt.Errorf("OpenSSL ECDSA P-256 key generation failed")
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOpenSSLECDSAP256Sign(warmup int, nIter int) ([]float64, error) {
	key, err := newOpenSSLECDSAP256Key()
	if err != nil {
		return nil, err
	}
	defer key.clean()

	if C.EC_KEY_generate_key(key.key) != 1 {
		return nil, fmt.Errorf("OpenSSL ECDSA P-256 setup key generation failed")
	}

	digest := make([]byte, C.SHA256_DIGEST_LENGTH)

	if C.SHA256(
		(*C.uchar)(unsafe.Pointer(&benchmarkMessage[0])),
		C.size_t(len(benchmarkMessage)),
		(*C.uchar)(unsafe.Pointer(&digest[0])),
	) == nil {
		return nil, fmt.Errorf("OpenSSL SHA-256 setup failed")
	}

	var signature *C.ECDSA_SIG

	operation := func() {
		signature = C.ECDSA_do_sign(
			(*C.uchar)(unsafe.Pointer(&digest[0])),
			C.int(len(digest)),
			key.key,
		)
	}

	postOperation := func() error {
		if signature == nil {
			return fmt.Errorf("OpenSSL ECDSA P-256 signing failed")
		}

		C.ECDSA_SIG_free(signature)
		signature = nil

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}

func benchmarkOpenSSLECDSAP256Verify(warmup int, nIter int) ([]float64, error) {
	key, err := newOpenSSLECDSAP256Key()
	if err != nil {
		return nil, err
	}
	defer key.clean()

	if C.EC_KEY_generate_key(key.key) != 1 {
		return nil, fmt.Errorf("OpenSSL ECDSA P-256 setup key generation failed")
	}

	digest := make([]byte, C.SHA256_DIGEST_LENGTH)

	if C.SHA256(
		(*C.uchar)(unsafe.Pointer(&benchmarkMessage[0])),
		C.size_t(len(benchmarkMessage)),
		(*C.uchar)(unsafe.Pointer(&digest[0])),
	) == nil {
		return nil, fmt.Errorf("OpenSSL SHA-256 setup failed")
	}

	signature := C.ECDSA_do_sign(
		(*C.uchar)(unsafe.Pointer(&digest[0])),
		C.int(len(digest)),
		key.key,
	)
	if signature == nil {
		return nil, fmt.Errorf("OpenSSL ECDSA P-256 setup signing failed")
	}
	defer C.ECDSA_SIG_free(signature)

	var result C.int

	operation := func() {
		result = C.ECDSA_do_verify(
			(*C.uchar)(unsafe.Pointer(&digest[0])),
			C.int(len(digest)),
			signature,
			key.key,
		)
	}

	postOperation := func() error {
		if result == -1 {
			return fmt.Errorf("OpenSSL ECDSA P-256 verification error")
		}

		if result != 1 {
			return fmt.Errorf("OpenSSL ECDSA P-256 signature is invalid")
		}

		return nil
	}

	return collectTimingSamples(operation, postOperation, warmup, nIter)
}
