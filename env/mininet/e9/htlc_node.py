#!/usr/bin/env python3
"""One network-only E9 path node, launched inside a Mininet host."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.e9.network import (  # noqa: E402
    NetworkCondition,
    RunOptions,
    prepared_htlc_messages,
    run_condition,
    verify_ping,
)
from src.e9.transport import (  # noqa: E402
    ADD_PORT,
    SETTLE_PORT,
    PersistentHTLCSender,
    configure_connected_socket,
    node_connection_plan,
    receiver_once,
    relay_once,
)


def _listener(address: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((address, port))
    listener.listen(1)
    return listener


def _connect(source: str, peer: str, port: int, deadline: float) -> socket.socket:
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            connection.bind((source, 0))
            connection.connect((peer, port))
            configure_connected_socket(connection)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
            time.sleep(0.05)
    raise RuntimeError(f"could not connect {source} -> {peer}:{port}: {last_error}")


def _wait_for(path: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {path}")


def _write_records(path: Path, records) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(json.dumps(record.__dict__, sort_keys=True, separators=(",", ":")))
            output.write("\n")


def run_node(args: argparse.Namespace) -> None:
    condition = NetworkCondition(args.rtt_ms, args.hops)
    condition.validate()
    plan = node_connection_plan(args.index, args.hops)
    messages = prepared_htlc_messages(args.config)
    total_iterations = args.warmup + args.iterations
    control_dir = Path(args.control_dir)
    deadline = time.monotonic() + args.connect_timeout

    add_listener = (
        _listener(plan.incoming_add_listen_ip, ADD_PORT)
        if plan.incoming_add_listen_ip
        else None
    )
    settle_listener = (
        _listener(plan.incoming_settle_listen_ip, SETTLE_PORT)
        if plan.incoming_settle_listen_ip
        else None
    )
    sockets: list[socket.socket] = []
    try:
        outgoing_add = (
            _connect(
                plan.outgoing_add_source_ip,
                plan.outgoing_add_peer_ip,
                ADD_PORT,
                deadline,
            )
            if plan.outgoing_add_peer_ip
            else None
        )
        outgoing_settle = (
            _connect(
                plan.outgoing_settle_source_ip,
                plan.outgoing_settle_peer_ip,
                SETTLE_PORT,
                deadline,
            )
            if plan.outgoing_settle_peer_ip
            else None
        )
        incoming_add = add_listener.accept()[0] if add_listener else None
        incoming_settle = settle_listener.accept()[0] if settle_listener else None
        sockets = [
            connection
            for connection in (outgoing_add, outgoing_settle, incoming_add, incoming_settle)
            if connection is not None
        ]
        for connection in sockets:
            configure_connected_socket(connection)
            connection.settimeout(args.io_timeout)
        (control_dir / f"ready-{args.index}").touch(exist_ok=False)
        _wait_for(control_dir / "start", args.start_timeout)

        if args.index == 0:
            assert outgoing_add is not None and incoming_settle is not None
            raw_ping = Path(args.ping_file).read_text(encoding="utf-8")
            verification = verify_ping(args.config, condition, raw_ping)
            records = run_condition(
                args.config,
                condition,
                PersistentHTLCSender(outgoing_add, incoming_settle),
                messages,
                RunOptions(args.warmup, args.iterations, args.scientific),
                verification if args.scientific else None,
            )
            _write_records(control_dir / "sender-records.jsonl", records)
        elif args.index == args.hops:
            assert incoming_add is not None and outgoing_settle is not None
            for _ in range(total_iterations):
                receiver_once(incoming_add, outgoing_settle, messages)
        else:
            assert all(
                connection is not None
                for connection in (incoming_add, outgoing_add, incoming_settle, outgoing_settle)
            )
            for _ in range(total_iterations):
                relay_once(
                    incoming_add,
                    outgoing_add,
                    incoming_settle,
                    outgoing_settle,
                    messages,
                )
    finally:
        for connection in sockets:
            connection.close()
        if add_listener:
            add_listener.close()
        if settle_listener:
            settle_listener.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--hops", type=int, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--rtt-ms", type=int, required=True)
    parser.add_argument("--warmup", type=int, required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--ping-file", required=True)
    parser.add_argument("--scientific", action="store_true")
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--start-timeout", type=float, default=30.0)
    parser.add_argument("--io-timeout", type=float, default=30.0)
    return parser.parse_args()


if __name__ == "__main__":
    run_node(parse_args())
