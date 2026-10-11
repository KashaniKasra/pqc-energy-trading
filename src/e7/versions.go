package e7

import (
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

const (
	ExpectedLiboqsVersion   = "0.15.0"
	ExpectedLiboqsGoVersion = "v0.15.0"
)

type VersionCommand func(directory, name string, arguments ...string) (string, error)

func systemVersionCommand(directory, name string, arguments ...string) (string, error) {
	command := exec.Command(name, arguments...)
	command.Dir = directory
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s: %w", name, err)
	}
	value := strings.TrimSpace(string(output))
	if value == "" {
		return "", fmt.Errorf("%s returned an empty version", name)
	}
	return value, nil
}

// CollectL2CryptoVersions deliberately queries native liboqs through
// pkg-config and liboqs-go through Go module metadata. liboqs-go is not a
// native pkg-config package.
func CollectL2CryptoVersions(moduleDirectory string, run VersionCommand) (map[string]string, error) {
	if moduleDirectory == "" || run == nil {
		return nil, errors.New("invalid E7 crypto version query")
	}
	liboqs, err := run(moduleDirectory, "pkg-config", "--modversion", "liboqs")
	if err != nil {
		return nil, fmt.Errorf("collect liboqs version: %w", err)
	}
	liboqsGo, err := run(moduleDirectory, "go", "list", "-m", "-f", "{{.Version}}", "github.com/open-quantum-safe/liboqs-go")
	if err != nil {
		return nil, fmt.Errorf("collect liboqs-go module version: %w", err)
	}
	liboqs, liboqsGo = strings.TrimSpace(liboqs), strings.TrimSpace(liboqsGo)
	if liboqs != ExpectedLiboqsVersion || liboqsGo != ExpectedLiboqsGoVersion {
		return nil, fmt.Errorf("unexpected crypto versions liboqs=%q liboqs-go=%q", liboqs, liboqsGo)
	}
	return map[string]string{"liboqs": liboqs, "liboqs_go": liboqsGo}, nil
}

func CollectSystemL2CryptoVersions(moduleDirectory string) (map[string]string, error) {
	return CollectL2CryptoVersions(moduleDirectory, systemVersionCommand)
}
