"""Fault framework (ADR-0002): a fault is a cause injected on one asset, instrument, point or
network element; the models compute every consequence.

A fault names one of its target type's fault modes and may set:

- parameters: the mode's own parameters (a capacity fraction, a bias); unset ones take the
  mode's defaults;
- `severity` (0–1, default 1): how far the fault's numeric parameters move from healthy to
  the values given. Healthy is 1 for a fraction (`*_fraction`, `gain`) and 0 otherwise;
- `ramp_s`: the severity grows linearly from 0 over this time (a fouling that develops);
- `duration_s`: the fault clears itself after this time (auto-clear).

A trip is latching: when it clears, the asset stays tripped until it is reset, as a
protection relay does. This module only keeps faults and their time course; the master
routes each active fault to the domain that owns its target.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_world_model.model import FaultKind


def healthy(parameter: str) -> float:
    """A fault parameter's value when there is no fault."""
    return 1.0 if parameter.endswith("fraction") or parameter == "gain" else 0.0


@dataclass
class Fault:
    id: str
    target: str
    mode: str
    kind: FaultKind
    domain: str
    """`thermofluid`, `electrical`, `network` or `sensor`: which domain acts on it."""
    parameters: dict[str, float] = field(default_factory=dict)
    severity: float = 1.0
    ramp_s: float = 0.0
    duration_s: float | None = None
    start: float = 0.0
    latching: bool = False
    cleared_at: float | None = None
    """When a latching fault cleared; it stays in effect until reset."""

    def progress(self, t: float) -> float:
        """Severity reached at time t, with the ramp."""
        if self.ramp_s <= 0:
            return self.severity
        return self.severity * min(max((t - self.start) / self.ramp_s, 0.0), 1.0)

    def values(self, t: float) -> dict[str, float]:
        """The fault's parameters at time t: healthy moved towards the set values."""
        s = self.progress(t)
        return {k: healthy(k) + (v - healthy(k)) * s for k, v in self.parameters.items()}

    @property
    def latched(self) -> bool:
        return self.cleared_at is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "mode": self.mode,
            "kind": self.kind.value,
            "domain": self.domain,
            "parameters": dict(self.parameters),
            "severity": self.severity,
            "ramp_s": self.ramp_s,
            "duration_s": self.duration_s,
            "start": self.start,
            "latching": self.latching,
            "cleared_at": self.cleared_at,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Fault:
        return cls(
            id=str(d["id"]),
            target=str(d["target"]),
            mode=str(d["mode"]),
            kind=FaultKind(d["kind"]),
            domain=str(d["domain"]),
            parameters={k: float(v) for k, v in d["parameters"].items()},
            severity=float(d["severity"]),
            ramp_s=float(d["ramp_s"]),
            duration_s=None if d["duration_s"] is None else float(d["duration_s"]),
            start=float(d["start"]),
            latching=bool(d["latching"]),
            cleared_at=None if d["cleared_at"] is None else float(d["cleared_at"]),
        )


class FaultBook:
    """The active faults, in injection order."""

    def __init__(self) -> None:
        self.faults: dict[str, Fault] = {}
        self._next = 1

    def add(self, fault: Fault) -> Fault:
        if not fault.id:
            fault.id = f"F{self._next}"
        self._next += 1
        if fault.id in self.faults:
            raise ValueError(f"fault {fault.id} already exists")
        self.faults[fault.id] = fault
        return fault

    def clear(self, fault_id: str, t: float) -> Fault:
        """Clear a fault. A latching one stays in effect, latched, until reset."""
        fault = self.faults[fault_id]
        if fault.latching:
            if fault.cleared_at is None:
                fault.cleared_at = t
        else:
            del self.faults[fault_id]
        return fault

    def reset(self, target: str) -> list[Fault]:
        """Reset a target: removes its latched faults (a fault still present stays)."""
        done = [f for f in self.faults.values() if f.target == target and f.latched]
        for f in done:
            del self.faults[f.id]
        return done

    def expire(self, t: float) -> list[Fault]:
        """Auto-clear the faults whose duration has passed."""
        due = [
            f
            for f in self.faults.values()
            if f.duration_s is not None and not f.latched and t >= f.start + f.duration_s - 1e-9
        ]
        for f in due:
            self.clear(f.id, t)
        return due

    def active(self) -> list[Fault]:
        return list(self.faults.values())

    def snapshot(self) -> dict[str, Any]:
        return {"next": self._next, "faults": [f.to_json() for f in self.faults.values()]}

    def restore(self, state: Mapping[str, Any]) -> None:
        self._next = int(state["next"])
        self.faults = {f["id"]: Fault.from_json(f) for f in state["faults"]}
