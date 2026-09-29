"""World Model endpoints: read revisions, edit drafts, validate, diff and apply.

Reads take an optional `revision` and default to the head. Edits never touch a revision: they
go into a draft as operations, and applying the draft commits a new revision if it validates
and its base is still the head.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from gws_world_model.changes import Change, overall
from gws_world_model.model import (
    COLLECTIONS,
    Asset,
    ChangeClass,
    ComponentType,
    Conditions,
    Connection,
    ControlBinding,
    Domain,
    Instrument,
    PointBinding,
    Site,
    WorldModel,
)
from gws_world_model.ops import Operation, OperationError
from gws_world_model.store import Conflict, Draft, Invalid, NotFound, RevisionInfo, SqliteStore
from gws_world_model.validate import Issue, Severity

router = APIRouter(prefix="/world-model", tags=["world-model"])


def get_store(request: Request) -> SqliteStore:
    store: SqliteStore = request.app.state.store
    return store


Store = Annotated[SqliteStore, Depends(get_store)]
RevisionQuery = Annotated[
    int | None, Query(description="Revision number; the head when omitted.", ge=1)
]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class RevisionOut(_Out):
    number: int
    parent: int | None
    created_at: str
    author: str
    message: str
    content_hash: str

    @classmethod
    def of(cls, info: RevisionInfo) -> RevisionOut:
        return cls(**{f: getattr(info, f) for f in cls.model_fields})


class DraftOut(_Out):
    id: str
    base: int
    created_at: str
    updated_at: str
    author: str
    operations: list[Operation]

    @classmethod
    def of(cls, draft: Draft) -> DraftOut:
        return cls(
            id=draft.id,
            base=draft.base,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            author=draft.author,
            operations=list(draft.operations),
        )


class ChangeOut(_Out):
    collection: str
    key: str
    kind: str
    fields: list[str]
    change_class: ChangeClass
    reason: str


class DiffOut(_Out):
    overall: ChangeClass | None
    """The most disruptive change class; null when nothing changed."""
    changes: list[ChangeOut]

    @classmethod
    def of(cls, changes: list[Change]) -> DiffOut:
        return cls(
            overall=overall(changes),
            changes=[
                ChangeOut(
                    collection=c.collection,
                    key=c.key,
                    kind=c.kind,
                    fields=list(c.fields),
                    change_class=c.change_class,
                    reason=c.reason,
                )
                for c in changes
            ],
        )


class IssueOut(_Out):
    severity: Severity
    path: str
    message: str


class ValidationOut(_Out):
    valid: bool
    """True when there are no errors; warnings do not block applying."""
    issues: list[IssueOut]

    @classmethod
    def of(cls, issues: list[Issue]) -> ValidationOut:
        return cls(
            valid=not any(i.severity is Severity.ERROR for i in issues),
            issues=[IssueOut(severity=i.severity, path=i.path, message=i.message) for i in issues],
        )


class Summary(_Out):
    revision: int
    content_hash: str
    site: str
    counts: dict[str, int]


class NewDraft(_Out):
    base: int | None = None
    """Revision to edit; the head when omitted."""
    author: str = "anonymous"


class ApplyDraft(_Out):
    message: str
    author: str | None = None


class Page[T](_Out):
    total: int
    items: list[T]


def _revision(store: SqliteStore, revision: int | None) -> tuple[int, WorldModel]:
    number = store.head() if revision is None else revision
    if number is None:
        raise HTTPException(404, "the store has no revisions")
    try:
        return number, store.get(number)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def _doc(store: SqliteStore, revision: int | None) -> WorldModel:
    return _revision(store, revision)[1]


def _one[T](items: dict[str, T], key: str, what: str) -> T:
    if key not in items:
        raise HTTPException(404, f"no {what} {key!r}")
    return items[key]


def _draft(store: SqliteStore, draft_id: str) -> Draft:
    try:
        return store.draft(draft_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --- revisions -------------------------------------------------------------------------------


@router.get("/summary")
def summary(store: Store, revision: RevisionQuery = None) -> Summary:
    number, doc = _revision(store, revision)
    counts = {name: len(getattr(doc, name)) for name in COLLECTIONS}
    counts["rooms"] = len(doc.site.rooms)
    return Summary(
        revision=number, content_hash=doc.content_hash(), site=doc.site.name, counts=counts
    )


@router.get("/revisions")
def revisions(store: Store) -> list[RevisionOut]:
    return [RevisionOut.of(r) for r in store.revisions()]


@router.get("/revisions/{number}")
def revision(store: Store, number: int) -> RevisionOut:
    try:
        return RevisionOut.of(store.info(number))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/revisions/{number}/document")
def document(store: Store, number: int) -> WorldModel:
    return _doc(store, number)


@router.get("/revisions/{old}/diff/{new}")
def revision_diff(store: Store, old: int, new: int) -> DiffOut:
    try:
        return DiffOut.of(store.diff(old, new))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# --- entities --------------------------------------------------------------------------------


@router.get("/site")
def site(store: Store, revision: RevisionQuery = None) -> Site:
    return _doc(store, revision).site


@router.get("/conditions")
def conditions(store: Store, revision: RevisionQuery = None) -> Conditions:
    return _doc(store, revision).conditions


@router.get("/types")
def types(store: Store, revision: RevisionQuery = None) -> list[ComponentType]:
    return list(_doc(store, revision).component_types.values())


@router.get("/types/{type_id:path}")
def component_type(store: Store, type_id: str, revision: RevisionQuery = None) -> ComponentType:
    return _one(_doc(store, revision).component_types, type_id, "type")


@router.get("/assets")
def assets(
    store: Store,
    revision: RevisionQuery = None,
    type: str | None = None,
    room: str | None = None,
    system: str | None = None,
) -> list[Asset]:
    return [
        a
        for a in _doc(store, revision).assets.values()
        if (type is None or a.type == type)
        and (room is None or a.location.room == room)
        and (system is None or a.system == system)
    ]


@router.get("/assets/{asset_id:path}")
def asset(store: Store, asset_id: str, revision: RevisionQuery = None) -> Asset:
    return _one(_doc(store, revision).assets, asset_id, "asset")


@router.get("/connections")
def connections(
    store: Store,
    revision: RevisionQuery = None,
    node: Annotated[str | None, Query(description="Asset id or `room:<id>` at either end.")] = None,
    domain: Domain | None = None,
) -> list[Connection]:
    return [
        c
        for c in _doc(store, revision).connections.values()
        if (node is None or node in (c.source.node, c.target.node))
        and (domain is None or c.domain is domain)
    ]


@router.get("/connections/{connection_id:path}")
def connection(store: Store, connection_id: str, revision: RevisionQuery = None) -> Connection:
    return _one(_doc(store, revision).connections, connection_id, "connection")


@router.get("/instruments")
def instruments(store: Store, revision: RevisionQuery = None) -> list[Instrument]:
    return list(_doc(store, revision).instruments.values())


@router.get("/control-bindings")
def control_bindings(store: Store, revision: RevisionQuery = None) -> list[ControlBinding]:
    return list(_doc(store, revision).control_bindings.values())


@router.get("/points")
def points(
    store: Store,
    revision: RevisionQuery = None,
    prefix: Annotated[str, Query(description="Export path prefix, e.g. `Chiller/`.")] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> Page[PointBinding]:
    matched = [
        p
        for path, p in sorted(_doc(store, revision).point_bindings.items())
        if path.startswith(prefix)
    ]
    return Page[PointBinding](total=len(matched), items=matched[offset : offset + limit])


# --- drafts ----------------------------------------------------------------------------------


@router.post("/drafts", status_code=201)
def create_draft(store: Store, body: NewDraft | None = None) -> DraftOut:
    body = body or NewDraft()
    try:
        return DraftOut.of(store.create_draft(body.base, body.author))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/drafts")
def drafts(store: Store) -> list[DraftOut]:
    return [DraftOut.of(d) for d in store.drafts()]


@router.get("/drafts/{draft_id}")
def draft(store: Store, draft_id: str) -> DraftOut:
    return DraftOut.of(_draft(store, draft_id))


@router.post("/drafts/{draft_id}/operations")
def add_operations(
    store: Store, draft_id: str, operations: Annotated[list[Operation], Body()]
) -> DraftOut:
    _draft(store, draft_id)
    try:
        return DraftOut.of(store.add_operations(draft_id, operations))
    except OperationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/drafts/{draft_id}/document")
def draft_document(store: Store, draft_id: str) -> WorldModel:
    _draft(store, draft_id)
    return store.draft_document(draft_id)


@router.get("/drafts/{draft_id}/validation")
def draft_validation(store: Store, draft_id: str) -> ValidationOut:
    _draft(store, draft_id)
    return ValidationOut.of(store.draft_issues(draft_id))


@router.get("/drafts/{draft_id}/diff")
def draft_diff(store: Store, draft_id: str) -> DiffOut:
    _draft(store, draft_id)
    return DiffOut.of(store.draft_diff(draft_id))


@router.post(
    "/drafts/{draft_id}/apply",
    status_code=201,
    responses={
        409: {"description": "The draft's base is no longer the head revision."},
        422: {"description": "The draft does not validate; `detail` lists the issues."},
    },
)
def apply_draft(store: Store, draft_id: str, body: ApplyDraft) -> RevisionOut:
    _draft(store, draft_id)
    try:
        return RevisionOut.of(store.commit(draft_id, body.message, body.author))
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Invalid as exc:
        detail: Any = ValidationOut.of(exc.issues).model_dump(mode="json")["issues"]
        raise HTTPException(422, detail) from exc


@router.delete("/drafts/{draft_id}", status_code=204)
def discard_draft(store: Store, draft_id: str) -> None:
    _draft(store, draft_id)
    store.discard(draft_id)
