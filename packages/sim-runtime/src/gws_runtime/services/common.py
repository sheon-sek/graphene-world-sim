"""What every site-services system shares: its view of the rest of the site, and its state."""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gws_world_model.model import Scalar, WorldModel

type Signals = dict[str, float | bool | str]
type Faults = Mapping[str, Mapping[str, float]]
"""Active fault modes of one asset, mode name to its current parameters."""

WATER_KG_M3 = 1000.0
G = 9.81


@dataclass(frozen=True, slots=True)
class Env:
    """The rest of the site as the services see it at a step."""

    t: float
    dt: float
    supply: Callable[[str], float]
    """Per-unit voltage at an asset's supply terminals (1.0 for an asset with no supply)."""
    state: Mapping[str, Mapping[str, float | bool | str]]
    """True state the models published at the last step, SI."""
    reachable: Callable[[str], bool]
    """Whether an asset can reach its gateway."""


def param(doc: WorldModel, asset: str, name: str, fallback: float) -> float:
    """A numeric parameter of an asset, else its type's default, else `fallback`."""
    value: Scalar | None = doc.assets[asset].parameters.get(name)
    if value is None:
        spec = doc.component_types[doc.assets[asset].type].parameters.get(name)
        value = spec.default if spec is not None else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value)


def unit_fraction(key: str) -> float:
    """A stable number in [0, 1) for a key: the spread of one sensor among many."""
    return zlib.crc32(key.encode()) / 2**32


def number(state: Mapping[str, float | bool | str], signal: str) -> float | None:
    value = state.get(signal)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def smooth(key: str, t: float, period: float) -> float:
    """A seeded value in [0, 1) that varies smoothly with time."""
    b = math.floor(t / period)
    f = t / period - b
    a, c = unit_fraction(f"{key}:{b}"), unit_fraction(f"{key}:{b + 1}")
    return a + (c - a) * f * f * (3 - 2 * f)
