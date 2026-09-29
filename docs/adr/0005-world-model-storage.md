# World Model documents, revisions and change classes

Status: accepted
Date: 2026-09-29

## Context

Engineers change the facility while it runs. Every change must be reviewable before it lands,
recoverable afterwards, and classified by how it reaches the running simulation (CONTEXT.md,
Change Class). The World Model also carries the Ignition contract: 8,811 point paths whose
paths, data types and typeIds must stay byte-identical (ADR-0003).

## Decision

- **One document per revision.** A `WorldModel` is a self-contained, validated document
  (`schemas/world-model.schema.json`): site, ComponentTypes, assets, connections, instruments,
  control bindings, point bindings and operating conditions. It includes its ComponentTypes, so
  a revision can be rebuilt without the library that produced it. Its content hash is the
  SHA-256 of its canonical JSON.
- **Linear history.** Revisions are immutable and numbered. A draft is a list of operations
  (`put`, `delete`, `set_conditions`, `set_site`) on a base revision. Applying a draft
  validates the result and appends a revision, and is refused with a conflict when its base is
  no longer the head. There are no branches or merges; a stale draft is rebased by re-creating
  it.
- **Storage.** `SqliteStore` keeps revisions as zlib-compressed canonical JSON and drafts as
  operation lists, through the standard library. The SQL is portable, so a server database can
  replace SQLite without changing the model.
- **Change classes are computed, not declared by the editor.** `changes.diff` compares two
  documents and gives each changed entity a class by the rules in `changes.py`. A
  ComponentType parameter declares its own class; the most disruptive class in a diff decides
  how the runtime applies it.
- **Semantic validation is separate from the schema.** The schema guarantees shapes;
  `validate` checks references, ports and domains, parameter types and limits. Errors block
  applying a draft; warnings do not.
- **The component type library is data.** One reviewed JSON file per ComponentType in
  `gws_world_model/library/types/`. Point templates are not in the library: an importer
  derives them from the Ignition project that exposes the type.
- **Revision 1 is imported.** `importers/graphene.py` builds it from `data/graphene/`. Points
  outside any UDT bind through the reviewed rule table `importers/graphene_bindings.json`.
- **HTTP API.** FastAPI endpoints under `/api/world-model` read any revision and edit drafts.
  The OpenAPI document is published at `docs/api/openapi.json` and kept current by a test.

## Consequences

- Any revision can be diffed against any other, and the runtime always names the revision it
  runs.
- A whole revision is about 3 MB of JSON and parses in well under a second; the store caches
  recent revisions. If documents grow by an order of magnitude, the store can move to
  per-entity rows without changing the API.
- Concurrent editors work in separate drafts; the second to apply rebases.
