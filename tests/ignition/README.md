# Ignition integration test

`live.py` runs a real Ignition gateway against the simulator (#49). It proves the ADR-0003
contract end to end: Ignition reads simulated values through the `Graphene Demo Twin` OPC UA
connection, an Ignition write to a command point becomes a runtime command, a write to a
measured point is refused, and a chiller trip raises the alarm the tag generator adds.

## Before the first run

- Docker with the Compose plugin.
- igdev on `PATH` (or its path in `IGDEV`). The repository's `igdev.toml` pins Ignition 8.3.8
  with the OPC UA and WebDev modules.
- The Ignition EULA accepted on this machine. This is a person's decision, so igdev records it
  only from `igdev setup --accept-eula`.
- The Phase 1 slice FMU: either already in `GWS_FMU_CACHE` or compiled on the first run, which
  needs OpenModelica in Docker and `GWS_OMLIB` (ADR-0002).

## Run it

```sh
uv run python tests/ignition/live.py
# or record the results:
uv run python tests/ignition/live.py --evidence docs/reports/phase4-ignition.json
# the Phase 6 gate: the whole site at real time, every [DemoTwin] tag Good
uv run python tests/ignition/live.py --site --evidence docs/reports/phase6-ignition.json
```

`pytest` runs the same test when `GWS_IGNITION_LIVE=1` is set, and skips it otherwise.

Every run rebuilds the checkout's gateway from scratch (`igdev gateway reset`: a fresh volume,
so a fresh trial period and nothing left from an earlier run), and leaves it running.
`igdev gateway down --volumes` removes it.

## What it does

1. Stages the built-in OPC UA and WebDev modules from the Ignition image with
   `igdev module add` (igdev mounts its staging folder over the image's module folder), then
   runs `igdev setup`, `igdev gateway reset` and `igdev gateway wait`.
2. Installs a disposable API token (a random key; Ignition stores only its SHA-256) and
   restarts the gateway.
3. Starts `python -m gws_api.serve` with the Graphene World Model, creates a session over the
   Phase 1 slice, serves it over OPC UA and runs it at five times real time.
4. Creates the `Graphene Demo Twin` OPC UA connection (security None, anonymous), the
   `DemoTwin` tag provider, imports the tags `gws_api.ignition` generates, and imports the
   `gws_probe` WebDev project in `project/`, whose `probe` endpoint reads and writes tags and
   queries alarms inside the gateway.
5. Runs the checks and prints PASS or FAIL for each. The exit status is 0 only when all pass.

## Phase 7: structural edits

```sh
uv run python tests/ignition/live.py --reconfigure --evidence docs/reports/phase7-ignition.json
```

Adds a chiller to the World Model while the slice runs, applies it to the running session
(`POST /api/runtime/sessions/{id}/swap`), imports the new chiller's tags and reads them in
Ignition, then removes the chiller and checks its tags go Bad.
