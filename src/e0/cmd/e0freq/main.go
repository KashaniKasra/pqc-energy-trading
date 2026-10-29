package main

/*
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <errno.h>
#include <time.h>

static int e0_frequency_monotonic_ns(int64_t *value) {
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		return -1;
	}
	*value = ((int64_t)ts.tv_sec * 1000000000LL) + (int64_t)ts.tv_nsec;
	return 0;
}

static int e0_sleep_until_monotonic_ns(int64_t value) {
	struct timespec deadline;
	deadline.tv_sec = value / 1000000000LL;
	deadline.tv_nsec = value % 1000000000LL;
	int result;
	do {
		result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL);
	} while (result == EINTR);
	return result;
}
*/
import "C"

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	outputBufferBytes    = 64 * 1024
	flushEverySamples    = 100
	stopPollIntervalNS   = int64(10_000_000)
	stabilizationSchema  = "pqc-energy-trading.e0-android-stabilization.v1"
	scientificIntervalMS = 100
	qualifyingSamples    = 5
)

type stabilizationObservation struct {
	ScheduledMonotonicNS int64 `json:"scheduled_monotonic_ns"`
	ActualMonotonicNS    int64 `json:"actual_monotonic_ns"`
	FrequencyKHz         int64 `json:"freq_khz"`
}

type stabilizationMessage struct {
	Schema               string                     `json:"schema"`
	Type                 string                     `json:"type"`
	Token                string                     `json:"token"`
	StartMonotonicNS     int64                      `json:"start_monotonic_ns"`
	QualifiedMonotonicNS int64                      `json:"qualified_monotonic_ns,omitempty"`
	WorkDurationNS       int64                      `json:"work_duration_ns,omitempty"`
	MinimumKHz           int64                      `json:"minimum_khz,omitempty"`
	MaximumKHz           int64                      `json:"maximum_khz,omitempty"`
	Observations         []stabilizationObservation `json:"observations,omitempty"`
	Error                string                     `json:"error,omitempty"`
}

type stabilizationStart struct {
	message stabilizationMessage
	conn    net.Conn
}

type stabilizationTracker struct {
	start       stabilizationMessage
	conn        net.Conn
	timeoutNS   int64
	consecutive []stabilizationObservation
	finished    bool
}

func stableFrequencyWindow(observations []stabilizationObservation) (int64, int64, bool) {
	if len(observations) != qualifyingSamples {
		return 0, 0, false
	}
	minimum, maximum := observations[0].FrequencyKHz, observations[0].FrequencyKHz
	for index, observation := range observations {
		if observation.FrequencyKHz <= 0 || (index > 0 &&
			observation.ScheduledMonotonicNS-observations[index-1].ScheduledMonotonicNS != 100_000_000) {
			return 0, 0, false
		}
		if observation.FrequencyKHz < minimum {
			minimum = observation.FrequencyKHz
		}
		if observation.FrequencyKHz > maximum {
			maximum = observation.FrequencyKHz
		}
	}
	return minimum, maximum, maximum*100 <= minimum*102
}

func (tracker *stabilizationTracker) resetSequence() {
	tracker.consecutive = tracker.consecutive[:0]
}

