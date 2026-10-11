package e7

import (
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	ExpectedFabricCPUSet          = "2,4,6,8,10,12,14"
	ChaincodeLaunchCPUSetVariable = "CORE_VM_DOCKER_HOSTCONFIG_CPUSETCPUS"
)

var simpleKVPackageID = regexp.MustCompile(`^simplekv_1:[0-9a-f]{64}$`)

type FabricCPUComponent struct {
	Role                  string `json:"role"`
	Container             string `json:"container"`
	CPUSet                string `json:"cpuset"`
	ChaincodeLaunchCPUSet string `json:"chaincode_launch_cpuset,omitempty"`
	ChaincodeID           string `json:"chaincode_id,omitempty"`
}

type FabricCPUIsolation struct {
	ExpectedCPUSet string               `json:"expected_fabric_cpu_set"`
	Components     []FabricCPUComponent `json:"components"`
}

type DockerContainerInspection struct {
	Name   string `json:"Name"`
	Config struct {
		Env []string `json:"Env"`
	} `json:"Config"`
	HostConfig struct {
		CPUSet string `json:"CpusetCpus"`
	} `json:"HostConfig"`
	State struct {
		Running bool `json:"Running"`
	} `json:"State"`
}

type DockerCommand func(name string, arguments ...string) ([]byte, error)

func systemDockerCommand(name string, arguments ...string) ([]byte, error) {
	return exec.Command(name, arguments...).CombinedOutput()
}

func NormalizeCPUSet(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", errors.New("empty Docker CPU set")
	}
	seen := map[int]bool{}
	for _, part := range strings.Split(value, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return "", errors.New("malformed Docker CPU set")
		}
		bounds := strings.Split(part, "-")
		if len(bounds) > 2 {
			return "", errors.New("malformed Docker CPU range")
		}
		start, err := strconv.Atoi(bounds[0])
		if err != nil || start < 0 {
			return "", errors.New("invalid Docker CPU")
		}
		end := start
		if len(bounds) == 2 {
			end, err = strconv.Atoi(bounds[1])
			if err != nil || end < start {
				return "", errors.New("invalid Docker CPU range")
			}
		}
		for cpu := start; cpu <= end; cpu++ {
			seen[cpu] = true
		}
	}
	values := make([]int, 0, len(seen))
	for cpu := range seen {
		values = append(values, cpu)
	}
	sort.Ints(values)
	parts := make([]string, len(values))
	for index, cpu := range values {
		parts[index] = strconv.Itoa(cpu)
	}
	return strings.Join(parts, ","), nil
}

func chaincodePackageID(environment []string) string {
	for _, entry := range environment {
		if strings.HasPrefix(entry, "CORE_CHAINCODE_ID_NAME=") {
			return strings.TrimPrefix(entry, "CORE_CHAINCODE_ID_NAME=")
		}
	}
	return ""
}

func environmentVariable(environment []string, name string) (string, error) {
	prefix := name + "="
	value := ""
	found := false
	for _, entry := range environment {
		if !strings.HasPrefix(entry, prefix) {
			continue
		}
		if found {
			return "", fmt.Errorf("duplicate Docker environment variable %s", name)
		}
		value = strings.TrimPrefix(entry, prefix)
		found = true
	}
	if !found {
		return "", fmt.Errorf("missing Docker environment variable %s", name)
	}
	return value, nil
}

func ValidateFabricCPUContainers(containers []DockerContainerInspection) (FabricCPUIsolation, error) {
	expectedRoles := map[string]string{
		"orderer.example.com":    "orderer",
		"peer0.org1.example.com": "peer0.org1",
		"peer1.org1.example.com": "peer1.org1",
		"peer0.org2.example.com": "peer0.org2",
		"peer1.org2.example.com": "peer1.org2",
	}
	found := map[string]bool{}
	result := FabricCPUIsolation{ExpectedCPUSet: ExpectedFabricCPUSet}
	for _, container := range containers {
		name := strings.TrimPrefix(container.Name, "/")
		role, required := expectedRoles[name]
		packageID := chaincodePackageID(container.Config.Env)
		chaincode := simpleKVPackageID.MatchString(packageID)
		if !required && !chaincode {
			continue
		}
		if !container.State.Running {
			return FabricCPUIsolation{}, fmt.Errorf("required Fabric container %s is not running", name)
		}
		normalized, err := NormalizeCPUSet(container.HostConfig.CPUSet)
		if err != nil || normalized != ExpectedFabricCPUSet {
			return FabricCPUIsolation{}, fmt.Errorf("Fabric container %s cpuset=%q, want %s", name, container.HostConfig.CPUSet, ExpectedFabricCPUSet)
		}
		component := FabricCPUComponent{Role: role, Container: name, CPUSet: normalized}
		if chaincode {
			component.Role = "simplekv_chaincode"
			component.ChaincodeID = packageID
		} else if required && role != "orderer" {
			launchCPUSet, environmentErr := environmentVariable(container.Config.Env, ChaincodeLaunchCPUSetVariable)
			if environmentErr != nil {
				return FabricCPUIsolation{}, fmt.Errorf("Fabric peer %s chaincode launch policy: %w", name, environmentErr)
			}
			normalizedLaunchCPUSet, normalizeErr := NormalizeCPUSet(launchCPUSet)
			if normalizeErr != nil || normalizedLaunchCPUSet != ExpectedFabricCPUSet {
				return FabricCPUIsolation{}, fmt.Errorf("Fabric peer %s chaincode launch cpuset=%q, want %s", name, launchCPUSet, ExpectedFabricCPUSet)
			}
			component.ChaincodeLaunchCPUSet = normalizedLaunchCPUSet
		}
		if required {
			if found[role] {
				return FabricCPUIsolation{}, fmt.Errorf("duplicate Fabric component %s", role)
			}
			found[role] = true
		}
		result.Components = append(result.Components, component)
	}
	for _, role := range expectedRoles {
		if !found[role] {
			return FabricCPUIsolation{}, fmt.Errorf("missing required Fabric component %s", role)
		}
	}
	hasChaincode := false
	for _, component := range result.Components {
		if component.Role == "simplekv_chaincode" {
			hasChaincode = true
		}
	}
	if !hasChaincode {
		return FabricCPUIsolation{}, errors.New("missing running simplekv chaincode container")
	}
	sort.Slice(result.Components, func(left, right int) bool {
		if result.Components[left].Role == result.Components[right].Role {
			return result.Components[left].Container < result.Components[right].Container
		}
		return result.Components[left].Role < result.Components[right].Role
	})
	if err := ValidateFabricCPUIsolationRecord(result); err != nil {
		return FabricCPUIsolation{}, err
	}
	return result, nil
}

