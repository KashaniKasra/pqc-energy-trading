import csv
from dataclasses import replace
import hashlib
import importlib.util
import io
import math
from pathlib import Path
import unittest

from src.e9.network import (
    CONFIG_VALUES,
    E9_PER_CHANNEL_RTT_COMPENSATION_MS,
    FINAL_FIELDS,
    HOP_VALUES,
    MESSAGE_BYTES_BY_CONFIG,
    MINIMUM_SCIENTIFIC_ITERATIONS,
    PING_FIELDS,
    RAW_FIELDS,
    RTT_VALUES_MS,
    CompletionStatistics,
    NetworkCondition,
    PingVerificationRecord,
    PreparedHTLCMessages,
    RawCompletionRecord,
    RunOptions,
    SummaryRow,
    completion_statistics,
    effective_one_way_tc_delay_ms,
    expected_end_to_end_rtt_ms,
    intermediate_node_count,
    payment_channel_link_count,
    payment_channel_node_names,
    per_channel_one_way_delay_ms,
    prepared_htlc_messages,
    run_condition,
    summarize_condition,
    sweep_conditions,
    validate_final_rows,
    validate_ping_gate,
    validate_run,
    verify_ping,
    write_final_csv,
    write_ping_verification_csv,
    write_raw_completion_csv,
)


class FakeExecutor:
    def __init__(self, values=(1.0,), *, scientific=False, fail_calls=()):
        self.scientific = scientific
        self._values = tuple(values)
        self._fail_calls = set(fail_calls)
        self._calls = 0

    def execute(self, config, condition, messages):
        if not messages.add or not messages.settle:
            raise RuntimeError("missing prepared messages")
        call = self._calls
        self._calls += 1
        if call in self._fail_calls:
            raise RuntimeError("deterministic completion failure")
        return self._values[call % len(self._values)]


def ping_output(*samples):
    return "\n".join(
        f"64 bytes from 10.0.0.2: icmp_seq={index} ttl=64 time={sample} ms"
        for index, sample in enumerate(samples, start=1)
    )


def passing_ping(config, condition):
    return verify_ping(config, condition, ping_output(expected_end_to_end_rtt_ms(condition)))


PREPARED_MESSAGES = PreparedHTLCMessages(add=b"prepared-add", settle=b"prepared-settle")


def scientific_records(count=MINIMUM_SCIENTIFIC_ITERATIONS):
    return [
        RawCompletionRecord(
            config="classical",
            configured_rtt_ms=5,
            hops=1,
            iteration=iteration,
            phase="measured",
            scientific=True,
            completion_ms=5.0,
            success=True,
            error="",
        )
        for iteration in range(count)
    ]


def complete_rows(configs=CONFIG_VALUES):
    rows = []
    for config in configs:
        for condition in sweep_conditions():
            verification = passing_ping(config, condition)
            rows.append(
                SummaryRow(
                    config=config,
                    rtt_ms=condition.configured_rtt_ms,
                    hops=condition.hops,
                    completion_median_ms=float(condition.configured_rtt_ms),
                    completion_p95_ms=float(condition.configured_rtt_ms + 1),
                    n_iter=MINIMUM_SCIENTIFIC_ITERATIONS,
                    completion_p99_ms=float(condition.configured_rtt_ms + 2),
                    scientific=True,
                    ping_verification=verification,
                )
            )
    return rows