func (tracker *stabilizationTracker) observe(observation stabilizationObservation) (*stabilizationMessage, error) {
	if tracker.finished || observation.ActualMonotonicNS < tracker.start.StartMonotonicNS {
		return nil, nil
	}
	if observation.ActualMonotonicNS-tracker.start.StartMonotonicNS > tracker.timeoutNS {
		tracker.finished = true
		message := stabilizationMessage{Schema: stabilizationSchema, Type: "failed", Token: tracker.start.Token,
			StartMonotonicNS: tracker.start.StartMonotonicNS, Error: "stabilization timeout"}
		return &message, nil
	}
	tracker.consecutive = append(tracker.consecutive, observation)
	if len(tracker.consecutive) > qualifyingSamples {
		tracker.consecutive = tracker.consecutive[len(tracker.consecutive)-qualifyingSamples:]
	}
	if observation.ActualMonotonicNS-tracker.start.StartMonotonicNS < int64(3*time.Second) {
		return nil, nil
	}
	minimum, maximum, stable := stableFrequencyWindow(tracker.consecutive)
	if !stable {
		return nil, nil
	}
	tracker.finished = true
	window := append([]stabilizationObservation(nil), tracker.consecutive...)
	message := stabilizationMessage{
		Schema: stabilizationSchema, Type: "ready", Token: tracker.start.Token,
		StartMonotonicNS: tracker.start.StartMonotonicNS, QualifiedMonotonicNS: observation.ActualMonotonicNS,
		WorkDurationNS: observation.ActualMonotonicNS - tracker.start.StartMonotonicNS,
		MinimumKHz:     minimum, MaximumKHz: maximum, Observations: window,
	}
	return &message, nil
}

type seekReadCloser interface {
	io.Reader
	io.Seeker
	io.Closer
}

type frequencyReader struct {
	path           string
	open           func(string) (seekReadCloser, error)
	file           seekReadCloser
	firstRead      bool
	reopenEachRead bool
	buffer         [64]byte
}

func currentAllowedCPUs(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		if strings.HasPrefix(scanner.Text(), "Cpus_allowed_list:") {
			value := strings.TrimSpace(strings.TrimPrefix(scanner.Text(), "Cpus_allowed_list:"))
			if value == "" {
				return "", fmt.Errorf("empty Cpus_allowed_list")
			}
			return value, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("Cpus_allowed_list not found")
}

func monotonicNanoseconds() (int64, error) {
	var value C.int64_t
	if C.e0_frequency_monotonic_ns(&value) != 0 {
		return 0, fmt.Errorf("clock_gettime(CLOCK_MONOTONIC) failed")
	}
	return int64(value), nil
}

func sleepUntilMonotonic(deadlineNS int64) error {
	if result := C.e0_sleep_until_monotonic_ns(C.int64_t(deadlineNS)); result != 0 {
		return fmt.Errorf("clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME) failed: %d", int(result))
	}
	return nil
}

func createExclusive(path string) (*os.File, error) {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return nil, fmt.Errorf("create %s: %w", path, err)
	}
	return file, nil
}

func newFrequencyReader(path string) (*frequencyReader, error) {
	return newFrequencyReaderWithOpen(path, func(path string) (seekReadCloser, error) {
		return os.Open(path)
	})
}

func newFrequencyReaderWithOpen(path string, open func(string) (seekReadCloser, error)) (*frequencyReader, error) {
	file, err := open(path)
	if err != nil {
		return nil, err
	}
	return &frequencyReader{path: path, open: open, file: file, firstRead: true}, nil
}

func (reader *frequencyReader) Close() error {
	if reader.file == nil {
		return nil
	}
	return reader.file.Close()
}

func (reader *frequencyReader) reopen() error {
	if reader.file != nil {
		_ = reader.file.Close()
	}
	file, err := reader.open(reader.path)
	if err != nil {
		reader.file = nil
		return err
	}
	reader.file = file
	return nil
}

func (reader *frequencyReader) resetForRead() error {
	if reader.firstRead {
		reader.firstRead = false
		return nil
	}
	if !reader.reopenEachRead {
		if _, err := reader.file.Seek(0, io.SeekStart); err == nil {
			return nil
		}
		// sysfs normally supports seeking an attribute back to offset zero,
		// which invokes its show callback again. If a device kernel rejects
		// seeking, reopen for each subsequent sample rather than assuming
		// ordinary-file semantics or reusing a stale value.
		reader.reopenEachRead = true
	}
	return reader.reopen()
}

