"""Record a slice trajectory for the web app's replay mode and its tests.

    uv run python apps/web/fixtures/record.py data/graphene \
        apps/web/public/fixtures/dh01-fcu-trip.json

Runs the Phase 1 slice with DH01 at 30 % IT load, lets it settle, trips FCU1, clears and
resets it, and writes every frame (the true state of the DH01 assets and the chilled-water
supply, their points, and the active faults). Needs the slice FMU (ADR-0002).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from gws_runtime.gate import DT, IT_FRACTION, SLICE
from gws_runtime.lifecycle import Session
from gws_world_model.importers.graphene import Sources, build

FCU = "FCU/L1_FCU1"
KEEP = (FCU, "~CCU-001", "~CB-001", "~IT-DH01", "room:DH01", "Chiller/R_C1", "Chiller/R_CP9")
SETTLE_S, TRIPPED_S, RECOVER_S = 600.0, 900.0, 1200.0


def _frame(frame: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    points = {
        path: p
        for path, p in frame["points"].items()
        if p["quality"] == "good" and any(k in path for k in ("FCU1", "CCU-001", "CB-001", "DH01"))
    }
    return {
        "t": frame["t"],
        "step": frame["step"],
        "state": {k: v for k, v in frame["state"].items() if k in KEEP},
        "points": points,
        "faults": frame["faults"],
        "events": events,
    }


def _parameters(doc: Any, asset: Any) -> dict[str, Any]:
    """The asset's numeric parameters: its own values over its type's defaults."""
    spec = doc.component_types[asset.type].parameters
    values = {k: p.default for k, p in spec.items()} | dict(asset.parameters)
    return {
        k: v for k, v in values.items() if isinstance(v, int | float) and not isinstance(v, bool)
    }


def _world(doc: Any, room_id: str) -> dict[str, Any]:
    """The hall as the World Model has it: its room, floor and the assets placed in it."""
    site = doc.site
    room = site.rooms[room_id]
    floor = next(f for f in site.floors if f.id == room.floor)
    assets = [
        {
            "id": a.id,
            "name": a.name,
            "type": a.type,
            "x": a.location.x,
            "y": a.location.y,
            "in_scope": a.id in SLICE,
            "parameters": _parameters(doc, a),
        }
        for a in sorted(doc.assets.values(), key=lambda a: a.id)
        if a.location is not None and a.location.room == room_id
    ]
    return {
        "room": room.model_dump(mode="json"),
        "floor": floor.model_dump(mode="json"),
        "assets": assets,
    }


def record(data: Path) -> dict[str, Any]:
    doc = build(Sources.read(data))
    session = Session(doc, SLICE, dt=DT, revision=1)
    session.apply("conditions", {"changes": {"it_fraction": {"DH01": IT_FRACTION}}})
    frames: list[dict[str, Any]] = []

    def run(seconds: float, events: list[dict[str, Any]] | None = None) -> None:
        pending = list(events or [])
        for _ in range(int(seconds / DT)):
            frames.append(_frame(session.step(1).to_json(), pending))
            pending = []

    run(SETTLE_S)
    fault = session.apply("fault", {"target": FCU, "mode": "trip"})
    run(TRIPPED_S, [{"kind": "fault", "target": FCU, "mode": "trip"}])
    session.apply("clear", {"id": fault.id})
    session.apply("reset", {"target": FCU})
    run(RECOVER_S, [{"kind": "clear", "target": FCU}, {"kind": "reset", "target": FCU}])
    return {
        "world": _world(doc, "DH01"),
        "scenario": "FCU1 trips in DH01 at 30 % IT load, then is cleared and reset",
        "dt": DT,
        "revision": 1,
        "scope": list(SLICE),
        "frames": frames,
    }


if __name__ == "__main__":
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record(Path(sys.argv[1])), separators=(",", ":")))
    print(out, out.stat().st_size)
