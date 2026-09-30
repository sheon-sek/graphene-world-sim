from __future__ import annotations

import logging

import pytest

from gws_runtime.units import Converter, UnitError, compatible, convert, lookup, si_unit

# Every unit revision 1 uses on a point (`engUnit`), an instrument or a parameter.
REVISION_1_UNITS = (
    "%", "°C", "V", "kW", "kWh", "A", "Mbps", "W", "count", "kVAR", "kVA", "%RH", "Hz", "kPa",
    "L/s", "ms", "days", "hrs", "m³/h", "m", "L/min", "tCO₂e", "h", "bar", "Min", "RPM", "kW/RT",
    "MW", "L", "times/day", "s", "tCO₂e/day", "m²", "tCO₂e/h", "MWh", "m/s", "hPa", "mm", "°",
    "L/kWh", "L/day", "day", "kgCO₂e/L", "kgCO₂e/kWh", "kgCO₂e/m²", "1", "kg/s", "degC", "pu",
    "m3", "Pa", "K",
)  # fmt: skip


def test_every_unit_revision_1_uses_is_known() -> None:
    assert [u for u in REVISION_1_UNITS if lookup(u) is None] == []


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (293.15, "K", "°C", 20.0),
        (100.0, "degC", "degF", 212.0),
        (32.0, "°F", "K", 273.15),
        (2500.0, "kW", "MW", 2.5),
        (1.0, "MWh", "kWh", 1000.0),
        (3.6e6, "J", "kWh", 1.0),
        (1.0, "bar", "kPa", 100.0),
        (1.0, "psi", "Pa", 6894.757293168),
        (1.0, "kg/s", "L/s", 1.0),
        (1.0, "L/s", "L/min", 60.0),
        (3.6, "m³/h", "L/s", 1.0),
        (1.0, "L/min", "L/day", 1440.0),
        (0.45, "1", "%", 45.0),
        (0.6, "pu", "%RH", 60.0),
        (90.0, "min", "h", 1.5),
        (2.0, "days", "hrs", 48.0),
        (1.0, "tCO₂e/h", "tCO₂e/day", 24.0),
        (1.0, "kVA", "W", 1000.0),
        (60.0, "RPM", "Hz", 1.0),
    ],
)
def test_converts_between_units_of_one_dimension(
    value: float, source: str, target: str, expected: float
) -> None:
    assert convert(value, source, target) == pytest.approx(expected)


def test_a_ratio_of_one_dimension_is_dimensionless() -> None:
    # A chiller at 0.6 kW of power per kW of cooling is at about 2.11 kW/RT.
    assert convert(0.6, "1", "kW/RT") == pytest.approx(0.6 * 3.5168528)
    assert si_unit("kW/RT") == "1"
    assert compatible("L/kWh", "m3/J")


def test_a_difference_ignores_the_offset() -> None:
    assert convert(2.0, "K", "degF", delta=True) == pytest.approx(3.6)
    assert convert(0.1, "1", "%", delta=True) == pytest.approx(10.0)


def test_a_missing_source_unit_is_the_si_base_and_a_missing_target_keeps_the_value() -> None:
    assert convert(280.15, None, "°C") == pytest.approx(7.0)
    assert convert(5.0, "kW", None) == 5.0


def test_incompatible_or_unknown_units_raise() -> None:
    with pytest.raises(UnitError):
        convert(1.0, "kW", "kPa")
    with pytest.raises(UnitError):
        convert(1.0, "furlong", "m")


def test_converter_passes_unknown_units_through_and_reports_them_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    converter = Converter()
    with caplog.at_level(logging.WARNING):
        assert converter.convert(3.0, "furlong", "m") == 3.0
        assert converter.convert(4.0, "furlong", "m") == 4.0
        assert converter.convert(1.0, "kW", "kPa") == 1.0
    assert converter.unknown == {"furlong"}
    assert converter.incompatible == {("kW", "kPa")}
    assert len(caplog.records) == 2
