package e7

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func inspectedContainer(name, cpuSet string, environment ...string) DockerContainerInspection {
	var container DockerContainerInspection
	container.Name = "/" + name
	container.HostConfig.CPUSet = cpuSet
	container.Config.Env = environment
	container.State.Running = true
	return container
}

func validFabricCPUContainers(cpuSet string) []DockerContainerInspection {
	peerEnvironment := []string{ChaincodeLaunchCPUSetVariable + "=" + cpuSet}
	return []DockerContainerInspection{
		inspectedContainer("orderer.example.com", cpuSet),
		inspectedContainer("peer0.org1.example.com", cpuSet, peerEnvironment...),
		inspectedContainer("peer1.org1.example.com", cpuSet, peerEnvironment...),
		inspectedContainer("peer0.org2.example.com", cpuSet, peerEnvironment...),
		inspectedContainer("peer1.org2.example.com", cpuSet, peerEnvironment...),
		inspectedContainer("dev-peer0.org1-simplekv", cpuSet,
			"CORE_CHAINCODE_ID_NAME=simplekv_1:"+strings.Repeat("a", 64)),
	}
}

func TestFabricCPUSetExactAndReorderedAccepted(t *testing.T) {
	for _, cpuSet := range []string{ExpectedFabricCPUSet, "14,2,12,4,10,6,8"} {
		record, err := ValidateFabricCPUContainers(validFabricCPUContainers(cpuSet))
		if err != nil {
			t.Fatalf("cpuset %q rejected: %v", cpuSet, err)
		}
		if record.ExpectedCPUSet != ExpectedFabricCPUSet {
			t.Fatal("wrong expected CPU set recorded")
		}
		for _, component := range record.Components {
			if component.CPUSet != ExpectedFabricCPUSet {
				t.Fatalf("non-canonical CPU set retained: %q", component.CPUSet)
			}
			if strings.HasPrefix(component.Role, "peer") && component.ChaincodeLaunchCPUSet != ExpectedFabricCPUSet {
				t.Fatalf("peer launch CPU set was not canonicalized: %q", component.ChaincodeLaunchCPUSet)
			}
			if (component.Role == "orderer" || component.Role == "simplekv_chaincode") && component.ChaincodeLaunchCPUSet != "" {
				t.Fatalf("%s fabricated a chaincode launch CPU set", component.Role)
			}
		}
	}
}

func TestFabricPeerChaincodeLaunchCPUSetFailsClosed(t *testing.T) {
	tests := map[string]string{
		"missing":   "",
		"broad":     ChaincodeLaunchCPUSetVariable + "=2-15",
		"malformed": ChaincodeLaunchCPUSetVariable + "=2,,4",
	}
	for name, environment := range tests {
		t.Run(name, func(t *testing.T) {
			containers := validFabricCPUContainers(ExpectedFabricCPUSet)
			if environment == "" {
				containers[1].Config.Env = nil
			} else {
				containers[1].Config.Env = []string{environment}
			}
			if _, err := ValidateFabricCPUContainers(containers); err == nil {
				t.Fatal("invalid peer chaincode launch CPU policy accepted")
			}
		})
	}
}

func TestFabricCPUIsolationRejectsInvalidComponents(t *testing.T) {
	tests := map[string]func([]DockerContainerInspection) []DockerContainerInspection{
		"broad cpuset": func(value []DockerContainerInspection) []DockerContainerInspection {
			value[0].HostConfig.CPUSet = "2-15"
			return value
		},
		"missing cpuset": func(value []DockerContainerInspection) []DockerContainerInspection {
			value[1].HostConfig.CPUSet = ""
			return value
		},
		"missing peer": func(value []DockerContainerInspection) []DockerContainerInspection {
			return append(value[:2], value[3:]...)
		},
		"missing orderer": func(value []DockerContainerInspection) []DockerContainerInspection {
			return value[1:]
		},
		"missing chaincode": func(value []DockerContainerInspection) []DockerContainerInspection {
			return value[:5]
		},
		"wrong chaincode cpuset": func(value []DockerContainerInspection) []DockerContainerInspection {
			value[5].HostConfig.CPUSet = "2-15"
			return value
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := ValidateFabricCPUContainers(mutate(validFabricCPUContainers(ExpectedFabricCPUSet))); err == nil {
				t.Fatal("invalid Fabric CPU placement accepted")
			}
		})
	}
}

func TestFabricCPUIsolationIgnoresIrrelevantContainer(t *testing.T) {
	containers := append(validFabricCPUContainers(ExpectedFabricCPUSet),
		inspectedContainer("unrelated-service", "", "UNRELATED=true"))
	if _, err := ValidateFabricCPUContainers(containers); err != nil {
		t.Fatal(err)
	}
}

func TestInspectFabricCPUIsolationUsesInjectedDockerInspection(t *testing.T) {
	containers := validFabricCPUContainers(ExpectedFabricCPUSet)
	encoded, err := json.Marshal(containers)
	if err != nil {
		t.Fatal(err)
	}
	calls := 0
	record, err := InspectFabricCPUIsolation(func(name string, arguments ...string) ([]byte, error) {
		calls++
		if name != "docker" {
			return nil, errors.New("unexpected command")
		}
		if calls == 1 {
			return []byte("one two three four five six\n"), nil
		}
		return encoded, nil
	})
	if err != nil || calls != 2 || len(record.Components) != 6 {
		t.Fatalf("injected inspection failed: calls=%d components=%d err=%v", calls, len(record.Components), err)
	}
}
