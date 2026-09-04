package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"math/big"

	oqs "github.com/open-quantum-safe/liboqs-go/oqs"
)

var pqIdentityExtensionOID = asn1.ObjectIdentifier{1, 3, 6, 1, 3, 9999, 1}

const pqPrivateKeyPEMType = "FABRIC PQ PRIVATE KEY"

type pqIdentityExtension struct {
	Algorithm string
	PublicKey []byte
}

type pqPrivateKeyASN1 struct {
	Algorithm string
	SecretKey []byte
	PublicKey []byte
}

type peerTarget struct {
	Org  string
	Peer string
}

var peerTargets = []peerTarget{
	{Org: "org1.example.com", Peer: "peer0.org1.example.com"},
	{Org: "org1.example.com", Peer: "peer1.org1.example.com"},
	{Org: "org2.example.com", Peer: "peer0.org2.example.com"},
	{Org: "org2.example.com", Peer: "peer1.org2.example.com"},
}

func main() {
	algorithm := flag.String("algorithm", "", "PQ signature algorithm")
	cryptoConfig := flag.String("crypto-config", "", "path to E1 crypto-config directory")
	flag.Parse()

	if err := validateAlgorithm(*algorithm); err != nil {
		fatal(err)
	}

	if *cryptoConfig == "" {
		fatal(errors.New("--crypto-config is required"))
	}

	for _, target := range peerTargets {
		if err := convertPeer(*cryptoConfig, target, *algorithm); err != nil {
			fatal(fmt.Errorf("%s: %w", target.Peer, err))
		}
	}

	fmt.Printf("PQ identities generated successfully using %s\n", *algorithm)
}

func validateAlgorithm(algorithm string) error {
	switch algorithm {
	case "ML-DSA-44",
		"ML-DSA-65",
		"SPHINCS+-SHA2-128s-simple":
		if !oqs.IsSigEnabled(algorithm) {
			return fmt.Errorf("algorithm %q is not enabled in liboqs", algorithm)
		}
		return nil
	default:
		return fmt.Errorf("unsupported algorithm %q", algorithm)
	}
}

