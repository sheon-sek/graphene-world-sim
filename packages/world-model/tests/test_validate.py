from __future__ import annotations

import json

from gws_world_model.model import WorldModel
from gws_world_model.validate import Severity, validate


def _edit(world: WorldModel, path: list[str], value: object) -> WorldModel:
    data = json.loads(world.canonical_json())
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return WorldModel.model_validate(data)


def _errors(doc: WorldModel) -> list[tuple[str, str]]:
    return [(i.path, i.message) for i in validate(doc) if i.severity is Severity.ERROR]


def test_valid_world_has_no_issues(world: WorldModel) -> None:
    assert validate(world) == []


def test_port_domain_and_direction(world: WorldModel) -> None:
    doc = _edit(world, ["connections", "chw:P1->C1", "source", "port"], "power_in")
    assert _errors(doc) == [
        ("connections[chw:P1->C1].source.port", "port 'power_in' carries power, not chw"),
        ("connections[chw:P1->C1].source.port", "port 'power_in' faces in; cannot be a source"),
    ]


def test_unknown_port_and_dangling_node(world: WorldModel) -> None:
    doc = _edit(world, ["connections", "chw:P1->C1", "target"], {"node": "C9", "port": "chw_in"})
    assert _errors(doc) == [
        ("connections[chw:P1->C1].target.node", "unknown asset 'C9' (dangling connection)")
    ]


def test_parameter_units_and_limits(world: WorldModel) -> None:
    doc = _edit(world, ["assets", "P1", "parameters"], {"speed_setpoint": 1.5, "colour": "red"})
    assert _errors(doc) == [
        ("assets[P1].parameters.speed_setpoint", "1.5 is above the maximum 1.0"),
        ("assets[P1].parameters.colour", "Pump declares no 'colour'"),
    ]


def test_point_binding_to_missing_asset(world: WorldModel) -> None:
    doc = _edit(world, ["point_bindings", "Chiller/C1/Power", "source", "asset"], "C7")
    assert _errors(doc) == [("point_bindings[Chiller/C1/Power].source.asset", "unknown asset 'C7'")]
