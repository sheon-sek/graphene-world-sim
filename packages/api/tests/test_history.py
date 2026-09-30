from __future__ import annotations

from typing import Any

import pytest

from gws_api.history import History

ALARMS = {"FCU/1/Fault": "FCU/1"}


def _frame(step: int, *, fault: bool = False, temp: float = 298.0, flow: float = 2.0) -> Any:
    return {
        "t": float(step),
        "step": step,
        "state": {"FCU/1": {"TSupAir": temp, "mAir_flow": flow, "tripped": fault}},
        "points": {"FCU/1/Fault": {"value": fault, "quality": "good"}},
    }


def _context(t: float) -> dict[str, Any]:
    return {"kind": "fault", "t": t}


def _record(h: History, frame: Any) -> None:
    h.record(frame, lambda: ALARMS, _context)


def test_alarm_is_raised_and_cleared_with_its_context() -> None:
    h = History()
    for step in range(6):
        _record(h, _frame(step, fault=2 <= step < 4))
    [alarm] = h.alarms
    assert (alarm.point, alarm.asset, alarm.raised_t, alarm.cleared_t) == (
        "FCU/1/Fault",
        "FCU/1",
        2.0,
        4.0,
    )
    assert alarm.context == {"kind": "fault", "t": 2.0}


def test_series_are_thinned_and_the_ring_keeps_the_newest() -> None:
    h = History(capacity=50)
    for step in range(120):
        _record(h, _frame(step, temp=290.0 + step))
    assert h.span == (70.0, 119.0)
    out = h.series([("state", "FCU/1", "TSupAir"), ("point", "FCU/1/Fault", "")], max_points=10)
    assert len(out["t"]) == 10 and out["t"][0] == 70.0 and out["t"][-1] == 119.0
    assert out["series"][0]["values"][-1] == pytest.approx(409.0)
    assert out["series"][1]["quality"][0] == "good"


def test_a_repeated_step_is_ignored_and_going_back_starts_a_new_branch() -> None:
    h = History()
    for step in range(5):
        _record(h, _frame(step))
    _record(h, _frame(4))
    assert h.count == 5
    _record(h, _frame(2))
    assert h.count == 1 and h.span == (2.0, 2.0)


def test_propagation_orders_assets_by_first_move_beyond_threshold() -> None:
    h = History()
    for step in range(10):
        # the flow drops at step 3; the temperature follows from step 6
        f = _frame(step, flow=0.5 if step >= 3 else 2.0, temp=298.0 + (2.0 if step >= 6 else 0.1))
        f["state"]["room:DH01"] = {"TAir": 298.0 + (1.0 if step >= 8 else 0.0)}
        _record(h, f)
    units = {("FCU/1", "TSupAir"): "K", ("FCU/1", "mAir_flow"): "kg/s", ("room:DH01", "TAir"): "K"}
    order = h.propagation(1.0, units)
    assert [(e["asset"], e["t"]) for e in order] == [("FCU/1", 3.0), ("room:DH01", 8.0)]
    signals = [(c["signal"], c["t"]) for c in order[0]["changes"]]
    assert signals == [("mAir_flow", 3.0), ("TSupAir", 6.0)]
