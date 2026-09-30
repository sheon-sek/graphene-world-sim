"""Values that cross between runtime components: true state, samples and quality.

True state is what the models compute, in SI units (K, W, Pa, kg/s, fractions), keyed by
World Model asset id and signal name. Samples are what an instrument or point reports, with a
quality and a timestamp in simulation seconds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

type Value = float | bool | str | None

type TrueState = Mapping[str, Mapping[str, float | bool]]
"""Asset id -> signal -> value, in SI units."""


class Quality(StrEnum):
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


@dataclass(frozen=True, slots=True)
class Sample:
    value: Value
    quality: Quality
    timestamp: float
    """Simulation time in seconds when the value was last refreshed."""
    reason: str = ""
    """Why the quality is not good (`comm_lost`, `sensor_failed`, `not_simulated`, …)."""


def ref(asset: str, signal: str) -> str:
    """A `<asset>:<signal>` reference, as World Model control bindings and aggregates use."""
    return f"{asset}:{signal}"


def split_ref(reference: str) -> tuple[str, str]:
    """`Chiller/R_C1:TChwLvg` -> (`Chiller/R_C1`, `TChwLvg`). Asset ids may contain `:`."""
    asset, sep, signal = reference.rpartition(":")
    if not sep or not asset or not signal:
        raise ValueError(f"not an <asset>:<signal> reference: {reference!r}")
    return asset, signal


class StateView(Protocol):
    """Read access to the true state the models computed at the current step."""

    def get(self, asset: str, signal: str) -> float | bool | None:
        """The value in SI units, or None when no model computes this signal."""
        ...

    def unit(self, asset: str, signal: str) -> str | None:
        """The SI unit of a signal (`K`, `W`, `Pa`, `kg/s`, `1`), or None when unknown."""
        ...

    # A view may also define `monitored(asset, signal) -> bool`: whether the gateway computes
    # the signal about the asset (its communication status), so it stays fresh when the asset
    # itself cannot report.