func parsePositiveDecimal(content []byte) (int64, error) {
	start := 0
	for start < len(content) && (content[start] == ' ' || content[start] == '\t' || content[start] == '\r' || content[start] == '\n') {
		start++
	}
	end := len(content)
	for end > start && (content[end-1] == ' ' || content[end-1] == '\t' || content[end-1] == '\r' || content[end-1] == '\n') {
		end--
	}
	content = content[start:end]
	if len(content) == 0 {
		return 0, fmt.Errorf("empty scaling_cur_freq value")
	}
	var value int64
	for _, character := range content {
		if character < '0' || character > '9' {
			return 0, fmt.Errorf("invalid scaling_cur_freq value %q", content)
		}
		digit := int64(character - '0')
		if value > (int64(^uint64(0)>>1)-digit)/10 {
			return 0, fmt.Errorf("scaling_cur_freq value overflows int64")
		}
		value = value*10 + digit
	}
	if value <= 0 {
		return 0, fmt.Errorf("invalid scaling_cur_freq value %q", content)
	}
	return value, nil
}

func (reader *frequencyReader) ReadKHz() (int64, error) {
	if err := reader.resetForRead(); err != nil {
		return 0, err
	}
	if reader.file == nil {
		return 0, fmt.Errorf("frequency file is not open")
	}
	n, err := reader.file.Read(reader.buffer[:])
	if err != nil && err != io.EOF {
		return 0, err
	}
	if n == len(reader.buffer) {
		return 0, fmt.Errorf("scaling_cur_freq value exceeds %d bytes", len(reader.buffer)-1)
	}
	return parsePositiveDecimal(reader.buffer[:n])
}

func appendCSVField(destination []byte, value string) []byte {
	if !strings.ContainsAny(value, ",\"\r\n") {
		return append(destination, value...)
	}
	destination = append(destination, '"')
	for index := 0; index < len(value); index++ {
		if value[index] == '"' {
			destination = append(destination, '"')
		}
		destination = append(destination, value[index])
	}
	return append(destination, '"')
}

func encodeSampleRow(buffer []byte, monotonicNS, frequencyKHz int64, success bool, errorText string) []byte {
	buffer = buffer[:0]
	buffer = strconv.AppendInt(buffer, monotonicNS, 10)
	buffer = append(buffer, ',')
	buffer = strconv.AppendInt(buffer, frequencyKHz, 10)
	if success {
		buffer = append(buffer, ",true,"...)
	} else {
		buffer = append(buffer, ",false,"...)
		buffer = appendCSVField(buffer, errorText)
	}
	return append(buffer, '\n')
}

func encodeScientificSampleRow(
	buffer []byte, scheduledNS, actualNS, frequencyKHz int64, success, missed bool, errorText, phase string,
) []byte {
	buffer = buffer[:0]
	buffer = strconv.AppendInt(buffer, scheduledNS, 10)
	buffer = append(buffer, ',')
	buffer = strconv.AppendInt(buffer, actualNS, 10)
	buffer = append(buffer, ',')
	buffer = strconv.AppendInt(buffer, frequencyKHz, 10)
	buffer = append(buffer, ',')
	buffer = strconv.AppendBool(buffer, success)
	buffer = append(buffer, ',')
	buffer = strconv.AppendBool(buffer, missed)
	buffer = append(buffer, ',')
	buffer = appendCSVField(buffer, errorText)
	buffer = append(buffer, ',')
	buffer = appendCSVField(buffer, phase)
	return append(buffer, '\n')
}

func acceptStabilizationStart(listener net.Listener, expectedToken string, starts chan<- stabilizationStart, errors chan<- error) {
	connection, err := listener.Accept()
	if err != nil {
		errors <- err
		return
	}
	var message stabilizationMessage
	if err := json.NewDecoder(bufio.NewReader(connection)).Decode(&message); err != nil {
		connection.Close()
		errors <- err
		return
	}
	if message.Schema != stabilizationSchema || message.Type != "start" ||
		message.Token != expectedToken || message.StartMonotonicNS <= 0 {
		connection.Close()
		errors <- fmt.Errorf("invalid or stale stabilization start message")
		return
	}
	starts <- stabilizationStart{message: message, conn: connection}
}

