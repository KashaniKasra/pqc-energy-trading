#!/usr/bin/env python3
"""Authoritative E9 payment-channel path topology.

``hops`` is the number of payment-channel links. Intermediate Mininet hosts
forward packets but perform no cryptography or application computation.
"""

from mininet.link import TCLink
from mininet.node import Host
from mininet.topo import Topo

from src.e9.network import (
    NetworkCondition,
    intermediate_node_count,
    payment_channel_link_count,
    payment_channel_node_names,
    per_channel_one_way_delay_ms,
)


class PaymentChannelHost(Host):
    """Network-namespace host with optional minimal IPv4 forwarding/routes."""

    def config(self, forwarding=False, routes=(), **params):
        result = super().config(**params)
        self._e9_forwarding = bool(forwarding)
        if self._e9_forwarding:
            self.cmd("sysctl -qw net.ipv4.ip_forward=1")
        for route in routes:
            self.cmd(f"ip route replace {route}")
        return result

    def terminate(self):
        if getattr(self, "_e9_forwarding", False):
            self.cmd("sysctl -qw net.ipv4.ip_forward=0")
        super().terminate()


def _endpoint_routes(node_index, hops):
    sender_ip = "10.0.1.1"
    receiver_ip = f"10.0.{hops}.2"
    routes = []
    if node_index > 0:
        left_neighbor_ip = f"10.0.{node_index}.1"
        routes.append(f"{sender_ip}/32 via {left_neighbor_ip}")
    if node_index < hops:
        right_neighbor_ip = f"10.0.{node_index + 1}.2"
        routes.append(f"{receiver_ip}/32 via {right_neighbor_ip}")
    return routes


class E9LinearTopology(Topo):
    """A -- I1 -- ... -- B, with one calibrated TCLink per payment channel.

    ``target_rtt_ms`` remains the logical per-channel RTT; the injected delay
    subtracts the explicit host/testbed stack calibration in ``src.e9``.
    """

    def build(self, hops=1, target_rtt_ms=5):
        condition = NetworkCondition(int(target_rtt_ms), int(hops))
        condition.validate()
        delay = f"{per_channel_one_way_delay_ms(condition):.9f}ms"
        names = payment_channel_node_names(condition.hops)
        nodes = []
        for index, name in enumerate(names):
            nodes.append(
                self.addHost(
                    name,
                    cls=PaymentChannelHost,
                    ip=None,
                    forwarding=0 < index < condition.hops,
                    routes=_endpoint_routes(index, condition.hops),
                )
            )
        if len(nodes) - 2 != intermediate_node_count(condition.hops):
            raise RuntimeError("internal E9 intermediate-node count mismatch")
        for index, (left, right) in enumerate(zip(nodes, nodes[1:]), start=1):
            self.addLink(
                left,
                right,
                cls=TCLink,
                delay=delay,
                params1={"ip": f"10.0.{index}.1/30"},
                params2={"ip": f"10.0.{index}.2/30"},
            )
        if len(nodes) - 1 != payment_channel_link_count(condition.hops):
            raise RuntimeError("internal E9 payment-channel link count mismatch")


topos = {"e9linear": E9LinearTopology}
