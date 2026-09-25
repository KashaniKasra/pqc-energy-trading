#!/usr/bin/env python3
"""Two-host E2 RTT topology: one direct 20 ms payment-channel link."""

from mininet.link import TCLink
from mininet.topo import Topo


TARGET_RTT_MS = 20
ONE_WAY_DELAY_MS = TARGET_RTT_MS / 2


class E2RTTTopology(Topo):
    def build(self):
        node_a = self.addHost("a", ip="10.20.0.1/30")
        node_b = self.addHost("b", ip="10.20.0.2/30")
        self.addLink(
            node_a,
            node_b,
            cls=TCLink,
            delay=f"{ONE_WAY_DELAY_MS:.3f}ms",
        )


topos = {"e2rtt": E2RTTTopology}
