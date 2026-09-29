"""Publish the World Model JSON Schema.

Usage: python -m gws_world_model.schema [output-path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from gws_world_model.model import WorldModel


def json_schema() -> dict[str, Any]:
    schema = WorldModel.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = (
        "https://github.com/sheon-sek/graphene-world-sim/schemas/world-model.schema.json"
    )
    return schema


def render() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if out is None:
        sys.stdout.write(render())
    else:
        out.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()