class SweepAndTopologyTests(unittest.TestCase):
    def test_required_sweep_and_order(self):
        conditions = sweep_conditions()
        self.assertEqual(RTT_VALUES_MS, (5, 20, 50, 100))
        self.assertEqual(HOP_VALUES, (1, 2, 3, 4, 5))
        self.assertEqual(len(conditions), 20)
        self.assertEqual(
            conditions,
            [NetworkCondition(rtt, hops) for rtt in RTT_VALUES_MS for hops in HOP_VALUES],
        )

    def test_authoritative_hops_map_to_channels_and_intermediates(self):
        expected = {1: (1, 0), 3: (3, 2), 5: (5, 4)}
        for hops, (links, intermediates) in expected.items():
            self.assertEqual(payment_channel_link_count(hops), links)
            self.assertEqual(intermediate_node_count(hops), intermediates)
        for invalid in (0, 6, -1):
            with self.assertRaises(ValueError):
                payment_channel_link_count(invalid)
            with self.assertRaises(ValueError):
                intermediate_node_count(invalid)

    def test_node_names_match_payment_channel_path(self):
        self.assertEqual(payment_channel_node_names(1), ("a", "b"))
        self.assertEqual(payment_channel_node_names(3), ("a", "i1", "i2", "b"))
        self.assertEqual(
            payment_channel_node_names(5),
            ("a", "i1", "i2", "i3", "i4", "b"),
        )

    def test_calibrated_delay_preserves_logical_end_to_end_rtt(self):
        expected_delays = {5: 2.25, 20: 9.75, 50: 24.75, 100: 49.75}
        self.assertEqual(E9_PER_CHANNEL_RTT_COMPENSATION_MS, 0.5)
        for condition in sweep_conditions():
            delay = per_channel_one_way_delay_ms(condition)
            self.assertEqual(delay, expected_delays[condition.configured_rtt_ms])
            self.assertEqual(
                expected_end_to_end_rtt_ms(condition),
                condition.configured_rtt_ms * condition.hops,
            )
        examples = (
            (NetworkCondition(20, 3), 9.75, 60),
            (NetworkCondition(5, 5), 2.25, 25),
            (NetworkCondition(100, 1), 49.75, 100),
        )
        for condition, delay, expected in examples:
            self.assertEqual(per_channel_one_way_delay_ms(condition), delay)
            self.assertEqual(expected_end_to_end_rtt_ms(condition), expected)

    def test_calibration_rejects_non_positive_effective_delay(self):
        for configured_rtt in (0.5, 0.0, -1.0):
            with self.assertRaisesRegex(ValueError, "non-positive"):
                effective_one_way_tc_delay_ms(configured_rtt)

    def test_mininet_topology_object_has_expected_path(self):
        topology_path = Path(__file__).parents[2] / "env/mininet/e9/topology.py"
        specification = importlib.util.spec_from_file_location("e9_topology_test", topology_path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        expected_shapes = {
            1: (["a", "b"], 1),
            3: (["a", "i1", "i2", "b"], 3),
            5: (["a", "i1", "i2", "i3", "i4", "b"], 5),
        }
        for hops, (hosts, link_count) in expected_shapes.items():
            topology = module.E9LinearTopology(hops=hops, target_rtt_ms=20)
            self.assertCountEqual(topology.hosts(), hosts)
            self.assertEqual(topology.switches(), [])
            links = topology.links(withInfo=True)
            self.assertEqual(len(links), link_count)
            self.assertTrue(all(info["delay"] == "9.750000000ms" for _, _, info in links))
            self.assertTrue(all(info["cls"].__name__ == "TCLink" for _, _, info in links))


class PingVerificationTests(unittest.TestCase):
    def test_strict_deviation_boundary(self):
        condition = NetworkCondition(20, 3)
        exact = verify_ping("classical", condition, ping_output(60))
        below = verify_ping("classical", condition, ping_output(65.99))
        boundary = verify_ping("classical", condition, ping_output(66))
        above = verify_ping("classical", condition, ping_output(67))
        self.assertTrue(exact.passed)
        self.assertTrue(below.passed)
        self.assertLess(below.deviation_pct, 10)
        self.assertFalse(boundary.passed)
        self.assertFalse(above.passed)

    def test_invalid_configured_rtt_and_samples(self):
        for rtt in (0, -5):
            with self.assertRaises(ValueError):
                verify_ping("classical", NetworkCondition(rtt, 1), ping_output(1))
        for output in ("time=nan ms", "time=inf ms", "no ping samples"):
            with self.assertRaises(ValueError):
                verify_ping("classical", NetworkCondition(5, 1), output)

    def test_median_verification_retains_samples(self):
        raw_output = ping_output(39, 40, 41, 42)
        record = verify_ping("classical", NetworkCondition(20, 2), raw_output)
        self.assertEqual(record.samples_ms, (39.0, 40.0, 41.0, 42.0))
        self.assertEqual(record.expected_end_to_end_rtt_ms, 40)
        self.assertEqual(record.measured_end_to_end_rtt_ms, 40.5)
        self.assertTrue(record.passed)
        self.assertEqual(record.raw_output, raw_output)
        self.assertEqual(record.raw_output_sha256, hashlib.sha256(raw_output.encode("utf-8")).hexdigest())
        self.assertEqual(record.raw_output_bytes, len(raw_output.encode("utf-8")))

    def test_altered_raw_output_rejected_with_old_provenance(self):
        condition = NetworkCondition(20, 2)
        record = verify_ping("classical", condition, ping_output(39, 40, 41))
        altered = replace(record, raw_output=record.raw_output + "\n")
        with self.assertRaisesRegex(ValueError, "SHA-256 provenance mismatch"):
            validate_ping_gate("classical", condition, altered)


class RunAndSummaryTests(unittest.TestCase):
    def test_scientific_guards_at_validation_level(self):
        condition = NetworkCondition(5, 1)
        verification = passing_ping("classical", condition)
        scientific_executor = FakeExecutor(scientific=True)
        with self.assertRaises(ValueError):
            validate_run(
                "classical",
                condition,
                scientific_executor,
                prepared_htlc_messages("classical"),
                RunOptions(0, 999, True),
                verification,
            )
        validate_run(
            "classical",
            condition,
            scientific_executor,
            prepared_htlc_messages("classical"),
            RunOptions(1, 1000, True),
            verification,
        )
        with self.assertRaisesRegex(ValueError, "authoritative deterministic"):
            validate_run(
                "classical",
                condition,
                scientific_executor,
                PREPARED_MESSAGES,
                RunOptions(0, 1000, True),
                verification,
            )
        failed = verify_ping("classical", condition, ping_output(5.5))
        self.assertFalse(failed.passed)
        with self.assertRaises(ValueError):
            validate_run(
                "classical",
                condition,
                scientific_executor,
                prepared_htlc_messages("classical"),
                RunOptions(0, 1000, True),
                failed,
            )
        with self.assertRaises(ValueError):
            validate_run(
                "classical",
                condition,
                FakeExecutor(scientific=False),
                prepared_htlc_messages("classical"),
                RunOptions(0, 1000, True),
                verification,
            )
        with self.assertRaises(ValueError):
            validate_run(
                "unknown",
                condition,
                scientific_executor,
                prepared_htlc_messages("classical"),
                RunOptions(0, 1000, True),
                verification,
            )
        for config in CONFIG_VALUES:
            validate_run(
                config,
                condition,
                scientific_executor,
                prepared_htlc_messages(config),
                RunOptions(0, 1000, True),
                passing_ping(config, condition),
            )

    def test_authoritative_prepared_message_sizes_and_content(self):
        self.assertEqual(
            MESSAGE_BYTES_BY_CONFIG,
            {"classical": 222, "uniform_mldsa": 10648, "layer_aware": 3252},
        )
        for config, expected_size in MESSAGE_BYTES_BY_CONFIG.items():
            messages = prepared_htlc_messages(config)
            self.assertEqual(len(messages.add), expected_size)
            self.assertEqual(len(messages.settle), expected_size)
            self.assertNotEqual(messages.add, messages.settle)
            self.assertEqual(messages, prepared_htlc_messages(config))

    def test_warmup_retained_but_excluded(self):
        records = run_condition(
            "cfg",
            NetworkCondition(5, 1),
            FakeExecutor(values=(100, 2, 4)),
            PREPARED_MESSAGES,
            RunOptions(1, 2, False),
        )
        self.assertEqual([record.phase for record in records], ["warmup", "measured", "measured"])
        row = summarize_condition(records, 0, None)
        self.assertEqual(row.n_iter, 2)
        self.assertEqual(row.completion_median_ms, 3)

    def test_failure_preserved_and_scientific_summary_rejected(self):
        records = run_condition(
            "cfg",
            NetworkCondition(5, 1),
            FakeExecutor(fail_calls=(0,)),
            PREPARED_MESSAGES,
            RunOptions(0, 2, False),
        )
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].error, "deterministic completion failure")

        scientific = scientific_records()
        scientific[50] = RawCompletionRecord(**{**scientific[50].__dict__, "success": False, "error": "failed"})
        with self.assertRaises(ValueError):
            summarize_condition(
                scientific,
                MINIMUM_SCIENTIFIC_ITERATIONS,
                passing_ping("classical", NetworkCondition(5, 1)),
            )

    def test_missing_and_duplicate_scientific_iterations_rejected(self):
        verification = passing_ping("classical", NetworkCondition(5, 1))
        missing = scientific_records()[:-1]
        with self.assertRaises(ValueError):
            summarize_condition(missing, MINIMUM_SCIENTIFIC_ITERATIONS, verification)
        duplicate = scientific_records()
        duplicate[-1] = RawCompletionRecord(**{**duplicate[-1].__dict__, "iteration": 998})
        with self.assertRaises(ValueError):
            summarize_condition(duplicate, MINIMUM_SCIENTIFIC_ITERATIONS, verification)

    def test_complete_scientific_summary_and_warmup_exclusion(self):
        records = scientific_records()
        records.insert(
            0,
            RawCompletionRecord(
                "classical", 5, 1, 0, "warmup", True, 999.0, True, ""
            ),
        )
        row = summarize_condition(
            records,
            MINIMUM_SCIENTIFIC_ITERATIONS,
            passing_ping("classical", NetworkCondition(5, 1)),
        )
        self.assertTrue(row.scientific)
        self.assertEqual(row.n_iter, 1000)
        self.assertEqual(
            (row.completion_median_ms, row.completion_p95_ms, row.completion_p99_ms),
            (5, 5, 5),
        )