func ValidateFabricCPUIsolationRecord(record FabricCPUIsolation) error {
	if record.ExpectedCPUSet != ExpectedFabricCPUSet {
		return errors.New("wrong expected Fabric CPU set")
	}
	required := map[string]string{
		"orderer":    "orderer.example.com",
		"peer0.org1": "peer0.org1.example.com",
		"peer1.org1": "peer1.org1.example.com",
		"peer0.org2": "peer0.org2.example.com",
		"peer1.org2": "peer1.org2.example.com",
	}
	found := map[string]bool{}
	chaincodes := 0
	seenContainers := map[string]bool{}
	for _, component := range record.Components {
		if component.Container == "" || seenContainers[component.Container] {
			return errors.New("invalid or duplicate Fabric CPU component container")
		}
		seenContainers[component.Container] = true
		normalized, err := NormalizeCPUSet(component.CPUSet)
		if err != nil || normalized != ExpectedFabricCPUSet || component.CPUSet != normalized {
			return errors.New("Fabric CPU component does not record the exact canonical CPU set")
		}
		if component.Role == "simplekv_chaincode" {
			if !simpleKVPackageID.MatchString(component.ChaincodeID) || component.ChaincodeLaunchCPUSet != "" {
				return errors.New("invalid simplekv chaincode CPU component identity")
			}
			chaincodes++
			continue
		}
		expectedContainer, ok := required[component.Role]
		if !ok || found[component.Role] || component.Container != expectedContainer || component.ChaincodeID != "" {
			return errors.New("invalid Fabric CPU component role")
		}
		if component.Role == "orderer" {
			if component.ChaincodeLaunchCPUSet != "" {
				return errors.New("orderer must not record a chaincode launch CPU set")
			}
		} else if component.ChaincodeLaunchCPUSet != ExpectedFabricCPUSet {
			return errors.New("peer does not record the exact canonical chaincode launch CPU set")
		}
		found[component.Role] = true
	}
	for role := range required {
		if !found[role] {
			return errors.New("missing required Fabric CPU component provenance")
		}
	}
	if chaincodes == 0 {
		return errors.New("missing simplekv chaincode CPU provenance")
	}
	return nil
}

func InspectFabricCPUIsolation(run DockerCommand) (FabricCPUIsolation, error) {
	if run == nil {
		return FabricCPUIsolation{}, errors.New("Docker command runner is required")
	}
	output, err := run("docker", "ps", "-q")
	if err != nil {
		return FabricCPUIsolation{}, fmt.Errorf("list running Docker containers: %w", err)
	}
	identifiers := strings.Fields(string(output))
	if len(identifiers) == 0 {
		return FabricCPUIsolation{}, errors.New("no running Docker containers")
	}
	arguments := append([]string{"inspect"}, identifiers...)
	output, err = run("docker", arguments...)
	if err != nil {
		return FabricCPUIsolation{}, fmt.Errorf("inspect Docker containers: %w", err)
	}
	var containers []DockerContainerInspection
	if err := json.Unmarshal(output, &containers); err != nil {
		return FabricCPUIsolation{}, fmt.Errorf("parse Docker inspection: %w", err)
	}
	return ValidateFabricCPUContainers(containers)
}

func InspectSystemFabricCPUIsolation() (FabricCPUIsolation, error) {
	return InspectFabricCPUIsolation(systemDockerCommand)
}
