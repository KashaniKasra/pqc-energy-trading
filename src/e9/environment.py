"""Collection and fail-closed validation of E9 timing-environment provenance."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Mapping


def _read_optional(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return value or None


def _parse_cpu_list(value: str) -> list[int]:
    cpus: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start < 0 or end < start:
                raise ValueError(f"invalid CPU range: {item}")
            cpus.update(range(start, end + 1))
        else:
            cpu = int(item)
            if cpu < 0:
                raise ValueError(f"invalid CPU index: {item}")
            cpus.add(cpu)
    return sorted(cpus)


def _policy_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("policy")
    return (int(suffix), path.name) if suffix.isdigit() else (2**31 - 1, path.name)


def _collect_policy_field(policies: list[Path], field: str) -> dict[str, object]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for policy in policies:
        value = _read_optional(policy / field)
        if value is None:
            missing.append(policy.name)
        else:
            values[policy.name] = value
    if not values:
        status = "unavailable"
    elif missing:
        status = "partial"
    else:
        status = "available"
    return {
        "status": status,
        "values_by_policy": values,
        "missing_policies": missing,
    }


def _cpu_model(proc_cpuinfo: Path) -> dict[str, object]:
    text = _read_optional(proc_cpuinfo)
    if text is None:
        return {"status": "unavailable", "value": None}
    for line in text.splitlines():
        if line.lower().startswith("model name") and ":" in line:
            return {"status": "available", "value": line.split(":", 1)[1].strip()}
    return {"status": "unavailable", "value": None}


def _online_cpus(cpu_root: Path) -> dict[str, object]:
    value = _read_optional(cpu_root / "online")
    if value is None:
        return {"status": "unavailable", "cpu_list": None, "cpus": [], "count": None}
    try:
        cpus = _parse_cpu_list(value)
    except ValueError:
        return {"status": "invalid", "cpu_list": value, "cpus": [], "count": None}
    return {"status": "available", "cpu_list": value, "cpus": cpus, "count": len(cpus)}


def _boost(cpu_root: Path) -> dict[str, object]:
    candidates = (
        cpu_root / "cpufreq/boost",
        cpu_root / "amd_pstate/cpb_boost",
    )
    for path in candidates:
        value = _read_optional(path)
        if value is None:
            continue
        enabled = {"0": False, "1": True}.get(value)
        return {
            "status": "available" if enabled is not None else "invalid",
            "path": str(path),
            "value": value,
            "enabled": enabled,
        }
    return {"status": "unavailable", "path": None, "value": None, "enabled": None}


def _ac_online(sys_root: Path) -> dict[str, object]:
    supplies = sys_root / "class/power_supply"
    sources: dict[str, int] = {}
    if supplies.is_dir():
        for supply in sorted(supplies.iterdir(), key=lambda path: path.name):
            if _read_optional(supply / "type") != "Mains":
                continue
            value = _read_optional(supply / "online")
            if value in ("0", "1"):
                sources[supply.name] = int(value)
    if not sources:
        return {"status": "unavailable", "sources": {}, "online": None}
    return {
        "status": "available",
        "sources": sources,
        "online": any(value == 1 for value in sources.values()),
    }


def collect_timing_environment(
    *,
    sys_root: Path = Path("/sys"),
    proc_cpuinfo: Path = Path("/proc/cpuinfo"),
    affinity_getter: Callable[[int], Iterable[int]] = os.sched_getaffinity,
) -> dict[str, object]:
    """Read policy state without changing any CPU or power setting."""
    cpu_root = sys_root / "devices/system/cpu"
    policies = sorted((cpu_root / "cpufreq").glob("policy*"), key=_policy_sort_key)
    try:
        affinity = sorted(set(int(cpu) for cpu in affinity_getter(0)))
        affinity_record: dict[str, object] = {"status": "available", "cpus": affinity}
    except (AttributeError, OSError, ValueError):
        affinity_record = {"status": "unavailable", "cpus": []}
    return {
        "cpu": {
            "model": _cpu_model(proc_cpuinfo),
            "online": _online_cpus(cpu_root),
            "policy_names": [policy.name for policy in policies],
            "scaling_driver": _collect_policy_field(policies, "scaling_driver"),
            "governor": _collect_policy_field(policies, "scaling_governor"),
            "energy_performance_preference": _collect_policy_field(
                policies, "energy_performance_preference"
            ),
            "scaling_min_freq_khz": _collect_policy_field(policies, "scaling_min_freq"),
            "scaling_max_freq_khz": _collect_policy_field(policies, "scaling_max_freq"),
            "boost": _boost(cpu_root),
            "process_affinity": affinity_record,
        },
        "power": {"ac_online": _ac_online(sys_root)},
    }


def _require_complete_policy_field(cpu: Mapping[str, object], name: str) -> dict[str, str]:
    record = cpu.get(name)
    if not isinstance(record, Mapping) or record.get("status") != "available":
        raise ValueError(f"scientific E9 requires complete {name} provenance")
    values = record.get("values_by_policy")
    if not isinstance(values, Mapping) or not values:
        raise ValueError(f"scientific E9 requires non-empty {name} values")
    return {str(policy): str(value) for policy, value in values.items()}


def validate_scientific_environment(provenance: Mapping[str, object]) -> None:
    """Fail closed when controlled timing policy cannot be established."""
    cpu = provenance.get("cpu")
    power = provenance.get("power")
    if not isinstance(cpu, Mapping) or not isinstance(power, Mapping):
        raise ValueError("scientific E9 timing provenance is incomplete")

    drivers = _require_complete_policy_field(cpu, "scaling_driver")
    if len(set(drivers.values())) != 1:
        raise ValueError("scaling drivers are inconsistent across CPU policies")

    governors = _require_complete_policy_field(cpu, "governor")
    if set(governors.values()) != {"performance"}:
        raise ValueError("scientific E9 requires performance governor on every policy")

    epp = cpu.get("energy_performance_preference")
    if not isinstance(epp, Mapping):
        raise ValueError("invalid energy_performance_preference provenance")
    if epp.get("status") == "available":
        values = epp.get("values_by_policy")
        if not isinstance(values, Mapping) or set(values.values()) != {"performance"}:
            raise ValueError("scientific E9 requires performance EPP when available")
    elif epp.get("status") != "unavailable":
        raise ValueError("EPP availability is inconsistent across CPU policies")

    minimums = _require_complete_policy_field(cpu, "scaling_min_freq_khz")
    maximums = _require_complete_policy_field(cpu, "scaling_max_freq_khz")
    if minimums.keys() != maximums.keys():
        raise ValueError("scaling frequency policy sets do not match")
    try:
        pinned = {int(value) for value in minimums.values()}
        maximum_values = {int(value) for value in maximums.values()}
    except ValueError as exc:
        raise ValueError("scaling frequency limits must be integer kHz values") from exc
    if any(minimums[policy] != maximums[policy] for policy in minimums):
        raise ValueError("scientific E9 requires scaling_min_freq == scaling_max_freq")
    if len(pinned) != 1 or pinned != maximum_values:
        raise ValueError("frequency limits are inconsistent across CPU policies")

    boost = cpu.get("boost")
    if not isinstance(boost, Mapping) or boost.get("status") != "available":
        raise ValueError("scientific E9 requires readable boost-state provenance")
    if boost.get("enabled") is not False:
        raise ValueError("scientific E9 requires boost disabled")

    affinity = cpu.get("process_affinity")
    if (
        not isinstance(affinity, Mapping)
        or affinity.get("status") != "available"
        or not affinity.get("cpus")
    ):
        raise ValueError("scientific E9 requires non-empty process CPU affinity")

    ac = power.get("ac_online")
    if not isinstance(ac, Mapping):
        raise ValueError("invalid AC-power provenance")
    if ac.get("status") == "available" and ac.get("online") is not True:
        raise ValueError("scientific E9 requires AC online when AC state is available")
    if ac.get("status") not in ("available", "unavailable"):
        raise ValueError("invalid AC-power availability state")
