"""The ComponentType library: one reviewed JSON file per type in `types/`.

The files carry no point templates; an importer adds those from the Ignition project that
exposes the type, because the points are Ignition's contract, not the equipment's physics.
"""

from __future__ import annotations

import re
from pathlib import Path

from gws_world_model.model import ComponentType

TYPES_DIR = Path(__file__).parent / "types"


def slug(type_id: str) -> str:
    """File name stem for a type id: `Production/GPM96` -> `production-gpm96`."""
    return re.sub(r"[^a-z0-9]+", "-", type_id.lower()).strip("-")


def load(directory: Path = TYPES_DIR) -> dict[str, ComponentType]:
    """Every type in the library, by id. A file whose name is not its id's slug is an error."""
    types: dict[str, ComponentType] = {}
    for path in sorted(directory.glob("*.json")):
        ctype = ComponentType.model_validate_json(path.read_bytes())
        if path.stem != slug(ctype.id):
            raise ValueError(f"{path.name} holds type {ctype.id!r}; expected {slug(ctype.id)}.json")
        if ctype.id in types:
            raise ValueError(f"type {ctype.id!r} is defined twice")
        types[ctype.id] = ctype
    return types
