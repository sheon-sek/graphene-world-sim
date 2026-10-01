"""A chiller's refrigerant-side points follow what its model computes (#71)."""

from __future__ import annotations

from gws_runtime.behaviours import _cond_k, _motor_current

HEALTHY = {
    "running": True,
    "PLR": 0.1,
    "TChwLvg": 287.0,
    "TCwLvg": 302.6,
    "QEva": 356e3,
    "P": 63.5e3,
}


def test_a_healthy_chiller_keeps_its_condensing_approach() -> None:
    assert _cond_k(HEALTHY) == 302.6 + 0.5 + 2.5 * 0.1
    assert _motor_current(HEALTHY) == 0.1


def test_more_power_for_the_same_cooling_raises_head_and_current() -> None:
    fouled = HEALTHY | {"P": 105e3}
    assert _cond_k(fouled) > _cond_k(HEALTHY) + 5.0
    assert _motor_current(fouled) > 1.3 * _motor_current(HEALTHY)


def test_without_an_electrical_model_the_approach_holds() -> None:
    alone = {k: v for k, v in HEALTHY.items() if k != "P"}
    assert _cond_k(alone) == _cond_k(HEALTHY)
