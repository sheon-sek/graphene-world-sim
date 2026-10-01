"""Unit conversion between the SI units models compute in and the units points display.

A unit is a dimension, a scale and an offset: `value_in_base = value * scale + offset`. Only
temperatures have an offset; a *delta* conversion (a bias, a drift rate, a noise amplitude)
ignores it, so a 2 K bias is 2 degC and 3.6 degF. A value that is itself a temperature
difference (an approach) is in `dK`, which always converts as a delta.

Choices worth knowing:

- Mass and volume flow share one dimension, `kg/s`, through the density of water
  (1000 kg/m3: 1 L/s = 1 kg/s). Every flow in the site data is water except the diesel
  flowmeters, which convert only between volumetric units (L/min, L/day), where the density
  cancels.
- `W`, `VA` and `var` share one dimension (all are volt-amperes), so kVA and kVAR points
  convert from a model's W-scaled value like kW points.
- Fractions: `1`, `pu`, `count` and `times` are dimensionless with scale 1; `%` and `%RH` are
  0.01 of it. A model computes fractions; a `%` point shows 100 times that.
- A unit written `a/b` whose parts are both known is a compound unit (`m/s`, `L/kWh`,
  `kgCO2e/kWh`, `kW/RT`); a ratio of one dimension (`kW/RT`) is dimensionless.
- A unit this module does not know passes values through unchanged. `Converter` reports each
  unknown unit and each incompatible pair once, through logging and its `unknown` and
  `incompatible` sets.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)

WATER_DENSITY_KG_M3 = 1000.0


@dataclass(frozen=True, slots=True)
class Unit:
    dimension: str
    """Name of the SI base unit of the dimension (`K`, `W`, `kg/s`, `1`)."""
    scale: float
    offset: float = 0.0
    delta: bool = False
    """A difference of this unit (`dK`): it converts without the offset."""


def _table() -> dict[str, Unit]:
    units: dict[str, Unit] = {}

    def add(dimension: str, *entries: tuple[str, float]) -> None:
        for name, scale in entries:
            units[name] = Unit(dimension, scale)

    units["K"] = Unit("K", 1.0)
    units["dK"] = Unit("K", 1.0, delta=True)
    units["degC"] = Unit("K", 1.0, 273.15)
    units["degF"] = Unit("K", 5 / 9, 459.67 * 5 / 9)
    add("1", ("1", 1.0), ("pu", 1.0), ("fraction", 1.0), ("count", 1.0), ("times", 1.0))
    add("1", ("%", 0.01), ("%RH", 0.01))
    add(
        "W",
        ("W", 1.0),
        ("kW", 1e3),
        ("MW", 1e6),
        ("VA", 1.0),
        ("kVA", 1e3),
        ("MVA", 1e6),
        ("var", 1.0),
        ("VAR", 1.0),
        ("kvar", 1e3),
        ("kVAR", 1e3),
        ("Mvar", 1e6),
        ("MVAR", 1e6),
        ("RT", 3516.8528),
    )
    add(
        "J",
        ("J", 1.0),
        ("kJ", 1e3),
        ("MJ", 1e6),
        ("GJ", 1e9),
        ("Wh", 3600.0),
        ("kWh", 3.6e6),
        ("MWh", 3.6e9),
        ("GWh", 3.6e12),
    )
    add(
        "Pa",
        ("Pa", 1.0),
        ("hPa", 100.0),
        ("kPa", 1e3),
        ("MPa", 1e6),
        ("mbar", 100.0),
        ("bar", 1e5),
        ("psi", 6894.757293168),
    )
    litre = 1e-3 * WATER_DENSITY_KG_M3  # kg of water per litre
    cubic = WATER_DENSITY_KG_M3  # kg of water per m3
    add(
        "kg/s",
        ("kg/s", 1.0),
        ("kg/h", 1 / 3600),
        ("L/s", litre),
        ("L/min", litre / 60),
        ("L/h", litre / 3600),
        ("L/day", litre / 86400),
        ("m3/s", cubic),
        ("m3/h", cubic / 3600),
    )
    add("m3", ("m3", 1.0), ("L", 1e-3))
    add("kg", ("kg", 1.0), ("g", 1e-3), ("t", 1e3))
    add("kgCO2e", ("kgCO2e", 1.0), ("tCO2e", 1e3))
    add(
        "s",
        ("s", 1.0),
        ("ms", 1e-3),
        ("min", 60.0),
        ("Min", 60.0),
        ("h", 3600.0),
        ("hr", 3600.0),
        ("hrs", 3600.0),
        ("day", 86400.0),
        ("days", 86400.0),
        ("d", 86400.0),
    )
    add("Hz", ("Hz", 1.0), ("kHz", 1e3), ("RPM", 1 / 60), ("rpm", 1 / 60))
    add("V", ("V", 1.0), ("mV", 1e-3), ("kV", 1e3))
    add("A", ("A", 1.0), ("mA", 1e-3), ("kA", 1e3))
    add("m", ("m", 1.0), ("mm", 1e-3), ("cm", 1e-2), ("km", 1e3))
    add("m2", ("m2", 1.0))
    add("rad", ("rad", 1.0), ("deg", math.pi / 180))
    add("bit/s", ("bps", 1.0), ("kbps", 1e3), ("Mbps", 1e6), ("Gbps", 1e9))
    return units


_UNITS = _table()
_ALIASES = {"°C": "degC", "°F": "degF", "°": "deg"}


def _normalise(unit: str) -> str:
    unit = unit.strip()
    unit = _ALIASES.get(unit, unit)
    return unit.replace("³", "3").replace("²", "2").replace("₂", "2")


def lookup(unit: str | None) -> Unit | None:
    """The unit, or None when it is None or not known."""
    if unit is None:
        return None
    name = _normalise(unit)
    if (found := _UNITS.get(name)) is not None:
        return found
    top, sep, bottom = name.partition("/")
    if sep and "/" not in bottom:
        a, b = _UNITS.get(top), _UNITS.get(bottom)
        if a is not None and b is not None and not a.offset and not b.offset:
            dim = "1" if a.dimension == b.dimension else f"{a.dimension}/{b.dimension}"
            return Unit(dim, a.scale / b.scale)
    return None


def si_unit(unit: str | None) -> str | None:
    """The SI base unit of a unit's dimension (`kW` -> `W`), or None when unknown."""
    found = lookup(unit)
    return None if found is None else found.dimension


