import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from src.e9.evidence import persist_condition_evidence, validate_condition_manifest
from src.e9.network import (
    MESSAGE_BYTES_BY_CONFIG,
    MINIMUM_SCIENTIFIC_ITERATIONS,
    NetworkCondition,
    RawCompletionRecord,
    RunOptions,
    prepared_htlc_messages,
    verify_ping,
)
from src.e9.transport import (
    FRAME_HEADER,
    FRAME_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    MESSAGE_TYPE_ADD,
    MESSAGE_TYPE_SETTLE,
    PersistentHTLCSender,
    TransportError,
    configure_connected_socket,
    encode_frame,
    expect_frame,
    node_connection_plan,
    receiver_once,
    recv_frame,
    relay_once,
)


def ping_output(value):
    return f"64 bytes from receiver: icmp_seq=1 ttl=64 time={value} ms\n"


def valid_environment_provenance():
    policies = {"policy0": "test-driver", "policy1": "test-driver"}
    performance = {"policy0": "performance", "policy1": "performance"}
    frequency = {"policy0": "3000000", "policy1": "3000000"}
    return {
        "software": {"python": "test", "git_commit": "test", "git_dirty": False},
        "timing": {
            "cpu": {
                "model": {"status": "available", "value": "Test CPU"},
                "online": {
                    "status": "available",
                    "cpu_list": "0-3",
                    "cpus": [0, 1, 2, 3],
                    "count": 4,
                },
                "policy_names": ["policy0", "policy1"],
                "scaling_driver": {
                    "status": "available",
                    "values_by_policy": policies,
                    "missing_policies": [],
                },
                "governor": {
                    "status": "available",
                    "values_by_policy": performance,
                    "missing_policies": [],
                },
                "energy_performance_preference": {
                    "status": "available",
                    "values_by_policy": performance,
                    "missing_policies": [],
                },
                "scaling_min_freq_khz": {
                    "status": "available",
                    "values_by_policy": frequency,
                    "missing_policies": [],
                },
                "scaling_max_freq_khz": {
                    "status": "available",
                    "values_by_policy": frequency,
                    "missing_policies": [],
                },
                "boost": {
                    "status": "available",
                    "path": "/fake/boost",
                    "value": "0",
                    "enabled": False,
                },
                "process_affinity": {"status": "available", "cpus": [2, 4, 6]},
            },
            "power": {
                "ac_online": {"status": "available", "sources": {"AC0": 1}, "online": True}
            },
        },
    }


class ChunkSocket:
    def __init__(self, content, chunk_size=1):
        self.content = content
        self.chunk_size = chunk_size

    def recv(self, length):
        if not self.content:
            return b""
        count = min(length, self.chunk_size, len(self.content))
        result, self.content = self.content[:count], self.content[count:]
        return result


class EventSocket:
    def __init__(self, events, incoming=b""):
        self.events = events
        self.incoming = incoming

    def sendall(self, content):
        self.events.append(("sendall", content))

    def recv(self, length):
        self.events.append(("recv", length))
        result, self.incoming = self.incoming[:length], self.incoming[length:]
        return result


