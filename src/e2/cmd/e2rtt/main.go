package main

import (
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"time"

	e2 "pqc-energy-trading/e2"
)

func main() {
	if len(os.Args) < 2 {
		fatal(errors.New("usage: e2rtt server|client [options]"))
	}
	var err error
	switch os.Args[1] {
	case "server":
		err = runServer(os.Args[2:])
	case "client":
		err = runClient(os.Args[2:])
	default:
		err = fmt.Errorf("unknown e2rtt role %q", os.Args[1])
	}
	if err != nil {
		fatal(err)
	}
}

func parseCondition(configurationText, transitionText string) (e2.Configuration, e2.TransitionType, error) {
	configuration, err := e2.ParseConfiguration(configurationText)
	if err != nil {
		return "", "", err
	}
	transition, err := e2.ParseTransitionType(transitionText)
	return configuration, transition, err
}

func runServer(arguments []string) error {
	flags := flag.NewFlagSet("server", flag.ContinueOnError)
	configurationText := flags.String("config", "", "E2 configuration")
	transitionText := flags.String("transition", "", "E2 transition")
	warmup := flags.Int("warmup", 0, "warm-up iterations")
	iterations := flags.Int("iterations", 0, "measured iterations")
	listenAddress := flags.String("listen", "", "TCP listen address")
	readyFile := flags.String("ready-file", "", "exclusive readiness marker")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	configuration, transitionType, err := parseCondition(*configurationText, *transitionText)
	if err != nil {
		return err
	}
	if *warmup < 0 || *iterations <= 0 || *listenAddress == "" || *readyFile == "" {
		return errors.New("server requires valid iteration counts, listen address, and ready file")
	}
	transition, err := e2.TransitionForMeasurement(configuration, transitionType)
	if err != nil {
		return err
	}
	verifier, err := e2.NewCanonicalTransactionVerifier(configuration, transition)
	if err != nil {
		return err
	}
	defer verifier.Close()
	listener, err := net.Listen("tcp", *listenAddress)
	if err != nil {
		return err
	}
	defer listener.Close()
	marker, err := os.OpenFile(*readyFile, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	marker.Close()
	connection, err := listener.Accept()
	if err != nil {
		return err
	}
	defer connection.Close()
	return e2.ServeVerification(connection, verifier, *warmup+*iterations)
}

func runClient(arguments []string) error {
	flags := flag.NewFlagSet("client", flag.ContinueOnError)
	configurationText := flags.String("config", "", "E2 configuration")
	transitionText := flags.String("transition", "", "E2 transition")
	warmup := flags.Int("warmup", 0, "warm-up iterations")
	iterations := flags.Int("iterations", 0, "measured iterations")
	connectAddress := flags.String("connect", "", "node B TCP address")
	sourceAddress := flags.String("source", "", "node A source IP")
	output := flags.String("output", "", "exclusive raw CSV output")
	scientific := flags.Bool("scientific", false, "enforce scientific provenance")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	configuration, transitionType, err := parseCondition(*configurationText, *transitionText)
	if err != nil {
		return err
	}
	if *connectAddress == "" || *sourceAddress == "" || *output == "" {
		return errors.New("client requires connect, source, and output paths")
	}
	backend, err := e2.NewRealCryptoBackend(configuration, []e2.PartyID{"alice", "bob"})
	if err != nil {
		return err
	}
	defer backend.Close()
	dialer := net.Dialer{
		LocalAddr: &net.TCPAddr{IP: net.ParseIP(*sourceAddress)},
		Timeout:   20 * time.Second,
	}
	connection, err := dialer.Dial("tcp", *connectAddress)
	if err != nil {
		return err
	}
	defer connection.Close()
	executor, err := e2.NewPersistentRTTExecutor(connection)
	if err != nil {
		return err
	}
	records, err := e2.RunConditionMeasurements(
		configuration,
		transitionType,
		backend,
		e2.CanonicalSerializer{},
		executor,
		e2.RunOptions{
			WarmupIterations:   *warmup,
			MeasuredIterations: *iterations,
			Scientific:         *scientific,
		},
	)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(*output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if err := e2.WriteRawCSV(file, records); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "ERROR:", err)
	os.Exit(1)
}