class StatisticsTests(unittest.TestCase):
    def test_known_vectors_and_input_not_mutated(self):
        cases = (
            ([7.0], CompletionStatistics(7, 7, 7)),
            ([4.0, 1.0, 3.0, 2.0], CompletionStatistics(2.5, 3.85, 3.97)),
            ([5.0, 1.0, 4.0, 2.0, 3.0], CompletionStatistics(3, 4.8, 4.96)),
        )
        for values, expected in cases:
            original = list(values)
            actual = completion_statistics(values)
            self.assertEqual(values, original)
            self.assertAlmostEqual(actual.median_ms, expected.median_ms)
            self.assertAlmostEqual(actual.p95_ms, expected.p95_ms)
            self.assertAlmostEqual(actual.p99_ms, expected.p99_ms)

    def test_empty_and_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            completion_statistics([])
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                completion_statistics([value])


class WriterTests(unittest.TestCase):
    def test_non_scientific_rows_rejected_and_scientific_rows_written(self):
        rows = complete_rows()
        non_scientific = list(rows)
        non_scientific[0] = SummaryRow(**{**non_scientific[0].__dict__, "scientific": False})
        with self.assertRaises(ValueError):
            write_final_csv(io.StringIO(), non_scientific)
        output = io.StringIO()
        write_final_csv(output, rows)
        self.assertTrue(output.getvalue().startswith(",".join(FINAL_FIELDS) + "\n"))

    def test_final_completeness_and_value_guards(self):
        rows = complete_rows()
        with self.assertRaises(ValueError):
            validate_final_rows(rows[:-1])
        duplicate = list(rows)
        duplicate[-1] = duplicate[0]
        with self.assertRaises(ValueError):
            validate_final_rows(duplicate)
        invalid_rtt = list(rows)
        invalid_rtt[0] = SummaryRow(**{**invalid_rtt[0].__dict__, "rtt_ms": 7})
        with self.assertRaises(ValueError):
            validate_final_rows(invalid_rtt)
        invalid_hops = list(rows)
        invalid_hops[0] = SummaryRow(**{**invalid_hops[0].__dict__, "hops": 0})
        with self.assertRaises(ValueError):
            validate_final_rows(invalid_hops)
        unknown = list(rows)
        unknown[0] = SummaryRow(**{**unknown[0].__dict__, "config": "unknown"})
        with self.assertRaises(ValueError):
            validate_final_rows(unknown)

    def test_final_csv_order_schema_and_no_p99(self):
        rows = complete_rows()
        rows.reverse()
        output = io.StringIO()
        write_final_csv(output, rows)
        parsed = list(csv.reader(io.StringIO(output.getvalue())))
        self.assertEqual(parsed[0], list(FINAL_FIELDS))
        self.assertEqual(len(parsed), 61)
        self.assertNotIn("p99", parsed[0])
        expected = []
        for config in CONFIG_VALUES:
            for condition in sweep_conditions():
                expected.append([config, str(condition.configured_rtt_ms), str(condition.hops)])
        self.assertEqual([row[:3] for row in parsed[1:]], expected)

    def test_raw_and_ping_csv_provenance(self):
        raw = [
            RawCompletionRecord("cfg", 5, 1, 0, "warmup", False, 1.0, True, ""),
            RawCompletionRecord("cfg", 5, 1, 0, "measured", False, 0.0, False, "failed"),
        ]
        raw_output = io.StringIO()
        write_raw_completion_csv(raw_output, raw)
        parsed_raw = list(csv.reader(io.StringIO(raw_output.getvalue())))
        self.assertEqual(parsed_raw[0], list(RAW_FIELDS))
        self.assertEqual(parsed_raw[1][4], "warmup")
        self.assertEqual(parsed_raw[2][4], "measured")
        self.assertEqual(parsed_raw[2][7:], ["false", "failed"])

        verification = verify_ping("cfg", NetworkCondition(5, 1), ping_output(4.9, 5.1))
        ping_csv = io.StringIO()
        write_ping_verification_csv(ping_csv, [verification])
        parsed_ping = list(csv.reader(io.StringIO(ping_csv.getvalue())))
        self.assertEqual(parsed_ping[0], list(PING_FIELDS))
        self.assertEqual(parsed_ping[1][3], "5.000000")
        self.assertEqual(parsed_ping[1][4], "2")
        self.assertEqual(parsed_ping[1][5], "4.900000;5.100000")
        self.assertEqual(parsed_ping[1][8], verification.raw_output_sha256)
        self.assertEqual(parsed_ping[1][9], str(verification.raw_output_bytes))
        self.assertEqual(parsed_ping[1][-1], "true")


if __name__ == "__main__":
    unittest.main()
