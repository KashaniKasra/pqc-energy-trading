package e7

import (
	"errors"
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