func convertPeer(cryptoConfig string, target peerTarget, algorithm string) error {
	orgDir := filepath.Join(
		cryptoConfig,
		"peerOrganizations",
		target.Org,
	)

	peerDir := filepath.Join(
		orgDir,
		"peers",
		target.Peer,
		"msp",
	)

	signCertPath := filepath.Join(
		peerDir,
		"signcerts",
		target.Peer+"-cert.pem",
	)

	keyStoreDir := filepath.Join(peerDir, "keystore")

	caCertPath := filepath.Join(
		orgDir,
		"ca",
		"ca."+target.Org+"-cert.pem",
	)

	caKeyPath := filepath.Join(
		orgDir,
		"ca",
		"priv_sk",
	)

	oldCert, err := loadCertificate(signCertPath)
	if err != nil {
		return fmt.Errorf("load peer certificate: %w", err)
	}

	caCert, err := loadCertificate(caCertPath)
	if err != nil {
		return fmt.Errorf("load CA certificate: %w", err)
	}

	caKey, err := loadECDSAPrivateKey(caKeyPath)
	if err != nil {
		return fmt.Errorf("load CA private key: %w", err)
	}

	var pqSigner oqs.Signature
	if err := pqSigner.Init(algorithm, nil); err != nil {
		return fmt.Errorf("initialize %s: %w", algorithm, err)
	}
	defer pqSigner.Clean()

	pqPublicKey, err := pqSigner.GenerateKeyPair()
	if err != nil {
		return fmt.Errorf("generate PQ key pair: %w", err)
	}

	pqSecretKey := pqSigner.ExportSecretKey()

	extensionValue, err := asn1.Marshal(pqIdentityExtension{
		Algorithm: algorithm,
		PublicKey: pqPublicKey,
	})
	if err != nil {
		return fmt.Errorf("marshal PQ certificate extension: %w", err)
	}

	serialLimit := new(big.Int).Lsh(big.NewInt(1), 128)

	serialNumber, err := rand.Int(rand.Reader, serialLimit)
	if err != nil {
		return fmt.Errorf("generate certificate serial number: %w", err)
	}
	if serialNumber.Sign() == 0 {
		serialNumber = big.NewInt(1)
	}

	template := &x509.Certificate{
		SerialNumber:          serialNumber,
		Subject:               oldCert.Subject,
		NotBefore:             oldCert.NotBefore,
		NotAfter:              oldCert.NotAfter,
		KeyUsage:              oldCert.KeyUsage,
		ExtKeyUsage:           oldCert.ExtKeyUsage,
		UnknownExtKeyUsage:    oldCert.UnknownExtKeyUsage,
		BasicConstraintsValid: oldCert.BasicConstraintsValid,
		IsCA:                  oldCert.IsCA,
		MaxPathLen:            oldCert.MaxPathLen,
		MaxPathLenZero:        oldCert.MaxPathLenZero,
		DNSNames:              oldCert.DNSNames,
		EmailAddresses:        oldCert.EmailAddresses,
		IPAddresses:           oldCert.IPAddresses,
		URIs:                  oldCert.URIs,
		ExtraExtensions: []pkix.Extension{
			{
				Id:       pqIdentityExtensionOID,
				Critical: false,
				Value:    extensionValue,
			},
		},
	}

	certDER, err := x509.CreateCertificate(
		rand.Reader,
		template,
		caCert,
		oldCert.PublicKey,
		caKey,
	)
	if err != nil {
		return fmt.Errorf("create PQ-extended certificate: %w", err)
	}

	certPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "CERTIFICATE",
		Bytes: certDER,
	})

	pqKeyDER, err := asn1.Marshal(pqPrivateKeyASN1{
		Algorithm: algorithm,
		SecretKey: pqSecretKey,
		PublicKey: pqPublicKey,
	})
	if err != nil {
		return fmt.Errorf("marshal PQ private key: %w", err)
	}

	pqKeyPEM := pem.EncodeToMemory(&pem.Block{
		Type:  pqPrivateKeyPEMType,
		Bytes: pqKeyDER,
	})

	ski := sha256.Sum256(pqPublicKey)
	pqKeyPath := filepath.Join(
		keyStoreDir,
		hex.EncodeToString(ski[:])+"_pqpriv",
	)

	if err := clearDirectory(keyStoreDir); err != nil {
		return fmt.Errorf("clear peer keystore: %w", err)
	}

	if err := os.WriteFile(pqKeyPath, pqKeyPEM, 0o600); err != nil {
		return fmt.Errorf("write PQ private key: %w", err)
	}

	if err := os.WriteFile(signCertPath, certPEM, 0o644); err != nil {
		return fmt.Errorf("write PQ certificate: %w", err)
	}

	fmt.Printf(
		"%s: algorithm=%s public_key_bytes=%d certificate_bytes=%d\n",
		target.Peer,
		algorithm,
		len(pqPublicKey),
		len(certPEM),
	)

	return nil
}

func loadCertificate(path string) (*x509.Certificate, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	block, rest := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("PEM certificate not found")
	}
	if len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("unexpected trailing data after certificate PEM")
	}

	return x509.ParseCertificate(block.Bytes)
}

func loadECDSAPrivateKey(path string) (*ecdsa.PrivateKey, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	block, rest := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("PEM private key not found")
	}
	if len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("unexpected trailing data after private key PEM")
	}

	if key, err := x509.ParseECPrivateKey(block.Bytes); err == nil {
		return key, nil
	}

	keyAny, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}

	key, ok := keyAny.(*ecdsa.PrivateKey)
	if !ok {
		return nil, errors.New("CA private key is not ECDSA")
	}

	return key, nil
}

func clearDirectory(path string) error {
	entries, err := os.ReadDir(path)
	if err != nil {
		return err
	}

	for _, entry := range entries {
		if entry.IsDir() {
			return fmt.Errorf("unexpected directory in keystore: %s", entry.Name())
		}

		if err := os.Remove(filepath.Join(path, entry.Name())); err != nil {
			return err
		}
	}

	return nil
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
