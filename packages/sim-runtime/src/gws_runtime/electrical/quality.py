"""Power quality of the electrical domain: frequency and harmonics, from the load flow.

A load flow is a fundamental-frequency, balanced solution, so what a power meter shows beyond
it comes from here, from the network's topology and its latest solution:

- **Islands.** Buses joined by in-service lines, closed switches and transformers form an
  island. A UPS's input and output buses are never joined: the double conversion decouples
  them.
- **Frequency.** The utility grid runs at the grid frequency (an operating condition). A
  genset island runs on its governor's droop: `f = f_nominal x (1 - droop x (P / P_prime -
  0.5))`, so it sags as the sets load up. A UPS output follows its input's frequency while its
  rectifier runs (the inverter synchronises to bypass) and runs on its own oscillator at
  nominal frequency on battery.
- **Harmonic current.** Every load draws harmonic current in proportion to its fundamental
  current: switched-mode IT power supplies more at light load, drive-fed equipment (pumps,
  fans, compressors) a six-pulse drive's share, an IGBT rectifier (a UPS input) very little.
  Harmonic currents of different loads add in quadrature. A meter's THD(I) is the harmonic
  current of the loads downstream of it over its own current; its neutral current is the
  triplen share of the single-phase loads (IT and small power) downstream.
- **Harmonic voltage.** A bus's THD(V) is its island's harmonic current over the sources'
  rated current, times the sources' harmonic impedance (the 5th harmonic dominates:
  transformer `vk` x 5, a genset's subtransient reactance x 5, an inverter's small output
  impedance), plus the grid's background distortion on utility islands.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

GRID_BACKGROUND_THDV = 0.01
GENSET_DROOP = 0.03
GENSET_XD_SUBTRANSIENT = 0.15
INVERTER_Z_PU = 0.02
HARMONIC_ORDER = 5
TRIPLEN_SHARE = 0.6
"""Share of a single-phase load's harmonic current at triplen orders (3rd, 9th), which add
in the neutral."""

THD_IT_FULL, THD_IT_LIGHT = 0.05, 0.15
"""Current THD of IT power supplies with power factor correction at full and at no load."""
THD_DRIVE = 0.35
THD_RECTIFIER = 0.03
THD_GENERAL = 0.15


def load_thd(kind: str, fraction: float) -> float:
    """Current THD of a load of a kind (`it`, `drive`, `rectifier`, `general`) running at a
    fraction of its rating."""
    if kind == "it":
        f = min(max(fraction, 0.0), 1.0)
        return THD_IT_FULL + (THD_IT_LIGHT - THD_IT_FULL) * (1 - f)
    if kind == "drive":
        return THD_DRIVE
    if kind == "rectifier":
        return THD_RECTIFIER
    return THD_GENERAL


@dataclass(frozen=True, slots=True)
class Source:
    """An ext grid of the load flow as a source of the power-quality model."""

    kind: str
    """`utility`, `genset` or `ups`."""
    bus: int
    rated_a: float
    z_h_pu: float
    """Harmonic impedance at the dominant order, per unit of its rating."""


class Islands:
    """Union-find over buses, rebuilt only when switch or source states change."""

    def __init__(
        self,
        n_buses: int,
        branches: Sequence[tuple[int, int]],
        switched: Mapping[int, tuple[int, int]],
    ) -> None:
        """`branches` are always-joined bus pairs (lines without a switch, transformers);
        `switched` maps a switch index to the bus pair it joins when closed."""
        self.n = n_buses
        self.branches = list(branches)
        self.switched = dict(switched)
        self._key: bytes | None = None
        self.root: npt.NDArray[np.int64] = np.arange(n_buses, dtype=np.int64)

    def update(self, switch: npt.NDArray[np.bool_]) -> npt.NDArray[np.int64]:
        key = switch.tobytes()
        if key == self._key:
            return self.root
        parent = list(range(self.n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for a, b in self.branches:
            union(a, b)
        for index, (a, b) in self.switched.items():
            if switch[index]:
                union(a, b)
        self.root = np.array([find(i) for i in range(self.n)], dtype=np.int64)
        self._key = key
        return self.root


def island_frequency(
    sources: Mapping[str, Source],
    live: Mapping[str, bool],
    power_w: Mapping[str, float],
    rated_w: Mapping[str, float],
    root: npt.NDArray[np.int64],
    grid_hz: float,
    nominal_hz: float,
    ups_input_bus: Mapping[str, int],
    ups_synchronised: Mapping[str, bool],
) -> dict[int, float]:
    """Frequency of each energised island, by the island's root bus."""
    freq: dict[int, list[float]] = {}
    for name, src in sources.items():
        if not live.get(name) or src.kind == "ups":
            continue
        if src.kind == "utility":
            f = grid_hz
        else:
            load = power_w.get(name, 0.0) / max(rated_w.get(name, 1.0), 1.0)
            f = nominal_hz * (1 - GENSET_DROOP * (load - 0.5))
        freq.setdefault(int(root[src.bus]), []).append(f)
    upstream = {k: sum(v) / len(v) for k, v in freq.items()}
    for name, src in sorted(sources.items()):
        if not live.get(name) or src.kind != "ups":
            continue
        f = nominal_hz
        if ups_synchronised.get(name):
            f = upstream.get(int(root[ups_input_bus[name]]), nominal_hz)
        freq.setdefault(int(root[src.bus]), []).append(f)
    return {k: sum(v) / len(v) for k, v in freq.items()}


def island_thdv(
    sources: Mapping[str, Source],
    live: Mapping[str, bool],
    root: npt.NDArray[np.int64],
    harmonic_a: Mapping[int, float],
) -> dict[int, float]:
    """THD(V) of each energised island: its harmonic current (A, by island root) over its
    sources' rating, times their harmonic impedance, plus grid background on utility."""
    rated: dict[int, float] = {}
    z: dict[int, float] = {}
    utility: set[int] = set()
    for name, src in sources.items():
        if not live.get(name):
            continue
        island = int(root[src.bus])
        rated[island] = rated.get(island, 0.0) + src.rated_a
        # Parallel sources: the harmonic impedance of the island is the rating-weighted mean.
        z[island] = z.get(island, 0.0) + src.z_h_pu * src.rated_a
        if src.kind == "utility":
            utility.add(island)
    out: dict[int, float] = {}
    for island, amps in rated.items():
        zh = z[island] / amps if amps > 0 else 0.0
        thd = harmonic_a.get(island, 0.0) / amps * zh if amps > 0 else 0.0
        if island in utility:
            thd = math.hypot(thd, GRID_BACKGROUND_THDV)
        out[island] = thd
    return out
