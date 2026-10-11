package e7

import (
	"path/filepath"
	"testing"
)

func TestDefaultFabricGatewayConfiguration(t *testing.T) {
	for _, test := range []struct {
		config Configuration
		mode   string
	}{
		{ConfigurationL1Only, "ml-dsa-65"},
		{ConfigurationSETSReimpl, "ecdsa"},
	} {
		configuration, err := DefaultFabricGatewayConfig("/repository", test.config)
		if err != nil {
			t.Fatal(err)
		}
		if configuration.IdentityMode != test.mode || configuration.Channel != "energychannel" || configuration.Chaincode != "simplekv" {
			t.Fatalf("configuration=%+v", configuration)
		}
		if configuration.IdentityCertPath != filepath.Join("/repository", "env/fabric/e1/crypto-config/peerOrganizations/org1.example.com/users/User1@org1.example.com/msp/signcerts/User1@org1.example.com-cert.pem") {
			t.Fatal("unexpected identity path")
		}
		if _, err := NewRealFabricSubmitter(configuration); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := DefaultFabricGatewayConfig(".", ConfigurationClassicalL2); err == nil {
		t.Fatal("L2 config accepted by Fabric adapter")
	}
}
