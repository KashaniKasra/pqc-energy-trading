package e7

import (
	"context"
	"errors"
	"io"
	"os/exec"
)

const FabricResetMethod = "env/e7/fabric_network.sh start invokes E1 setup: compose down, regenerate identities/channel genesis, start peers/orderer, create energychannel, deploy simplekv"
const L2ResetMethod = "fresh e7bench process, fresh mirrored E2 channel/backend, synchronized loopback TCP accept before row 0"

type FabricLifecycle interface {
	Start(context.Context, Configuration, io.Writer) (FabricCPUIsolation, error)
	Stop(context.Context, Configuration, io.Writer) error
}

type CommandFabricLifecycle struct{ Script string }

func (l CommandFabricLifecycle) run(ctx context.Context, action string, config Configuration, output io.Writer) error {
	if l.Script == "" || (config != ConfigurationL1Only && config != ConfigurationSETSReimpl) {
		return errors.New("invalid E7 Fabric lifecycle request")
	}
	command := exec.CommandContext(ctx, l.Script, action, string(config))
	command.Stdout = output
	command.Stderr = output
	return command.Run()
}
func (l CommandFabricLifecycle) Start(ctx context.Context, c Configuration, w io.Writer) (FabricCPUIsolation, error) {
	if err := l.run(ctx, "start", c, w); err != nil {
		return FabricCPUIsolation{}, err
	}
	return InspectSystemFabricCPUIsolation()
}
func (l CommandFabricLifecycle) Stop(ctx context.Context, c Configuration, w io.Writer) error {
	return l.run(ctx, "stop", c, w)
}
