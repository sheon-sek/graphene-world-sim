from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gws_world_model.model import Asset, ParameterSpec, WorldModel
from gws_world_model.schema import render

ROOT = Path(__file__).resolve().parents[3]


def test_document_round_trips_through_json(world: WorldModel) -> None:
    again = WorldModel.model_validate_json(world.canonical_json())
    assert again == world
    assert again.content_hash() == world.content_hash()


def test_published_schema_is_current() -> None:
    published = ROOT / "schemas" / "world-model.schema.json"
    assert published.read_text(encoding="utf-8") == render(), (
        "run: uv run python -m gws_world_model.schema schemas/world-model.schema.json"
    )


def test_schema_accepts_a_serialised_document(world: WorldModel) -> None:
    schema = json.loads(render())
    assert set(schema["required"]) <= set(json.loads(world.canonical_json()))


def test_key_must_match_id(world: WorldModel) -> None:
    data = json.loads(world.canonical_json())
    data["assets"]["P1"]["id"] = "P2"
    with pytest.raises(ValidationError, match="has id"):
        WorldModel.model_validate(data)


def test_parameter_default_must_respect_limits() -> None:
    with pytest.raises(ValidationError, match="above max"):
        ParameterSpec(default=2.0, max=1.0)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Asset.model_validate({"id": "a", "type": "t", "name": "a", "colour": "red"})
