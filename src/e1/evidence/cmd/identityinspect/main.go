package main

import (
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/asn1"
	"encoding/csv"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
)

var pqIdentityExtensionOID = asn1.ObjectIdentifier{1, 3, 6, 1, 3, 9999, 1}

type pqIdentityExtension struct {
	Algorithm string
	PublicKey []byte
}

type keyEvidence struct {
	peer           string
	sourceType     string
	sourcePath     string
	representation string
	bytes          []byte
}

func extractPublicKey(cert *x509.Certificate, expectedConfig string) ([]byte, string, string, error) {
	for _, extension := range cert.Extensions {
		if !extension.Id.Equal(pqIdentityExtensionOID) {
			continue
		}

		if expectedConfig == "ecdsa" {
			return nil, "", "", errors.New("ECDSA certificate unexpectedly contains the PQ extension")
		}

		var pqExtension pqIdentityExtension
		rest, err := asn1.Unmarshal(extension.Value, &pqExtension)
		if err != nil {
			return nil, "", "", fmt.Errorf("decode PQ identity extension: %w", err)
		}
		if len(rest) != 0 || len(pqExtension.PublicKey) == 0 {
			return nil, "", "", errors.New("PQ identity extension has trailing data or an empty public key")
		}

		expectedAlgorithm := map[string]string{
			"ml-dsa-44": "ML-DSA-44",
			"ml-dsa-65": "ML-DSA-65",
			"sphincs":   "SPHINCS+-SHA2-128s-simple",
		}[expectedConfig]
		if pqExtension.Algorithm != expectedAlgorithm {
			return nil, "", "", fmt.Errorf(
				"certificate algorithm %q does not match configuration %q",
				pqExtension.Algorithm,
				expectedConfig,
			)
		}

		return pqExtension.PublicKey,
			"experimental_x509_extension_1.3.6.1.3.9999.1",
			"liboqs_raw_public_key",
			nil
	}

	if expectedConfig != "ecdsa" {
		return nil, "", "", errors.New("PQ certificate extension not found")
	}

	publicKey, ok := cert.PublicKey.(*ecdsa.PublicKey)
	if !ok || publicKey.Curve.Params().Name != "P-256" {
		return nil, "", "", errors.New("ECDSA identity does not contain a P-256 public key")
	}
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		return nil, "", "", fmt.Errorf("marshal ECDSA public key as SubjectPublicKeyInfo: %w", err)
	}
	return der, "x509_subject_public_key_info", "der_subject_public_key_info", nil
}

func inspectCertificate(path, cryptoConfig, sourcePrefix, config string) (keyEvidence, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return keyEvidence{}, err
	}
	block, rest := pem.Decode(raw)
	if block == nil || block.Type != "CERTIFICATE" || len(rest) != 0 {
		return keyEvidence{}, errors.New("file must contain exactly one PEM certificate")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return keyEvidence{}, err
	}
	publicKey, sourceType, representation, err := extractPublicKey(cert, config)
	if err != nil {
		return keyEvidence{}, err
	}

	peerDir := filepath.Dir(filepath.Dir(filepath.Dir(path)))
	return keyEvidence{
		peer:           filepath.Base(peerDir),
		sourceType:     sourceType,
		sourcePath:     filepath.ToSlash(filepath.Join(sourcePrefix, mustRelative(cryptoConfig, path))),
		representation: representation,
		bytes:          publicKey,
	}, nil
}

func mustRelative(base, path string) string {
	relative, err := filepath.Rel(base, path)
	if err != nil {
		panic(err)
	}
	return relative
}

func run() error {
	config := flag.String("config", "", "ecdsa, ml-dsa-44, ml-dsa-65, or sphincs")
	runLabel := flag.String("run-label", "", "collision-safe evidence run label")
	cryptoConfig := flag.String("crypto-config", "", "path to the generated crypto-config directory")
	sourcePrefix := flag.String("source-prefix", "generated_crypto_config", "path label stored in evidence")
	flag.Parse()

	if _, ok := map[string]bool{"ecdsa": true, "ml-dsa-44": true, "ml-dsa-65": true, "sphincs": true}[*config]; !ok {
		return fmt.Errorf("unsupported configuration %q", *config)
	}
	if *runLabel == "" || *cryptoConfig == "" || *sourcePrefix == "" {
		return errors.New("--run-label, --crypto-config, and --source-prefix are required")
	}

	pattern := filepath.Join(
		*cryptoConfig,
		"peerOrganizations", "*", "peers", "*", "msp", "signcerts", "*.pem",
	)
	paths, err := filepath.Glob(pattern)
	if err != nil {
		return err
	}
	sort.Strings(paths)
	if len(paths) != 4 {
		return fmt.Errorf("expected four peer signcerts, found %d", len(paths))
	}

	writer := csv.NewWriter(os.Stdout)
	if err := writer.Write([]string{
		"config", "run_label", "peer", "source_type", "source_path",
		"public_key_representation", "bytes", "sha256",
	}); err != nil {
		return err
	}
	for _, path := range paths {
		evidence, err := inspectCertificate(path, *cryptoConfig, *sourcePrefix, *config)
		if err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		digest := sha256.Sum256(evidence.bytes)
		if err := writer.Write([]string{
			*config,
			*runLabel,
			evidence.peer,
			evidence.sourceType,
			evidence.sourcePath,
			evidence.representation,
			strconv.Itoa(len(evidence.bytes)),
			hex.EncodeToString(digest[:]),
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
