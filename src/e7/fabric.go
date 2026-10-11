package e7

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	client "github.com/hyperledger/fabric-gateway/pkg/client"
	"github.com/hyperledger/fabric-gateway/pkg/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

const (
	FabricGatewayVersion = "1.7.1"
	FabricVersion        = "2.5.16"
	FabricChannel        = "energychannel"
	FabricChaincode      = "simplekv"
)

type FabricGatewayConfig struct {
	E7Config         Configuration
	IdentityMode     string
	MSPID            string
	PeerEndpoint     string
	PeerServerName   string
	TLSCACertPath    string
	IdentityCertPath string
	IdentityKeyPath  string
	Channel          string
	Chaincode        string
}

func DefaultFabricGatewayConfig(projectRoot string, config Configuration) (FabricGatewayConfig, error) {
	mode := ""
	switch config {
	case ConfigurationL1Only:
		mode = "ml-dsa-65"
	case ConfigurationSETSReimpl:
		mode = "ecdsa"
	default:
		return FabricGatewayConfig{}, errors.New("Fabric Gateway is only valid for on-chain E7 configs")
	}
	base := filepath.Join(projectRoot, "env", "fabric", "e1", "crypto-config", "peerOrganizations", "org1.example.com")
	user := filepath.Join(base, "users", "User1@org1.example.com", "msp")
	return FabricGatewayConfig{
		E7Config: config, IdentityMode: mode, MSPID: "Org1MSP",
		PeerEndpoint: "localhost:7051", PeerServerName: "peer0.org1.example.com",
		TLSCACertPath:    filepath.Join(base, "peers", "peer0.org1.example.com", "tls", "ca.crt"),
		IdentityCertPath: filepath.Join(user, "signcerts", "User1@org1.example.com-cert.pem"),
		IdentityKeyPath:  filepath.Join(user, "keystore", "priv_sk"),
		Channel:          FabricChannel, Chaincode: FabricChaincode,
	}, nil
}

func (config FabricGatewayConfig) Validate() error {
	if !config.E7Config.Valid() || config.MSPID == "" || config.PeerEndpoint == "" || config.PeerServerName == "" || config.TLSCACertPath == "" || config.IdentityCertPath == "" || config.IdentityKeyPath == "" || config.Channel != FabricChannel || config.Chaincode != FabricChaincode {
		return errors.New("invalid E7 Fabric Gateway configuration")
	}
	want, err := DefaultFabricGatewayConfig(".", config.E7Config)
	if err != nil || config.IdentityMode != want.IdentityMode {
		return errors.New("E7 Fabric identity mode/config mismatch")
	}
	return nil
}

type RealFabricSubmitter struct {
	config     FabricGatewayConfig
	connection *grpc.ClientConn
	gateway    *client.Gateway
	contract   *client.Contract
}

var _ FabricSubmitter = (*RealFabricSubmitter)(nil)

func NewRealFabricSubmitter(config FabricGatewayConfig) (*RealFabricSubmitter, error) {
	if err := config.Validate(); err != nil {
		return nil, err
	}
	return &RealFabricSubmitter{config: config}, nil
}

func (submitter *RealFabricSubmitter) PrepareRun(ctx context.Context, config Configuration) error {
	_ = ctx
	if config != submitter.config.E7Config || submitter.gateway != nil {
		return errors.New("Fabric Gateway config mismatch or already prepared")
	}
	certPool, err := loadCertPool(submitter.config.TLSCACertPath)
	if err != nil {
		return err
	}
	connection, err := grpc.NewClient(submitter.config.PeerEndpoint, grpc.WithTransportCredentials(credentials.NewClientTLSFromCert(certPool, submitter.config.PeerServerName)))
	if err != nil {
		return fmt.Errorf("connect Fabric peer: %w", err)
	}
	certificate, err := loadX509Certificate(submitter.config.IdentityCertPath)
	if err != nil {
		connection.Close()
		return err
	}
	id, err := identity.NewX509Identity(submitter.config.MSPID, certificate)
	if err != nil {
		connection.Close()
		return err
	}
	keyPEM, err := os.ReadFile(submitter.config.IdentityKeyPath)
	if err != nil {
		connection.Close()
		return fmt.Errorf("read Fabric identity key: %w", err)
	}
	privateKey, err := identity.PrivateKeyFromPEM(keyPEM)
	if err != nil {
		connection.Close()
		return fmt.Errorf("parse Fabric identity key: %w", err)
	}
	sign, err := identity.NewPrivateKeySign(privateKey)
	if err != nil {
		connection.Close()
		return err
	}
	gateway, err := client.Connect(id, client.WithSign(sign), client.WithClientConnection(connection), client.WithEvaluateTimeout(30*time.Second), client.WithEndorseTimeout(30*time.Second), client.WithSubmitTimeout(30*time.Second), client.WithCommitStatusTimeout(60*time.Second))
	if err != nil {
		connection.Close()
		return fmt.Errorf("initialize Fabric Gateway: %w", err)
	}
	submitter.connection, submitter.gateway = connection, gateway
	submitter.contract = gateway.GetNetwork(submitter.config.Channel).GetContract(submitter.config.Chaincode)
	return nil
}

func (submitter *RealFabricSubmitter) SubmitAndAwaitCommit(ctx context.Context, key string, value []byte) (FabricCompletion, error) {
	if submitter.contract == nil || key == "" || len(value) == 0 {
		return FabricCompletion{}, errors.New("Fabric Gateway is not prepared or request is empty")
	}
	_, commit, err := submitter.contract.SubmitAsyncWithContext(ctx, "Set", client.WithArguments(key, string(value)))
	if err != nil {
		return FabricCompletion{}, fmt.Errorf("submit simplekv.Set: %w", err)
	}
	status, err := commit.StatusWithContext(ctx)
	if err != nil {
		return FabricCompletion{TransactionID: commit.TransactionID()}, fmt.Errorf("await Fabric commit status: %w", err)
	}
	return FabricCompletion{TransactionID: status.TransactionID, Committed: true, Verified: status.Successful, ValidationStatus: status.Code.String(), ValidationCode: int(status.Code), BlockNumber: status.BlockNumber}, nil
}

func (submitter *RealFabricSubmitter) Close() error {
	if submitter.gateway != nil {
		submitter.gateway.Close()
		submitter.gateway = nil
	}
	if submitter.connection != nil {
		err := submitter.connection.Close()
		submitter.connection = nil
		return err
	}
	return nil
}

func loadCertPool(path string) (*x509.CertPool, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read Fabric TLS CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(content) {
		return nil, errors.New("Fabric TLS CA PEM is invalid")
	}
	return pool, nil
}

func loadX509Certificate(path string) (*x509.Certificate, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read Fabric identity certificate: %w", err)
	}
	block, rest := pem.Decode(content)
	if block == nil || len(rest) != 0 {
		return nil, errors.New("Fabric identity certificate PEM is invalid")
	}
	certificate, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return nil, err
	}
	return certificate, nil
}