func stopRequested(path string) (bool, error) {
	_, err := os.Stat(path)
	if err == nil {
		return true, nil
	}
	if os.IsNotExist(err) {
		return false, nil
	}
	return false, err
}

func nextAbsoluteDeadline(previousDeadline, workCompleted, intervalNS int64) int64 {
	next := previousDeadline + intervalNS
	if workCompleted >= next {
		next += ((workCompleted-next)/intervalNS + 1) * intervalNS
	}
	return next
}

func run() error {
	cpu := flag.Int("cpu", 7, "CPU whose scaling_cur_freq is sampled")
	intervalMS := flag.Int("interval-ms", 1, "fixed sampling interval in milliseconds")
	output := flag.String("output", "", "new CSV output path")
	readyFile := flag.String("ready-file", "", "new readiness marker")
	stopFile := flag.String("stop-file", "", "stop when this path exists")
	stabilizationListen := flag.String("stabilization-listen", "", "loopback TCP listen address for scientific stabilization")
	stabilizationToken := flag.String("stabilization-token", "", "unique per-attempt stabilization token")
	stabilizationTimeout := flag.Duration("stabilization-timeout", 30*time.Second, "finite stabilization safety timeout")
	flag.Parse()
	if *cpu < 0 || *intervalMS <= 0 || *output == "" || *readyFile == "" || *stopFile == "" {
		return fmt.Errorf("--cpu, --interval-ms, --output, --ready-file, and --stop-file are required")
	}
	scientificProtocol := *stabilizationListen != "" || *stabilizationToken != ""
	if scientificProtocol && (*stabilizationListen == "" || *stabilizationToken == "" ||
		*intervalMS != scientificIntervalMS || *stabilizationTimeout < 3*time.Second) {
		return fmt.Errorf("scientific stabilization requires listen address, token, 100 ms interval, and timeout >= 3s")
	}
	allowed, err := currentAllowedCPUs("/proc/self/status")
	if err != nil {
		return fmt.Errorf("read sampler affinity: %w", err)
	}
	if allowed != "0" {
		return fmt.Errorf("frequency sampler requires exact affinity 0, got %s", allowed)
	}
	for _, path := range []string{*output, *readyFile} {
		if _, err := os.Lstat(path); err == nil {
			return fmt.Errorf("refusing existing path %s", path)
		} else if !os.IsNotExist(err) {
			return err
		}
	}

	file, err := createExclusive(*output)
	if err != nil {
		return err
	}
	defer file.Close()
	buffered := bufio.NewWriterSize(file, outputBufferBytes)
	header := "monotonic_ns,freq_khz,success,error\n"
	if scientificProtocol {
		header = "scheduled_monotonic_ns,actual_monotonic_ns,freq_khz,success,missed,error,phase\n"
	}
	if _, err := buffered.WriteString(header); err != nil {
		return err
	}
	if err := buffered.Flush(); err != nil {
		return err
	}

	frequencyPath := filepath.Join("/sys/devices/system/cpu", fmt.Sprintf("cpu%d", *cpu), "cpufreq", "scaling_cur_freq")
	frequencyReader, err := newFrequencyReader(frequencyPath)
	if err != nil {
		return fmt.Errorf("open scaling_cur_freq: %w", err)
	}
	defer frequencyReader.Close()
	if _, err := frequencyReader.ReadKHz(); err != nil {
		return fmt.Errorf("probe scaling_cur_freq: %w", err)
	}

	var listener net.Listener
	starts := make(chan stabilizationStart, 1)
	coordinationErrors := make(chan error, 1)
	if scientificProtocol {
		listener, err = net.Listen("tcp", *stabilizationListen)
		if err != nil {
			return fmt.Errorf("listen for stabilization coordination: %w", err)
		}
		defer listener.Close()
		go acceptStabilizationStart(listener, *stabilizationToken, starts, coordinationErrors)
	}

	ready, err := createExclusive(*readyFile)
	if err != nil {
		return err
	}
	if err := ready.Close(); err != nil {
		return err
	}

	intervalNS := int64(*intervalMS) * 1_000_000
	deadline, err := monotonicNanoseconds()
	if err != nil {
		return err
	}
	nextStopPoll := deadline
	sampleCount := 0
	rowBuffer := make([]byte, 0, 256)
	var tracker *stabilizationTracker
	for {
		select {
		case start := <-starts:
			if tracker != nil {
				start.conn.Close()
				return fmt.Errorf("duplicate stabilization start")
			}
			tracker = &stabilizationTracker{start: start.message, conn: start.conn, timeoutNS: int64(*stabilizationTimeout)}
			defer tracker.conn.Close()
		case coordinationErr := <-coordinationErrors:
			return fmt.Errorf("stabilization coordination failed: %w", coordinationErr)
		default:
		}
		ns, clockErr := monotonicNanoseconds()
		if clockErr == nil && ns >= nextStopPoll {
			stop, err := stopRequested(*stopFile)
			if err != nil {
				return fmt.Errorf("check stop file: %w", err)
			}
			if stop {
				break
			}
			nextStopPoll = ns + stopPollIntervalNS
		}

		frequency, frequencyErr := frequencyReader.ReadKHz()
		success := clockErr == nil && frequencyErr == nil
		errorText := ""
		if clockErr != nil {
			errorText = clockErr.Error()
		} else if frequencyErr != nil {
			errorText = frequencyErr.Error()
		}
		phase := "legacy"
		if scientificProtocol {
			phase = "pre_stabilization"
			if tracker != nil {
				phase = "stabilization"
				if tracker.finished {
					phase = "post_stabilization"
				}
			}
			rowBuffer = encodeScientificSampleRow(rowBuffer, deadline, ns, frequency, success, false, errorText, phase)
		} else {
			rowBuffer = encodeSampleRow(rowBuffer, ns, frequency, success, errorText)
		}
		if _, err := buffered.Write(rowBuffer); err != nil {
			return err
		}
		sampleCount++
		if scientificProtocol && tracker != nil && !tracker.finished {
			if !success {
				tracker.resetSequence()
			} else {
				response, err := tracker.observe(stabilizationObservation{
					ScheduledMonotonicNS: deadline, ActualMonotonicNS: ns, FrequencyKHz: frequency,
				})
				if err != nil {
					return err
				}
				if response != nil {
					if err := json.NewEncoder(tracker.conn).Encode(response); err != nil {
						return fmt.Errorf("send stabilization result: %w", err)
					}
				}
			}
		}
		if sampleCount%flushEverySamples == 0 {
			if err := buffered.Flush(); err != nil {
				return err
			}
		}

		workCompleted, err := monotonicNanoseconds()
		if err != nil {
			return err
		}
		nextDeadline := deadline + intervalNS
		if scientificProtocol {
			for nextDeadline <= workCompleted {
				missedPhase := "pre_stabilization"
				if tracker != nil {
					missedPhase = "stabilization"
					if tracker.finished {
						missedPhase = "post_stabilization"
					} else {
						tracker.resetSequence()
					}
				}
				rowBuffer = encodeScientificSampleRow(rowBuffer, nextDeadline, 0, 0, false, true, "scheduled observation missed", missedPhase)
				if _, err := buffered.Write(rowBuffer); err != nil {
					return err
				}
				sampleCount++
				nextDeadline += intervalNS
			}
		}
		deadline = nextDeadline
		if !scientificProtocol {
			deadline = nextAbsoluteDeadline(deadline-intervalNS, workCompleted, intervalNS)
		}
		if err := sleepUntilMonotonic(deadline); err != nil {
			return err
		}
	}
	if err := buffered.Flush(); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
