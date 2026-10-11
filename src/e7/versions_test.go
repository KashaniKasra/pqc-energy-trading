package e7

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestCollectL2CryptoVersionsUsesNativeAndGoModuleQueries(t *testing.T) {
	type call struct {
		directory, name string
		arguments       []string
	}
	var calls []call
	run := func(directory, name string, arguments ...string) (string, error) {
		calls = append(calls, call{directory, name, append([]string(nil), arguments...)})
		if name == "pkg-config" {
			return " 0.15.0\n", nil
		}
		return "v0.15.0\n", nil
	}
	versions, err := CollectL2CryptoVersions("/repo/src/e7", run)
	if err != nil {
		t.Fatal(err)
	}
	if versions["liboqs_go"] != "v0.15.0" || len(calls) != 2 {
		t.Fatalf("versions=%v calls=%v", versions, calls)
	}
	if !reflect.DeepEqual(calls[0].arguments, []string{"--modversion", "liboqs"}) ||
		!reflect.DeepEqual(calls[1].arguments, []string{"list", "-m", "-f", "{{.Version}}", "github.com/open-quantum-safe/liboqs-go"}) {
		t.Fatalf("wrong version commands: %+v", calls)
	}
}

func TestCollectL2CryptoVersionsFailsClosed(t *testing.T) {
	for _, test := range []struct {
		name, native, module string
		fail                 bool
	}{
		{"wrong native", "0.14.0", "v0.15.0", false},
		{"wrong module", "0.15.0", "v0.14.0", false},
		{"command failure", "", "", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			run := func(_ string, name string, _ ...string) (string, error) {
				if test.fail {
					return "", errors.New("injected failure")
				}
				if name == "pkg-config" {
					return test.native, nil
				}
				return test.module, nil
			}
			if _, err := CollectL2CryptoVersions("/module", run); err == nil {
				t.Fatal("invalid version provenance accepted")
			}
		})
	}
}

func makePeerBinary(t *testing.T, base string, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(base, "bin", "peer")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("test peer\n"), mode); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestResolveFabricPeerBinaryPolicy(t *testing.T) {
	explicitBase := filepath.Join(t.TempDir(), "explicit-samples")
	explicitPeer := makePeerBinary(t, explicitBase, 0o755)
	resolved, err := ResolveFabricPeerBinary(explicitBase, "/ignored/home")
	if err != nil || resolved != explicitPeer {
		t.Fatalf("explicit FABRIC_SAMPLES resolution=%q err=%v", resolved, err)
	}

	home := t.TempDir()
	defaultPeer := makePeerBinary(t, filepath.Join(home, "projects", "fabric-samples"), 0o755)
	resolved, err = ResolveFabricPeerBinary("", home)
	if err != nil || resolved != defaultPeer {
		t.Fatalf("default HOME resolution=%q err=%v", resolved, err)
	}
	if _, err := ResolveFabricPeerBinary("", ""); err == nil {
		t.Fatal("missing HOME accepted")
	}
	if _, err := ResolveFabricPeerBinary(filepath.Join(t.TempDir(), "missing"), home); err == nil {
		t.Fatal("missing Fabric peer binary accepted")
	}
	nonExecutable := filepath.Join(t.TempDir(), "non-executable")
	makePeerBinary(t, nonExecutable, 0o644)
	if _, err := ResolveFabricPeerBinary(nonExecutable, home); err == nil {
		t.Fatal("non-executable Fabric peer binary accepted")
	}
}

func TestParseAndValidateFabricPeerVersion(t *testing.T) {
	output := "peer:\n Version: 2.5.16\n Commit SHA: abcdef\n"
	version, err := ParseFabricPeerVersion(output)
	if err != nil || version != FabricVersion {
		t.Fatalf("version=%q err=%v", version, err)
	}
	version, err = ParseFabricPeerVersion("peer:\n Version: v2.5.16\n")
	if err != nil || version != FabricVersion {
		t.Fatalf("v-prefixed version=%q err=%v", version, err)
	}
	for _, malformed := range []string{"", "peer: no version", "Version 2.5.16", "Version: 2.5.16 extra", "Version: 2.5.16\nVersion: 2.5.16"} {
		if _, err := ParseFabricPeerVersion(malformed); err == nil {
			t.Fatalf("malformed output accepted: %q", malformed)
		}
	}
	if ValidateFabricPeerVersion("2.5.15") == nil {
		t.Fatal("wrong Fabric version accepted")
	}
}

func TestCollectFabricPeerVersionCommandAndFailures(t *testing.T) {
	base := t.TempDir()
	peer := makePeerBinary(t, base, 0o755)
	called := false
	path, version, err := CollectFabricPeerVersion(base, "", func(name string, arguments ...string) (string, error) {
		called = true
		if name != peer || !reflect.DeepEqual(arguments, []string{"version"}) {
			t.Fatalf("wrong command: %s %v", name, arguments)
		}
		return "peer:\n Version: 2.5.16\n", nil
	})
	if err != nil || !called || path != peer || version != FabricVersion {
		t.Fatalf("path=%q version=%q called=%v err=%v", path, version, called, err)
	}
	if _, _, err := CollectFabricPeerVersion(base, "", func(string, ...string) (string, error) {
		return "", errors.New("injected command failure")
	}); err == nil {
		t.Fatal("Fabric peer command failure accepted")
	}
	if _, _, err := CollectFabricPeerVersion(base, "", func(string, ...string) (string, error) {
		return "peer:\n Version: 2.5.15\n", nil
	}); err == nil {
		t.Fatal("wrong collected Fabric version accepted")
	}
}
