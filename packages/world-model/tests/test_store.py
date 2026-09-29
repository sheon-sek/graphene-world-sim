from __future__ import annotations

import pytest

from gws_world_model.changes import ChangeKind, overall
from gws_world_model.model import ChangeClass, WorldModel
from gws_world_model.ops import Delete, OperationError, Put, SetConditions
from gws_world_model.store import Conflict, Invalid, NotFound, SqliteStore


@pytest.fixture
def store(world: WorldModel) -> SqliteStore:
    s = SqliteStore()
    s.create_revision(world, "initial")
    return s


def test_draft_edit_diff_commit(store: SqliteStore, world: WorldModel) -> None:
    draft = store.create_draft()
    asset = world.assets["C1"].model_dump() | {"parameters": {"capacity": 800.0}}
    store.add_operations(draft.id, [Put(collection="assets", value=asset)])

    changes = store.draft_diff(draft.id)
    assert [(c.collection, c.key, c.kind, c.fields) for c in changes] == [
        ("assets", "C1", ChangeKind.MODIFIED, ("parameters.capacity",))
    ]
    assert changes[0].change_class is ChangeClass.WARM

    info = store.commit(draft.id, "derate chiller")
    assert (info.number, info.parent) == (2, 1)
    assert store.get(2).assets["C1"].parameters == {"capacity": 800.0}
    assert store.get(1) == world  # revisions are immutable
    with pytest.raises(NotFound):
        store.draft(draft.id)


def test_commit_refuses_invalid_document(store: SqliteStore) -> None:
    draft = store.create_draft()
    store.add_operations(draft.id, [Delete(collection="assets", key="C1")])
    with pytest.raises(Invalid) as exc:
        store.commit(draft.id, "remove chiller")
    assert any("dangling" in i.message for i in exc.value.issues)
    assert store.head() == 1


def test_commit_refuses_stale_base(store: SqliteStore) -> None:
    first, second = store.create_draft(), store.create_draft()
    store.add_operations(
        first.id, [SetConditions.model_validate({"value": {"utility_available": False}})]
    )
    store.commit(first.id, "utility lost")
    store.add_operations(second.id, [SetConditions.model_validate({"value": {}})])
    with pytest.raises(Conflict):
        store.commit(second.id, "stale")


def test_operation_on_missing_key_is_rejected(store: SqliteStore) -> None:
    draft = store.create_draft()
    with pytest.raises(OperationError, match="does not exist"):
        store.add_operations(draft.id, [Delete(collection="assets", key="nope")])
    assert store.draft(draft.id).operations == ()


def test_change_classes(store: SqliteStore, world: WorldModel) -> None:
    draft = store.create_draft()
    pump = world.assets["P1"].model_dump()
    store.add_operations(
        draft.id,
        [
            Put(collection="assets", value=pump | {"parameters": {"speed_setpoint": 0.5}}),
            Put(collection="assets", value=pump | {"id": "P2", "name": "P2"}),
            SetConditions.model_validate({"value": {"utility_available": False}}),
        ],
    )
    changes = {(c.collection, c.key): c.change_class for c in store.draft_diff(draft.id)}
    assert changes == {
        ("assets", "P1"): ChangeClass.LIVE,
        ("assets", "P2"): ChangeClass.STRUCTURAL,
        ("conditions", "conditions"): ChangeClass.LIVE,
    }
    assert overall(store.draft_diff(draft.id)) is ChangeClass.STRUCTURAL
