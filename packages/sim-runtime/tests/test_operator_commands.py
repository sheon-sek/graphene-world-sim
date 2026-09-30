"""Operator commands on the chiller through its command points. Needs the slice FMU (cached or
compiled with OpenModelica); skipped otherwise, like the gate."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gws_runtime.compiler import CACHE, plan
from gws_runtime.gate import SLICE
from gws_runtime.lifecycle import Session
from gws_world_model.importers.graphene import Sources, build

ROOT = Path(__file__).resolve().parents[3]
DOC = build(Sources.read(ROOT / "data" / "graphene"))
CH = "Chiller System Control/Chillers/CH-001"
C1 = "Chiller/R_C1"


def _available() -> bool:
    if all((CACHE / f"{p.name}.fmu").exists() for p in plan(DOC, SLICE).partitions):
        return True
    return shutil.which("docker") is not None and "GWS_OMLIB" in os.environ


@pytest.mark.skipif(not _available(), reason="needs OpenModelica or a cached slice FMU")
def test_stop_holds_the_chiller_off_in_manual_and_a_disabled_chiller_stays_off() -> None:
    session = Session(DOC, SLICE, dt=5.0)
    session.apply("conditions", {"changes": {"it_fraction": {"DH01": 0.3}}})
    frame = session.step(12)
    assert frame.points[f"{C1}/On_Off"].value == 1

    session.apply("command", {"target": f"{CH}/Commands/Stop", "signal": None, "value": True})
    frame = session.step(12)  # the staging controller keeps asking; manual wins
    assert frame.points[f"{C1}/Auto_Manual"].value == 0
    assert frame.points[f"{C1}/On_Off"].value == 0

    session.apply("command", {"target": f"{CH}/Commands/Start", "signal": None, "value": True})
    assert session.step(12).points[f"{C1}/On_Off"].value == 1

    session.apply("command", {"target": f"{CH}/Enabled", "signal": None, "value": False})
    frame = session.step(12)
    assert frame.points[f"{CH}/Enabled"].value is False
    assert frame.points[f"{C1}/On_Off"].value == 0
    session.close()