class FramingTests(unittest.TestCase):
    def test_fixed_width_header_and_partial_reads(self):
        payload = b"prepared"
        frame = encode_frame(MESSAGE_TYPE_ADD, payload)
        self.assertEqual(FRAME_HEADER_BYTES, 5)
        self.assertEqual(frame[:5], struct.pack("!BI", MESSAGE_TYPE_ADD, len(payload)))
        self.assertEqual(recv_frame(ChunkSocket(frame, 1)), (MESSAGE_TYPE_ADD, payload))

    def test_short_oversize_wrong_type_and_corruption_fail_closed(self):
        with self.assertRaisesRegex(TransportError, "short"):
            recv_frame(ChunkSocket(FRAME_HEADER.pack(MESSAGE_TYPE_ADD, 10) + b"short", 3))
        with self.assertRaisesRegex(TransportError, "invalid E9 payload length"):
            recv_frame(ChunkSocket(FRAME_HEADER.pack(MESSAGE_TYPE_ADD, MAX_PAYLOAD_BYTES + 1)))
        with self.assertRaisesRegex(TransportError, "unsupported E9 frame type"):
            recv_frame(ChunkSocket(FRAME_HEADER.pack(99, 1) + b"x"))
        corrupt = encode_frame(MESSAGE_TYPE_SETTLE, b"wrong")
        with self.assertRaisesRegex(TransportError, "content/length mismatch"):
            expect_frame(ChunkSocket(corrupt, 2), MESSAGE_TYPE_SETTLE, b"expected")

    def test_neighbor_address_plan_uses_each_direct_link(self):
        sender = node_connection_plan(0, 3)
        middle = node_connection_plan(1, 3)
        receiver = node_connection_plan(3, 3)
        self.assertEqual(sender.outgoing_add_source_ip, "10.0.1.1")
        self.assertEqual(sender.outgoing_add_peer_ip, "10.0.1.2")
        self.assertEqual(middle.incoming_add_listen_ip, "10.0.1.2")
        self.assertEqual(middle.outgoing_add_peer_ip, "10.0.2.2")
        self.assertEqual(receiver.incoming_add_listen_ip, "10.0.3.2")
        self.assertEqual(receiver.outgoing_settle_peer_ip, "10.0.3.1")

    def test_real_tcp_socket_uses_nodelay(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(listener.getsockname())
            server, _ = listener.accept()
            try:
                configure_connected_socket(client)
                configure_connected_socket(server)
                self.assertEqual(client.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
                self.assertEqual(server.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
            finally:
                server.close()
        finally:
            client.close()
            listener.close()


class ExecutorTests(unittest.TestCase):
    def test_two_hop_iteration_is_add_forward_and_settle_reverse(self):
        messages = prepared_htlc_messages("classical")
        a_add, relay_add_in = socket.socketpair()
        relay_add_out, b_add = socket.socketpair()
        b_settle, relay_settle_in = socket.socketpair()
        relay_settle_out, a_settle = socket.socketpair()
        sockets = (
            a_add,
            relay_add_in,
            relay_add_out,
            b_add,
            b_settle,
            relay_settle_in,
            relay_settle_out,
            a_settle,
        )
        errors = []

        def run_relay():
            try:
                relay_once(
                    relay_add_in,
                    relay_add_out,
                    relay_settle_in,
                    relay_settle_out,
                    messages,
                )
            except Exception as exc:
                errors.append(exc)

        def run_receiver():
            try:
                receiver_once(b_add, b_settle, messages)
            except Exception as exc:
                errors.append(exc)

        relay_thread = threading.Thread(target=run_relay)
        receiver_thread = threading.Thread(target=run_receiver)
        relay_thread.start()
        receiver_thread.start()
        try:
            elapsed = PersistentHTLCSender(a_add, a_settle).execute(
                "classical", NetworkCondition(5, 2), messages
            )
            self.assertGreaterEqual(elapsed, 0)
            relay_thread.join(2)
            receiver_thread.join(2)
            self.assertFalse(relay_thread.is_alive())
            self.assertFalse(receiver_thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            for connection in sockets:
                connection.close()

    def test_timer_excludes_setup_and_brackets_send_through_complete_receive(self):
        messages = prepared_htlc_messages("classical")
        events = []
        clock_values = iter((1_000_000, 4_000_000))

        def clock():
            events.append(("clock",))
            return next(clock_values)

        add = EventSocket(events)
        settle = EventSocket(events, encode_frame(MESSAGE_TYPE_SETTLE, messages.settle))
        executor = PersistentHTLCSender(add, settle, clock_ns=clock)
        self.assertEqual(events, [])
        self.assertEqual(executor.execute("classical", NetworkCondition(5, 1), messages), 3.0)
        self.assertEqual(events[0], ("clock",))
        self.assertEqual(events[1][0], "sendall")
        self.assertEqual(events[-1], ("clock",))
        self.assertTrue(any(event[0] == "recv" for event in events[2:-1]))

    def test_transport_contains_no_crypto_operations(self):
        source = (Path(__file__).with_name("transport.py")).read_text(encoding="utf-8").lower()
        for forbidden in ("liboqs", "keygen", "signature generation", "verify("):
            self.assertNotIn(forbidden, source)


class EvidenceAndCLITests(unittest.TestCase):
    def _scientific_records(self):
        return [
            RawCompletionRecord(
                "classical",
                5,
                1,
                iteration,
                "measured",
                True,
                5.0,
                True,
                "",
            )
            for iteration in range(MINIMUM_SCIENTIFIC_ITERATIONS)
        ]

    def test_manifest_hashes_match_files_and_temp_evidence_is_cleaned(self):
        condition = NetworkCondition(5, 1)
        verification = verify_ping("classical", condition, ping_output(5.0))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = persist_condition_evidence(
                root,
                self._scientific_records(),
                verification,
                RunOptions(0, MINIMUM_SCIENTIFIC_ITERATIONS, True),
                MESSAGE_BYTES_BY_CONFIG["classical"],
                valid_environment_provenance(),
            )
            validate_condition_manifest(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "pqc-energy-trading.e9-condition-evidence.v2")
            for section in ("ping_verification", "completion_samples"):
                item = manifest[section]
                content = (root / item["filename"]).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            self.assertEqual(
                (root / manifest["ping_verification"]["filename"]).read_text(encoding="utf-8"),
                verification.raw_output,
            )
            self.assertEqual(manifest["condition"]["prepared_message_bytes"], 222)
            timing = manifest["environment"]["timing"]
            self.assertEqual(timing["cpu"]["model"]["value"], "Test CPU")
            self.assertEqual(timing["cpu"]["process_affinity"]["cpus"], [2, 4, 6])
            self.assertEqual(timing["power"]["ac_online"]["online"], True)
            with self.assertRaises(FileExistsError):
                persist_condition_evidence(
                    root,
                    self._scientific_records(),
                    verification,
                    RunOptions(0, MINIMUM_SCIENTIFIC_ITERATIONS, True),
                    222,
                    valid_environment_provenance(),
                )
            retained_root = root
        self.assertFalse(retained_root.exists())

    def test_preflight_cannot_target_final_csv(self):
        runner_path = Path(__file__).parents[2] / "env/mininet/e9/run_e9.py"
        specification = importlib.util.spec_from_file_location("e9_runner_test", runner_path)
        runner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(runner)
        args = argparse.Namespace(
            config="classical",
            rtt_ms=5,
            hops=1,
            warmup=1,
            iterations=3,
            output_root="data/e9_network.csv",
            scientific=False,
            preflight=True,
        )
        with self.assertRaisesRegex(ValueError, "never writes"):
            runner.validate_cli_mode(args)

        args.output_root = "raw/e9"
        args.iterations = 11
        with self.assertRaisesRegex(ValueError, "at most 10"):
            runner.validate_cli_mode(args)

        args.preflight = False
        args.scientific = True
        args.iterations = 999
        with self.assertRaisesRegex(ValueError, "at least 1000"):
            runner.validate_cli_mode(args)


if __name__ == "__main__":
    unittest.main()
