package e7

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	ExpectedLiboqsVersion   = "0.15.0"
	ExpectedLiboqsGoVersion = "v0.15.0"
)

var fabricVersionPattern = regexp.MustCompile(`^v?([0-9]+\.[0-9]+\.[0-9]+)$`)

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

type FabricVersionCommand func(peerBinary string, arguments ...string) (string, error)

func ResolveFabricPeerBinary(fabricSamples, home string) (string, error) {
	base := fabricSamples
	if base == "" {
		if home == "" {
			return "", errors.New("HOME is required when FABRIC_SAMPLES is unset")
		}
		base = filepath.Join(home, "projects", "fabric-samples")
	}
	peerBinary := filepath.Join(base, "bin", "peer")
	info, err := os.Stat(peerBinary)
	if err != nil {
		return "", fmt.Errorf("resolve Fabric peer binary %s: %w", peerBinary, err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("Fabric peer binary is not a regular file: %s", peerBinary)
	}
	if info.Mode().Perm()&0o111 == 0 {
		return "", fmt.Errorf("Fabric peer binary is not executable: %s", peerBinary)
	}
	return peerBinary, nil
}

func ParseFabricPeerVersion(output string) (string, error) {
	if strings.TrimSpace(output) == "" {
		return "", errors.New("Fabric peer returned empty version output")
	}
	version := ""
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "Version:") {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		candidate := strings.TrimSpace(parts[1])
		match := fabricVersionPattern.FindStringSubmatch(candidate)
		if version != "" || match == nil {
			return "", errors.New("malformed Fabric peer version output")
		}
		version = match[1]
	}
	if version == "" {
		return "", errors.New("Fabric peer version line is missing")
	}
	return version, nil
}

func ValidateFabricPeerVersion(version string) error {
	if version != FabricVersion {
		return fmt.Errorf("unexpected Fabric version %q, want %s", version, FabricVersion)
	}
	return nil
}

func CollectFabricPeerVersion(fabricSamples, home string, run FabricVersionCommand) (string, string, error) {
	if run == nil {
		return "", "", errors.New("Fabric version command runner is required")
	}
	peerBinary, err := ResolveFabricPeerBinary(fabricSamples, home)
	if err != nil {
		return "", "", err
	}
	output, err := run(peerBinary, "version")
	if err != nil {
		return "", "", fmt.Errorf("query Fabric peer version: %w", err)
	}
	version, err := ParseFabricPeerVersion(output)
	if err != nil {
		return "", "", err
	}
	if err := ValidateFabricPeerVersion(version); err != nil {
		return "", "", err
	}
	return peerBinary, version, nil
}

func systemFabricVersionCommand(peerBinary string, arguments ...string) (string, error) {
	output, err := exec.Command(peerBinary, arguments...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("%s: %w", peerBinary, err)
	}
	return string(output), nil
}

func CollectSystemFabricPeerVersion() (string, string, error) {
	return CollectFabricPeerVersion(os.Getenv("FABRIC_SAMPLES"), os.Getenv("HOME"), systemFabricVersionCommand)
}
