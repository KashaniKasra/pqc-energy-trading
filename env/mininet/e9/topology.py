#!/usr/bin/env python3
"""Parameterized E9 Mininet topology; importing it makes no host changes.

Provisional definition: ``hops`` is the number of intermediate forwarding
switches between h1 and h2. The path therefore contains ``hops + 1`` links.
This interpretation requires professor confirmation before final measurement.
"""

from mininet.link import TCLink
from mininet.topo import Topo

from src.e9.network import NetworkCondition, path_link_count, per_link_one_way_delay_ms


class E9LinearTopology(Topo):
    """Two endpoints connected through 1..5 intermediate switches."""

    def build(self, hops: int = 1, target_rtt_ms: int = 5) -> None:
        condition = NetworkCondition(int(target_rtt_ms), int(hops))
        condition.validate()
        delay = f"{per_link_one_way_delay_ms(condition):.9f}ms"

        endpoint_a = self.addHost("h1")
        endpoint_b = self.addHost("h2")
        switches = [self.addSwitch(f"s{index}") for index in range(1, condition.hops + 1)]
        path = [endpoint_a, *switches, endpoint_b]
        if len(path) - 1 != path_link_count(condition.hops):
            raise RuntimeError("internal E9 path/link count mismatch")
        for left, right in zip(path, path[1:]):
            self.addLink(left, right, cls=TCLink, delay=delay)


topos = {"e9linear": E9LinearTopology}
