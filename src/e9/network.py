"""Pure, non-privileged E9 measurement and validation infrastructure.

E9 times a network-only HTLC_ADD/HTLC_SETTLE round trip using injected,
already-prepared messages. This module does not start Mininet, apply host
networking changes, or perform cryptography/serialization.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol, Sequence, TextIO


RTT_VALUES_MS = (5, 20, 50, 100)
HOP_VALUES = (1, 2, 3, 4, 5)
CONFIG_VALUES = ("classical", "uniform_mldsa", "layer_aware")
E9_PER_CHANNEL_RTT_COMPENSATION_MS = 0.75
MINIMUM_SCIENTIFIC_ITERATIONS = 1000
DEFAULT_SCIENTIFIC_WARMUP_ITERATIONS = 100
MESSAGE_BYTES_BY_CONFIG = {
    "classical": 222,
    "uniform_mldsa": 10648,
    "layer_aware": 3252,
}
FINAL_FIELDS = (
    "config",
    "rtt_ms",
    "hops",
    "completion_median_ms",
    "completion_p95_ms",
    "n_iter",
)
RAW_FIELDS = (
    "config",
    "rtt_ms",
    "hops",
    "iteration",
    "phase",
    "scientific",
    "completion_ms",
    "success",
    "error",
)
PING_FIELDS = (
    "config",
    "rtt_ms",
    "hops",
    "expected_end_to_end_rtt_ms",
    "ping_sample_count",
    "ping_samples_ms",
    "measured_end_to_end_rtt_ms",
    "deviation_pct",
    "raw_output_sha256",
    "raw_output_bytes",
    "pass",
)

PHASE_WARMUP = "warmup"
PHASE_MEASURED = "measured"


@dataclass(frozen=True, order=True)
class NetworkCondition:
    """Per-payment-channel RTT and number of payment-channel links."""

    configured_rtt_ms: int
    hops: int

    def validate(self) -> None:
        if self.configured_rtt_ms not in RTT_VALUES_MS:
            raise ValueError(f"unsupported configured RTT: {self.configured_rtt_ms}")
        if self.hops not in HOP_VALUES:
            raise ValueError(f"unsupported hop count: {self.hops}")


def sweep_conditions() -> list[NetworkCondition]:
    """Return RTT-major, then ascending-hop, Cartesian sweep order."""
    return [NetworkCondition(rtt, hops) for rtt in RTT_VALUES_MS for hops in HOP_VALUES]


def payment_channel_link_count(hops: int) -> int:
    """Return the authoritative number of payment-channel links."""
    if hops not in HOP_VALUES:
        raise ValueError(f"unsupported hop count: {hops}")
    return hops


def intermediate_node_count(hops: int) -> int:
    if hops not in HOP_VALUES:
        raise ValueError(f"unsupported hop count: {hops}")
    return hops - 1


def effective_one_way_tc_delay_ms(configured_per_channel_rtt_ms: float) -> float:
    """Return calibrated TCLink delay, excluding host network-stack overhead.

    The compensation is specific to this E9 host/testbed and is not part of
    the requested logical per-payment-channel RTT.
    """
    if not math.isfinite(configured_per_channel_rtt_ms):
        raise ValueError("configured per-channel RTT must be finite")
    delay = (
        configured_per_channel_rtt_ms - E9_PER_CHANNEL_RTT_COMPENSATION_MS
    ) / 2.0
    if delay <= 0:
        raise ValueError("E9 RTT compensation makes TCLink delay non-positive")
    return delay


def per_channel_one_way_delay_ms(condition: NetworkCondition) -> float:
    """Return the calibrated one-way delay injected on each payment channel."""
    condition.validate()
    return effective_one_way_tc_delay_ms(condition.configured_rtt_ms)


def expected_end_to_end_rtt_ms(condition: NetworkCondition) -> float:
    """Return whole-path RTT: per-channel RTT multiplied by channel count."""
    condition.validate()
    return float(condition.configured_rtt_ms * condition.hops)


def validate_config(config: str, *, scientific: bool) -> None:
    if not config:
        raise ValueError("config must be non-empty")
    if scientific and config not in CONFIG_VALUES:
        raise ValueError(f"unsupported scientific E9 config: {config}")


def payment_channel_node_names(hops: int) -> tuple[str, ...]:
    """Return sender, zero or more intermediates, and receiver."""
    payment_channel_link_count(hops)
    return ("a", *(f"i{index}" for index in range(1, hops)), "b")


def link_endpoint_addresses(link_index: int, hops: int) -> tuple[str, str]:
    """Return the left/right directly connected addresses for one channel."""
    payment_channel_link_count(hops)
    if not 1 <= link_index <= hops:
        raise ValueError(f"invalid payment-channel link index: {link_index}")
    return (f"10.0.{link_index}.1", f"10.0.{link_index}.2")


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("no timing samples")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be in [0,1]")
    copied = list(values)
    if any(not math.isfinite(value) for value in copied):
        raise ValueError("timing samples must be finite")
    copied.sort()
    rank = probability * (len(copied) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return copied[lower]
    weight = rank - lower
    return copied[lower] + weight * (copied[upper] - copied[lower])


@dataclass(frozen=True)
class CompletionStatistics:
    median_ms: float
    p95_ms: float
    p99_ms: float


def completion_statistics(values: Sequence[float]) -> CompletionStatistics:
    return CompletionStatistics(
        median_ms=percentile(values, 0.50),
        p95_ms=percentile(values, 0.95),
        p99_ms=percentile(values, 0.99),
    )


_PING_TIME_RE = re.compile(r"\btime=([0-9]+(?:\.[0-9]+)?)\s*ms\b")


@dataclass(frozen=True)
class PingVerificationRecord:
    config: str
    condition: NetworkCondition
    samples_ms: tuple[float, ...]
    expected_end_to_end_rtt_ms: float
    measured_end_to_end_rtt_ms: float
    deviation_pct: float
    passed: bool
    raw_output: str
    raw_output_sha256: str
    raw_output_bytes: int


def parse_ping_samples(output: str) -> tuple[float, ...]:
    samples = tuple(float(match) for match in _PING_TIME_RE.findall(output))
    if not samples:
        raise ValueError("ping output contains no exact time=... ms samples")
    if any(not math.isfinite(sample) or sample < 0 for sample in samples):
        raise ValueError("ping RTT samples must be finite and non-negative")
    return samples


def verify_ping(
    config: str,
    condition: NetworkCondition,
    output: str,
) -> PingVerificationRecord:
    validate_config(config, scientific=False)
    condition.validate()
    if condition.configured_rtt_ms <= 0:
        raise ValueError("configured RTT must be positive")
    samples = parse_ping_samples(output)
    measured = percentile(samples, 0.50)
    expected = expected_end_to_end_rtt_ms(condition)
    deviation = abs(measured - expected) / expected * 100.0
    raw_bytes = output.encode("utf-8")
    return PingVerificationRecord(
        config=config,
        condition=condition,
        samples_ms=samples,
        expected_end_to_end_rtt_ms=expected,
        measured_end_to_end_rtt_ms=measured,
        deviation_pct=deviation,
        passed=deviation < 10.0,
        raw_output=output,
        raw_output_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_output_bytes=len(raw_bytes),
    )


@dataclass(frozen=True)
class PreparedHTLCMessages:
    """Already-prepared network messages; E9 performs no crypto or serialization."""

    add: bytes
    settle: bytes

    def validate(self) -> None:
        if not self.add or not self.settle:
            raise ValueError("prepared HTLC_ADD and HTLC_SETTLE bytes are required")


def _prepared_payload(config: str, message_type: str) -> bytes:
    """Build inert E9 bytes with the authoritative E2 serialized length.

    These bytes model already-prepared E2 transactions for network-size
    purposes. They are deterministic integrity-test payloads, not independently
    valid cryptographic transactions.
    """
    validate_config(config, scientific=True)
    if message_type not in ("HTLC_ADD", "HTLC_SETTLE"):
        raise ValueError(f"unsupported prepared message type: {message_type}")
    size = MESSAGE_BYTES_BY_CONFIG[config]
    seed = hashlib.sha256(f"e9:{config}:{message_type}:v1".encode("ascii")).digest()
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def prepared_htlc_messages(config: str) -> PreparedHTLCMessages:
    return PreparedHTLCMessages(
        add=_prepared_payload(config, "HTLC_ADD"),
        settle=_prepared_payload(config, "HTLC_SETTLE"),
    )


def validate_authoritative_messages(config: str, messages: PreparedHTLCMessages) -> None:
    expected = prepared_htlc_messages(config)
    if messages != expected:
        raise ValueError(
            "scientific E9 messages must be the authoritative deterministic "
            "prepared HTLC_ADD/HTLC_SETTLE buffers"
        )


class HTLCCompletionExecutor(Protocol):
    scientific: bool

    def execute(
        self,
        config: str,
        condition: NetworkCondition,
        messages: PreparedHTLCMessages,
    ) -> float:
        """Time sender HTLC_ADD send through settle receipt at the sender.

        Implementations may only move already-prepared bytes forward across all
        payment-channel links and move the settle confirmation back. Crypto and
        serialization computation are outside this timed E9 operation.
        """


@dataclass(frozen=True)
class RunOptions:
    warmup_iterations: int
    measured_iterations: int
    scientific: bool


def validate_run(
    config: str,
    condition: NetworkCondition,
    executor: HTLCCompletionExecutor | None,
    messages: PreparedHTLCMessages | None,
    options: RunOptions,
    verification: PingVerificationRecord | None,
) -> None:
    validate_config(config, scientific=options.scientific)
    condition.validate()
    if executor is None:
        raise ValueError("completion executor is required")
    if messages is None:
        raise ValueError("prepared HTLC messages are required")
    messages.validate()
    if options.warmup_iterations < 0:
        raise ValueError("warmup iterations cannot be negative")
    if options.measured_iterations <= 0:
        raise ValueError("measured iterations must be positive")
    if options.scientific:
        if options.measured_iterations < MINIMUM_SCIENTIFIC_ITERATIONS:
            raise ValueError("scientific runs require at least 1000 measured iterations")
        if not executor.scientific:
            raise ValueError("non-scientific completion executor cannot run scientifically")
        validate_authoritative_messages(config, messages)
        validate_ping_gate(config, condition, verification)


def validate_ping_gate(
    config: str,
    condition: NetworkCondition,
    verification: PingVerificationRecord | None,
) -> None:
    if verification is None:
        raise ValueError("scientific run requires ping verification")
    if verification.config != config or verification.condition != condition:
        raise ValueError("ping verification does not match run config/condition")
    raw_bytes = verification.raw_output.encode("utf-8")
    if verification.raw_output_sha256 != hashlib.sha256(raw_bytes).hexdigest():
        raise ValueError("raw ping output SHA-256 provenance mismatch")
    if verification.raw_output_bytes != len(raw_bytes):
        raise ValueError("raw ping output byte-length provenance mismatch")
    parsed_samples = parse_ping_samples(verification.raw_output)
    if parsed_samples != verification.samples_ms:
        raise ValueError("ping samples do not match retained raw ping output")
    expected = expected_end_to_end_rtt_ms(condition)
    measured = percentile(verification.samples_ms, 0.50)
    deviation = abs(measured - expected) / expected * 100.0
    passed = deviation < 10.0
    if not math.isclose(expected, verification.expected_end_to_end_rtt_ms, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("ping expected whole-path RTT provenance mismatch")
    if not math.isclose(measured, verification.measured_end_to_end_rtt_ms, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("ping measured RTT provenance mismatch")
    if not math.isclose(deviation, verification.deviation_pct, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("ping deviation provenance mismatch")
    if verification.passed != passed:
        raise ValueError("ping pass/fail provenance mismatch")
    if not passed:
        raise ValueError("ping verification failed strict <10% deviation gate")


@dataclass(frozen=True)
class RawCompletionRecord:
    config: str
    configured_rtt_ms: int
    hops: int
    iteration: int
    phase: str
    scientific: bool
    completion_ms: float
    success: bool
    error: str


def run_condition(
    config: str,
    condition: NetworkCondition,
    executor: HTLCCompletionExecutor,
    messages: PreparedHTLCMessages,
    options: RunOptions,
    verification: PingVerificationRecord | None = None,
) -> list[RawCompletionRecord]:
    validate_run(config, condition, executor, messages, options, verification)
    records: list[RawCompletionRecord] = []
    for phase, iterations in (
        (PHASE_WARMUP, options.warmup_iterations),
        (PHASE_MEASURED, options.measured_iterations),
    ):
        for iteration in range(iterations):
            try:
                completion_ms = executor.execute(config, condition, messages)
                if not math.isfinite(completion_ms) or completion_ms < 0:
                    raise ValueError("executor returned invalid completion time")
                success = True
                error = ""
            except Exception as exc:  # Preserve executor failure as raw evidence.
                completion_ms = 0.0
                success = False
                error = str(exc)
            records.append(
                RawCompletionRecord(
                    config=config,
                    configured_rtt_ms=condition.configured_rtt_ms,
                    hops=condition.hops,
                    iteration=iteration,
                    phase=phase,
                    scientific=options.scientific,
                    completion_ms=completion_ms,
                    success=success,
                    error=error,
                )
            )
    return records


@dataclass(frozen=True)
class SummaryRow:
    config: str
    rtt_ms: int
    hops: int
    completion_median_ms: float
    completion_p95_ms: float
    n_iter: int
    completion_p99_ms: float
    scientific: bool
    ping_verification: PingVerificationRecord | None


def summarize_condition(
    records: Sequence[RawCompletionRecord],
    expected_measured_iterations: int,
    verification: PingVerificationRecord | None,
) -> SummaryRow:
    if not records:
        raise ValueError("no completion records")
    first = records[0]
    key = (first.config, first.configured_rtt_ms, first.hops)
    scientific = first.scientific
    for record in records[1:]:
        if (record.config, record.configured_rtt_ms, record.hops) != key:
            raise ValueError("records contain multiple config/network conditions")
        if record.scientific != scientific:
            raise ValueError("mixed scientific and non-scientific records")
    condition = NetworkCondition(first.configured_rtt_ms, first.hops)
    condition.validate()
    validate_config(first.config, scientific=scientific)

    measured = [record for record in records if record.phase == PHASE_MEASURED]
    if scientific:
        if expected_measured_iterations < MINIMUM_SCIENTIFIC_ITERATIONS:
            raise ValueError("scientific summary requires at least 1000 expected iterations")
        validate_ping_gate(first.config, condition, verification)
        by_iteration: dict[int, RawCompletionRecord] = {}
        for record in measured:
            if record.iteration < 0 or record.iteration >= expected_measured_iterations:
                raise ValueError("scientific iteration index out of range")
            if record.iteration in by_iteration:
                raise ValueError("duplicate scientific iteration")
            by_iteration[record.iteration] = record
            if not record.success:
                raise ValueError("failed scientific completion sample")
        if len(by_iteration) != expected_measured_iterations:
            raise ValueError("missing scientific completion iteration")

    successful = [record.completion_ms for record in measured if record.success]
    statistics = completion_statistics(successful)
    return SummaryRow(
        config=first.config,
        rtt_ms=first.configured_rtt_ms,
        hops=first.hops,
        completion_median_ms=statistics.median_ms,
        completion_p95_ms=statistics.p95_ms,
        n_iter=len(measured),
        completion_p99_ms=statistics.p99_ms,
        scientific=scientific,
        ping_verification=verification,
    )


def validate_final_rows(rows: Sequence[SummaryRow]) -> None:
    expected_row_count = len(CONFIG_VALUES) * len(RTT_VALUES_MS) * len(HOP_VALUES)
    if len(rows) != expected_row_count:
        raise ValueError(f"final E9 dataset requires exactly {expected_row_count} rows")
    seen: set[tuple[str, int, int]] = set()
    configs: set[str] = set()
    for row in rows:
        validate_config(row.config, scientific=True)
        condition = NetworkCondition(row.rtt_ms, row.hops)
        condition.validate()
        if not row.scientific:
            raise ValueError("final E9 dataset contains non-scientific row")
        if row.n_iter < MINIMUM_SCIENTIFIC_ITERATIONS:
            raise ValueError("final E9 row has fewer than 1000 iterations")
        validate_ping_gate(row.config, condition, row.ping_verification)
        key = (row.config, row.rtt_ms, row.hops)
        if key in seen:
            raise ValueError("duplicate final E9 config/RTT/hops row")
        seen.add(key)
        configs.add(row.config)
    if configs != set(CONFIG_VALUES):
        raise ValueError("final E9 dataset does not contain exactly the three authoritative configs")
    expected_conditions = set(sweep_conditions())
    for config in CONFIG_VALUES:
        actual = {NetworkCondition(rtt, hops) for cfg, rtt, hops in seen if cfg == config}
        if actual != expected_conditions:
            raise ValueError(f"incomplete 20-condition sweep for config {config!r}")


def write_final_csv(output: TextIO, rows: Sequence[SummaryRow]) -> None:
    validate_final_rows(rows)
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FINAL_FIELDS)
    by_key = {(row.config, row.rtt_ms, row.hops): row for row in rows}
    for config in CONFIG_VALUES:
        for condition in sweep_conditions():
            row = by_key[(config, condition.configured_rtt_ms, condition.hops)]
            writer.writerow(
                (
                    row.config,
                    row.rtt_ms,
                    row.hops,
                    _format_float(row.completion_median_ms),
                    _format_float(row.completion_p95_ms),
                    row.n_iter,
                )
            )


def write_raw_completion_csv(output: TextIO, records: Sequence[RawCompletionRecord]) -> None:
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(RAW_FIELDS)
    for record in records:
        writer.writerow(
            (
                record.config,
                record.configured_rtt_ms,
                record.hops,
                record.iteration,
                record.phase,
                str(record.scientific).lower(),
                _format_float(record.completion_ms),
                str(record.success).lower(),
                record.error,
            )
        )


def write_ping_verification_csv(
    output: TextIO,
    records: Sequence[PingVerificationRecord],
) -> None:
    # A future scientific runner must retain each raw ping output separately
    # under raw/e9; these fingerprints must identify that retained file.
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(PING_FIELDS)
    for record in records:
        writer.writerow(
            (
                record.config,
                record.condition.configured_rtt_ms,
                record.condition.hops,
                _format_float(record.expected_end_to_end_rtt_ms),
                len(record.samples_ms),
                ";".join(_format_float(sample) for sample in record.samples_ms),
                _format_float(record.measured_end_to_end_rtt_ms),
                _format_float(record.deviation_pct),
                record.raw_output_sha256,
                record.raw_output_bytes,
                str(record.passed).lower(),
            )
        )


def _format_float(value: float) -> str:
    return f"{value:.6f}"
