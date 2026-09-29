# Graphene World Simulator

A runtime-configurable World Model and simulation engine for industrial infrastructure. Engineers
build and change the facility in a web application, inject faults, and watch the simulation
compute the consequences. The simulated facility is exposed over OPC UA, so Ignition connects
to it exactly as it would to real equipment.

> **The application injects the cause. The simulation engine computes the consequences.**

Status: Phase 0 (foundations). Nothing simulates yet. The plan is tracked as one epic issue per
phase, with tasks as sub-issues.

## Layout

| Path | What it is |
| --- | --- |
| `packages/world-model` | `gws_world_model`: the World Model as versioned data |
| `packages/sim-runtime` | `gws_runtime`: domain models, simulation runtime, instrumentation |
| `packages/opcua-server` | `gws_opcua`: OPC UA exposure, driven only by point bindings |
| `packages/api` | `gws_api`: HTTP API for the web application |
| `apps/web` | The web application (React, TypeScript, Vite) |
| `docs/adr` | Architecture decision records |
| `CONTEXT.md` | Domain glossary |

## Development

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm 10.

```sh
# Python: lint, typecheck, test
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests
uv run pytest

# Web: typecheck, test, build
pnpm install
pnpm web:check
```

## Decisions

- [ADR-0001](docs/adr/0001-layer-boundaries.md): layer boundaries
- [ADR-0002](docs/adr/0002-simulation-stack.md): simulation stack (proposed, pending the Phase 1 spike)
- [ADR-0003](docs/adr/0003-opcua-compatibility-contract.md): OPC UA compatibility contract
- [ADR-0004](docs/adr/0004-web-and-3d-stack.md): web application and 3D visualisation stack

Asset data comes from `sheon-sek/graphene-demo-twin-2`, which is a source of what exists only.
None of its simulation, fault, runtime or UI code is used here.
