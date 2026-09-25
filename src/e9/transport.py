"""Fixed-frame, network-only TCP transport for E9 HTLC completion timing."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

from .network import (
    MESSAGE_BYTES_BY_CONFIG,
    NetworkCondition,
    PreparedHTLCMessages,
    link_endpoint_addresses,
    validate_authoritative_messages,
    validate_config,
)


FRAME_HEADER = struct.Struct("!BI")
FRAME_HEADER_BYTES = FRAME_HEADER.size
MESSAGE_TYPE_ADD = 1
MESSAGE_TYPE_SETTLE = 2
MAX_PAYLOAD_BYTES = max(MESSAGE_BYTES_BY_CONFIG.values())
ADD_PORT = 19091
SETTLE_PORT = 19092


class TransportError(RuntimeError):
    pass


def encode_frame(message_type: int, payload: bytes) -> bytes:
    if message_type not in (MESSAGE_TYPE_ADD, MESSAGE_TYPE_SETTLE):
        raise TransportError(f"unsupported E9 frame type: {message_type}")
    if not payload:
        raise TransportError("E9 frame payload must be non-empty")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise TransportError(f"E9 frame payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return FRAME_HEADER.pack(message_type, len(payload)) + payload


def recv_exact(connection: socket.socket, length: int) -> bytes:
    if length < 0:
        raise TransportError("negative receive length")
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise TransportError(f"short E9 frame: missing {remaining} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(connection: socket.socket) -> tuple[int, bytes]:
    header = recv_exact(connection, FRAME_HEADER_BYTES)
    message_type, payload_length = FRAME_HEADER.unpack(header)
    if message_type not in (MESSAGE_TYPE_ADD, MESSAGE_TYPE_SETTLE):
        raise TransportError(f"unsupported E9 frame type: {message_type}")
    if payload_length == 0 or payload_length > MAX_PAYLOAD_BYTES:
        raise TransportError(f"invalid E9 payload length: {payload_length}")
    return message_type, recv_exact(connection, payload_length)


def send_frame(connection: socket.socket, message_type: int, payload: bytes) -> None:
    connection.sendall(encode_frame(message_type, payload))


def expect_frame(
    connection: socket.socket,
    expected_type: int,
    expected_payload: bytes,
) -> None:
    message_type, payload = recv_frame(connection)
    if message_type != expected_type:
        raise TransportError(
            f"wrong E9 frame type: got {message_type}, expected {expected_type}"
        )
    if payload != expected_payload:
        raise TransportError("E9 payload content/length mismatch")


def configure_connected_socket(connection: socket.socket) -> None:
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


@dataclass(frozen=True)
class NodeConnectionPlan:
    index: int
    hops: int
    incoming_add_listen_ip: str | None
    outgoing_add_source_ip: str | None
    outgoing_add_peer_ip: str | None
    incoming_settle_listen_ip: str | None
    outgoing_settle_source_ip: str | None
    outgoing_settle_peer_ip: str | None


def node_connection_plan(index: int, hops: int) -> NodeConnectionPlan:
    if not 0 <= index <= hops:
        raise ValueError(f"node index {index} outside path 0..{hops}")
    incoming_add = outgoing_settle_source = outgoing_settle_peer = None
    outgoing_add_source = outgoing_add_peer = incoming_settle = None
    if index > 0:
        left_ip, right_ip = link_endpoint_addresses(index, hops)
        incoming_add = right_ip
        outgoing_settle_source = right_ip
        outgoing_settle_peer = left_ip
    if index < hops:
        left_ip, right_ip = link_endpoint_addresses(index + 1, hops)
        outgoing_add_source = left_ip
        outgoing_add_peer = right_ip
        incoming_settle = left_ip
    return NodeConnectionPlan(
        index=index,
        hops=hops,
        incoming_add_listen_ip=incoming_add,
        outgoing_add_source_ip=outgoing_add_source,
        outgoing_add_peer_ip=outgoing_add_peer,
        incoming_settle_listen_ip=incoming_settle,
        outgoing_settle_source_ip=outgoing_settle_source,
        outgoing_settle_peer_ip=outgoing_settle_peer,
    )


class PersistentHTLCSender:
    """Time one prepared ADD forward and one complete SETTLE return.

    Connections and payloads are established before ``execute``. The monotonic
    timer starts immediately before the framed ADD write and stops immediately
    after the complete, integrity-checked SETTLE has been received.
    """

    scientific = True

    def __init__(
        self,
        add_connection: socket.socket,
        settle_connection: socket.socket,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._add_connection = add_connection
        self._settle_connection = settle_connection
        self._clock_ns = clock_ns

    def execute(
        self,
        config: str,
        condition: NetworkCondition,
        messages: PreparedHTLCMessages,
    ) -> float:
        validate_config(config, scientific=True)
        condition.validate()
        validate_authoritative_messages(config, messages)
        # Transport framing and the already-prepared E2-sized payload are built
        # before the timer; completion_ms starts at the actual socket write.
        add_frame = encode_frame(MESSAGE_TYPE_ADD, messages.add)
        started_ns = self._clock_ns()
        self._add_connection.sendall(add_frame)
        returned_type, returned_payload = recv_frame(self._settle_connection)
        finished_ns = self._clock_ns()
        if returned_type != MESSAGE_TYPE_SETTLE:
            raise TransportError(
                f"wrong E9 frame type: got {returned_type}, expected {MESSAGE_TYPE_SETTLE}"
            )
        if returned_payload != messages.settle:
            raise TransportError("E9 payload content/length mismatch")
        if finished_ns < started_ns:
            raise TransportError("monotonic clock moved backwards")
        return (finished_ns - started_ns) / 1_000_000.0


def relay_once(
    incoming_add: socket.socket,
    outgoing_add: socket.socket,
    incoming_settle: socket.socket,
    outgoing_settle: socket.socket,
    messages: PreparedHTLCMessages,
) -> None:
    expect_frame(incoming_add, MESSAGE_TYPE_ADD, messages.add)
    send_frame(outgoing_add, MESSAGE_TYPE_ADD, messages.add)
    expect_frame(incoming_settle, MESSAGE_TYPE_SETTLE, messages.settle)
    send_frame(outgoing_settle, MESSAGE_TYPE_SETTLE, messages.settle)


def receiver_once(
    incoming_add: socket.socket,
    outgoing_settle: socket.socket,
    messages: PreparedHTLCMessages,
) -> None:
    expect_frame(incoming_add, MESSAGE_TYPE_ADD, messages.add)
    send_frame(outgoing_settle, MESSAGE_TYPE_SETTLE, messages.settle)
