package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"testing"
)

func TestExtractECDSAPublicKeyUsesFabricDERRepresentation(t *testing.T) {
	privateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{PublicKey: &privateKey.PublicKey}

	publicKey, sourceType, representation, err := extractPublicKey(cert, "ecdsa")
	if err != nil {
		t.Fatal(err)
	}
	if sourceType != "x509_subject_public_key_info" || representation != "der_subject_public_key_info" {
		t.Fatalf("unexpected representation: %s %s", sourceType, representation)
	}
	if len(publicKey) != 91 {
		t.Fatalf("P-256 SubjectPublicKeyInfo is %d bytes, want 91", len(publicKey))
	}
}

func TestExtractPQPublicKeyUsesRawExtensionBytes(t *testing.T) {
	rawPublicKey := []byte{1, 2, 3, 4}
	extensionValue, err := asn1.Marshal(pqIdentityExtension{
		Algorithm: "ML-DSA-44",
		PublicKey: rawPublicKey,
	})
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{Extensions: []pkix.Extension{{
		Id: pqIdentityExtensionOID, Value: extensionValue,
	}}}

	publicKey, sourceType, representation, err := extractPublicKey(cert, "ml-dsa-44")
	if err != nil {
		t.Fatal(err)
	}
	if sourceType != "experimental_x509_extension_1.3.6.1.3.9999.1" || representation != "liboqs_raw_public_key" {
		t.Fatalf("unexpected representation: %s %s", sourceType, representation)
	}
	if string(publicKey) != string(rawPublicKey) {
		t.Fatalf("public key %v, want %v", publicKey, rawPublicKey)
	}
}
