"""Starting the server on a store kept from an older checkout brings in the current import."""

from __future__ import annotations

from pathlib import Path

from gws_api.serve import IMPORTER, seed
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.store import SqliteStore

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "graphene"


def _older_import() -> SqliteStore:
    doc = build(Sources.read(DATA))
    point = next(iter(doc.point_bindings))
    older = doc.model_copy(
        update={"point_bindings": {k: v for k, v in doc.point_bindings.items() if k != point}}
    )
    store = SqliteStore()
    store.create_revision(older, "import", IMPORTER)
    return store


def test_an_empty_store_gets_the_import_and_a_current_one_is_left_alone() -> None:
    store = SqliteStore()
    seed(store, DATA)
    assert store.head() == 1
    assert "current import" in seed(store, DATA) and store.head() == 1


def test_an_older_import_gets_the_current_import_on_top() -> None:
    store = _older_import()
    seed(store, DATA)
    assert store.head() == 2
    assert store.get(2).content_hash() == build(Sources.read(DATA)).content_hash()


def test_edited_revisions_stay_the_head_unless_asked() -> None:
    store = _older_import()
    store.create_revision(store.get(1), "my edit", "sheon")
    assert seed(store, DATA).startswith("Warning") and store.head() == 2
    seed(store, DATA, reimport=True)
    assert store.head() == 3
