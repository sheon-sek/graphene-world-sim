"""Revision store: immutable revisions, drafts on top of them, diffs and commits.

History is linear. A draft is based on a revision; committing it validates the resulting
document and appends a new revision, but only while its base is still the head, so two drafts
cannot silently overwrite each other.

`SqliteStore` keeps everything in SQLite through the standard library. The SQL is portable
(no SQLite-only types or functions beyond `?` parameters), so a Postgres store is the same
class with another connection.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Concatenate

from gws_world_model.changes import Change, diff
from gws_world_model.model import WorldModel
from gws_world_model.ops import OPERATIONS, Operation, apply
from gws_world_model.validate import Issue, errors, validate

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    number INTEGER PRIMARY KEY,
    parent INTEGER REFERENCES revisions(number),
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    document BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    base INTEGER NOT NULL REFERENCES revisions(number),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    author TEXT NOT NULL,
    operations TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    """The draft's base is no longer the head revision."""


class Invalid(StoreError):
    """Committing would produce a document with validation errors."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(f"{len(issues)} validation error(s)")
        self.issues = issues


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    number: int
    parent: int | None
    created_at: str
    author: str
    message: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class Draft:
    id: str
    base: int
    created_at: str
    updated_at: str
    author: str
    operations: tuple[Operation, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _pack(doc: WorldModel) -> bytes:
    return zlib.compress(doc.canonical_json().encode(), 6)


@lru_cache(maxsize=8)
def _unpack(blob: bytes) -> WorldModel:
    return WorldModel.model_validate_json(zlib.decompress(blob))


type _Method[**P, R] = Callable[Concatenate[SqliteStore, P], R]


def _serialised[**P, R](method: _Method[P, R]) -> _Method[P, R]:
    """Run the method holding the store's lock: one connection is shared by the API's threads."""

    def locked(self: SqliteStore, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class SqliteStore:
    """Safe to share between threads: every call holds the store's lock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)

    @_serialised
    def close(self) -> None:
        self._db.close()

    # --- revisions ---------------------------------------------------------------------------

    @_serialised
    def head(self) -> int | None:
        row = self._db.execute("SELECT MAX(number) FROM revisions").fetchone()
        return None if row[0] is None else int(row[0])

    @_serialised
    def create_revision(
        self, doc: WorldModel, message: str, author: str = "system"
    ) -> RevisionInfo:
        """Append a document as the next revision (used for the initial import)."""
        problems = errors(validate(doc))
        if problems:
            raise Invalid(problems)
        return self._append(doc, message, author, self.head())

    @_serialised
    def _append(
        self, doc: WorldModel, message: str, author: str, parent: int | None
    ) -> RevisionInfo:
        number = (parent or 0) + 1
        info = RevisionInfo(number, parent, _now(), author, message, doc.content_hash())
        with self._db:
            self._db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, parent, info.created_at, author, message, info.content_hash, _pack(doc)),
            )
        return info

    @_serialised
    def revisions(self) -> list[RevisionInfo]:
        rows = self._db.execute(
            "SELECT number, parent, created_at, author, message, content_hash "
            "FROM revisions ORDER BY number"
        )
        return [RevisionInfo(*row) for row in rows]

    @_serialised
    def info(self, number: int) -> RevisionInfo:
        row = self._db.execute(
            "SELECT number, parent, created_at, author, message, content_hash "
            "FROM revisions WHERE number = ?",
            (number,),
        ).fetchone()
        if row is None:
            raise NotFound(f"revision {number}")
        return RevisionInfo(*row)

    @_serialised
    def get(self, number: int) -> WorldModel:
        row = self._db.execute(
            "SELECT document FROM revisions WHERE number = ?", (number,)
        ).fetchone()
        if row is None:
            raise NotFound(f"revision {number}")
        return _unpack(bytes(row[0]))

    @_serialised
    def diff(self, old: int, new: int) -> list[Change]:
        return diff(self.get(old), self.get(new))

    # --- drafts ------------------------------------------------------------------------------

    @_serialised
    def create_draft(self, base: int | None = None, author: str = "system") -> Draft:
        base = self.head() if base is None else base
        if base is None:
            raise NotFound("no revision to base a draft on")
        self.info(base)
        now = _now()
        draft = Draft(uuid.uuid4().hex, base, now, now, author, ())
        with self._db:
            self._db.execute(
                "INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                (draft.id, base, now, now, author, "[]"),
            )
        return draft

    @_serialised
    def draft(self, draft_id: str) -> Draft:
        row = self._db.execute(
            "SELECT id, base, created_at, updated_at, author, operations FROM drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"draft {draft_id}")
        ops = tuple(OPERATIONS.validate_json(row[5]))
        return Draft(row[0], row[1], row[2], row[3], row[4], ops)

    @_serialised
    def drafts(self) -> list[Draft]:
        ids = [row[0] for row in self._db.execute("SELECT id FROM drafts ORDER BY created_at")]
        return [self.draft(i) for i in ids]

    @_serialised
    def add_operations(self, draft_id: str, operations: list[Operation]) -> Draft:
        """Append edits to a draft. They must apply cleanly to the draft's current document."""
        current = self.draft(draft_id)
        combined = [*current.operations, *operations]
        apply(self.get(current.base), combined)  # raises OperationError if they don't apply
        with self._db:
            self._db.execute(
                "UPDATE drafts SET operations = ?, updated_at = ? WHERE id = ?",
                (OPERATIONS.dump_json(combined).decode(), _now(), draft_id),
            )
        return self.draft(draft_id)

    @_serialised
    def draft_document(self, draft_id: str) -> WorldModel:
        d = self.draft(draft_id)
        return apply(self.get(d.base), list(d.operations))

    @_serialised
    def draft_diff(self, draft_id: str) -> list[Change]:
        d = self.draft(draft_id)
        return diff(self.get(d.base), self.draft_document(draft_id))

    @_serialised
    def draft_issues(self, draft_id: str) -> list[Issue]:
        return validate(self.draft_document(draft_id))

    @_serialised
    def commit(self, draft_id: str, message: str, author: str | None = None) -> RevisionInfo:
        d = self.draft(draft_id)
        if d.base != self.head():
            raise Conflict(f"draft is based on revision {d.base}; head is {self.head()}")
        doc = self.draft_document(draft_id)
        problems = errors(validate(doc))
        if problems:
            raise Invalid(problems)
        info = self._append(doc, message, author or d.author, d.base)
        self.discard(draft_id)
        return info

    @_serialised
    def discard(self, draft_id: str) -> None:
        self.draft(draft_id)
        with self._db:
            self._db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