class UnitError(ValueError):
    """A unit is unknown, or two units measure different dimensions."""


def convert(value: float, source: str | None, target: str | None, *, delta: bool = False) -> float:
    """`value` in `source` expressed in `target`.

    A None target leaves the value unchanged. A None source means the SI base unit of the
    target's dimension (what a model computes). `delta` converts a difference (no offset).
    Raises UnitError for an unknown unit or a dimension mismatch.
    """
    if target is None or source == target:
        return value
    to = lookup(target)
    if to is None:
        raise UnitError(f"unknown unit {target!r}")
    if source is None:
        frm = Unit(to.dimension, 1.0)
    else:
        found = lookup(source)
        if found is None:
            raise UnitError(f"unknown unit {source!r}")
        frm = found
    if frm.dimension != to.dimension:
        raise UnitError(f"cannot convert {source!r} to {target!r}")
    if delta or frm.delta or to.delta:
        return value * frm.scale / to.scale
    return (value * frm.scale + frm.offset - to.offset) / to.scale


def compatible(a: str | None, b: str | None) -> bool:
    """True when both units are known and measure the same dimension."""
    ua, ub = lookup(a), lookup(b)
    return ua is not None and ub is not None and ua.dimension == ub.dimension


class Converter:
    """`convert` that never raises: an unknown unit or an incompatible pair passes the value
    through unchanged and is reported once."""

    def __init__(self) -> None:
        self.unknown: set[str] = set()
        self.incompatible: set[tuple[str, str]] = set()

    def convert(
        self, value: float, source: str | None, target: str | None, *, delta: bool = False
    ) -> float:
        try:
            return convert(value, source, target, delta=delta)
        except UnitError:
            self._report(source, target)
            return value

    def _report(self, source: str | None, target: str | None) -> None:
        for unit in (source, target):
            if unit is not None and lookup(unit) is None:
                if unit not in self.unknown:
                    self.unknown.add(unit)
                    log.warning("unit %r is not known; values pass through unchanged", unit)
                return
        pair = (str(source), str(target))
        if pair not in self.incompatible:
            self.incompatible.add(pair)
            log.warning("cannot convert %r to %r; values pass through unchanged", *pair)
