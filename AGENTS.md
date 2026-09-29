# Agent guide

Read `CONTEXT.md` for the vocabulary and `docs/adr/` before changing behaviour. ADRs are
binding: changing a decided rule needs a new or amended ADR, never a silent edit.

## Rules that cut across the code

- The application injects the cause; the simulation computes the consequences. Never write a
  rule that makes one asset react to another asset's fault. Model the equipment instead.
- Keep the layers of ADR-0001 apart. `tests/test_layer_boundaries.py` enforces the imports.
- The OPC UA identifiers in ADR-0003 are a contract with a live Ignition gateway. Do not change
  them.
- graphene-demo-twin-2 is a source of asset data only. Do not copy its simulation, fault,
  runtime or UI code.

## Checks before a pull request

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests
uv run pytest
pnpm web:check
```

## Work tracking

Issues live in GitHub Issues for `sheon-sek/graphene-world-sim`. Each phase is an epic issue
labelled `epic` and `phase:N`; each task is a sub-issue with `phase:N` and an `area:*` label.
Reference the task issue in the pull request that completes it.
