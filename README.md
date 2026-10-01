# Graphene World Simulator

A runtime-configurable World Model and simulation engine for industrial infrastructure. Engineers
build and change the facility in a web application, inject faults, and watch the simulation
compute the consequences. The simulated facility is exposed over OPC UA, so Ignition connects
to it exactly as it would to real equipment.

> **The application injects the cause. The simulation engine computes the consequences.**

Status: Phase 2 (World Model). The graphene site is imported as World Model revision 1 and can
be read and edited through the API; nothing simulates yet. The plan is tracked as one epic issue
per phase, with tasks as sub-issues.

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

World Model tools:

```sh
# Build revision 1 from data/graphene and print its counts and Ignition contract checksum
uv run python -m gws_world_model.importers.graphene data/graphene /tmp/world-model.json
# Regenerate the published schema and OpenAPI document after changing the model or the API
uv run python -m gws_world_model.schema schemas/world-model.schema.json
uv run python -m gws_api.app docs/api/openapi.json
# ...and the web app's typed client from it
pnpm --filter @gws/web api:gen
```

Run the application: the API, the runtime and the web app in one process. Starting a session
compiles its models with OpenModelica (ADR-0002) the first time; later starts use the FMU cache.
OpenModelica runs in Docker and needs the Modelica Standard Library 4.0.0 and Modelica Buildings
11.1.0, which OpenModelica's own image does not include. Build the image CI uses once; it
downloads both libraries from GitHub:

```sh
docker build -t gws-omc:1.25 spikes/phase1
```

The runtime uses `gws-omc:1.25` by default. To use a library tree on the host instead, set
`GWS_OMLIB` to a directory holding `Modelica 4.0.0`, `ModelicaServices 4.0.0`,
`Complex 4.0.0.mo` and `Buildings 11.1.0` (laid out as in the Dockerfile); the runtime then
mounts it into `openmodelica/openmodelica:v1.25.0-minimal`. `GWS_OMC_IMAGE` overrides the image
and `GWS_FMU_CACHE` the cache directory (`~/.cache/gws-world-sim/fmu`). If neither the image
nor the libraries are there, starting a session fails with these steps.

```sh
pnpm --filter @gws/web build
uv run python -m gws_api.serve --import-graphene data/graphene --web apps/web/dist
# http://127.0.0.1:8000/          the Sessions page, then Operations, Engineering, Diagnostics
# http://127.0.0.1:8000/#/hero    the recorded hero data hall
# ?3d=0                           the plan view instead of the 3D hall
```

The end-to-end workflow test (add, configure, connect, run, fault, observe, recover) starts
its own server on a fresh store, or uses `GWS_E2E_URL`:

```sh
pnpm --filter @gws/web build && pnpm web:e2e
```

## Decisions

- [ADR-0001](docs/adr/0001-layer-boundaries.md): layer boundaries
- [ADR-0002](docs/adr/0002-simulation-stack.md): simulation stack
- [ADR-0003](docs/adr/0003-opcua-compatibility-contract.md): OPC UA compatibility contract
- [ADR-0004](docs/adr/0004-web-and-3d-stack.md): web application and 3D visualisation stack
- [ADR-0005](docs/adr/0005-world-model-storage.md): World Model documents, revisions and change
  classes

Asset data comes from `sheon-sek/graphene-demo-twin-2`, which is a source of what exists only.
None of its simulation, fault, runtime or UI code is used here.
