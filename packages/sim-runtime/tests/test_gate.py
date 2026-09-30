"""The Phase 3 gate on the slice. Needs OpenModelica in Docker and the Modelica libraries
(`GWS_OMLIB`, ADR-0002) unless the FMU cache already holds the slice; skipped otherwise."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gws_runtime.compiler import CACHE, plan
from gws_runtime.gate import SLICE, Gate
from gws_world_model.importers.graphene import Sources, build

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "graphene"


def _available() -> bool:
    doc = build(Sources.read(DATA))
    if all((CACHE / f"{p.name}.fmu").exists() for p in plan(doc, SLICE).partitions):
        return True
    return shutil.which("docker") is not None and "GWS_OMLIB" in os.environ


@pytest.mark.skipif(not _available(), reason="needs OpenModelica or a cached slice FMU")
def test_phase3_gate_passes() -> None:
    result = Gate(DATA).run()
    failed = [
        f"{s['name']}: {c['name']} ({c['detail']})"
        for s in result["scenarios"]
        for c in s["checks"]
        if not c["passed"]
    ]
    assert failed == []
