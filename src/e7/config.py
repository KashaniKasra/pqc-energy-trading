"""Authoritative E7 configuration registry and settlement semantics."""

from __future__ import annotations

from dataclasses import dataclass


CONFIG_NAMES = (
    "l1_only",
    "sets_reimpl",
    "classical_l2",
    "layer_aware_l2",
)


@dataclass(frozen=True)
class E7Configuration:
    name: str
    settlement_path: str
    cryptography: str
    completion_definition: str
    uses_layer2: bool
    description: str
    sets_reimplementation: bool = False


CONFIGURATIONS = {
    "l1_only": E7Configuration(
        name="l1_only",
        settlement_path="Layer 1; every settlement committed and validated by Fabric",
        cryptography="ML-DSA-65",
        completion_definition="committed/validated Fabric transaction status",
        uses_layer2=False,
        description="Project Layer-1-only post-quantum baseline with no Layer 2",
    ),
    "sets_reimpl": E7Configuration(
        name="sets_reimpl",
        settlement_path="Layer 1; every settlement recorded on the main-chain ledger",
        cryptography="ECDSA",
        completion_definition="committed/validated Fabric transaction status",
        uses_layer2=False,
        description=(
            "Our limited classical reimplementation of the published SETS on-chain "
            "behavior: producer offer, consumer request, controller eligibility/balance "
            "validation, and ledger settlement"
        ),
        sets_reimplementation=True,
    ),
    "classical_l2": E7Configuration(
        name="classical_l2",
        settlement_path="Layer 2 payment-channel settlement",
        cryptography="E2 classical representative compact channel semantics",
        completion_definition="complete Layer-2 settlement operation returned successfully",
        uses_layer2=True,
        description="Project classical Layer-2 baseline reusing E2 semantics",
    ),
    "layer_aware_l2": E7Configuration(
        name="layer_aware_l2",
        settlement_path="Layer 2 payment-channel settlement with Layer-1-sensitive operations",
        cryptography=(
            "Falcon-padded-512 for Layer-2 channel operations; "
            "ML-DSA-65 for Layer-1-sensitive operations"
        ),
        completion_definition="complete Layer-2 settlement operation returned successfully",
        uses_layer2=True,
        description="Project layer-aware Layer-2 path reusing E2 semantics",
    ),
}


SETS_REIMPL_BEHAVIOR = (
    "producer_offer",
    "consumer_request",
    "controller_validate_eligibility_and_balance",
    "record_and_settle_on_layer_1_ledger",
)

COMMON_WORKLOAD_SEMANTICS = {
    "settlement_interval_minutes": 15,
    "replay_waits_wall_clock_interval": False,
    "trace_policy": "same prepared trace, row order, and measured sample_idx population",
    "sample_idx": "zero-based index within the measured rows after warm-up",
    "latency_start": "immediately before processing/submitting one settlement sample",
    "latency_stop": "when the selected configuration reports settlement complete",
    "summary_metric_composition": False,
}


def get_configuration(name: str) -> E7Configuration:
    try:
        return CONFIGURATIONS[name]
    except KeyError as error:
        raise ValueError(f"unknown E7 configuration: {name!r}") from error


def validate_registry() -> None:
    if tuple(CONFIGURATIONS) != CONFIG_NAMES:
        raise ValueError("E7 configuration registry order or names differ from authority")
    if any(configuration.name != name for name, configuration in CONFIGURATIONS.items()):
        raise ValueError("E7 configuration registry key/name mismatch")
    sets = CONFIGURATIONS["sets_reimpl"]
    if not sets.sets_reimplementation or sets.cryptography != "ECDSA":
        raise ValueError("sets_reimpl must be the classical ECDSA SETS reimplementation")
    if "Layer 1" not in sets.settlement_path:
        raise ValueError("sets_reimpl must settle entirely on Layer 1")
    if sets.uses_layer2:
        raise ValueError("sets_reimpl must not use Layer 2")


validate_registry()
