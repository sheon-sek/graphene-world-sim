# OPC UA and Ignition

The simulator is meant to stand in for the real Graphene plant behind an Ignition gateway. It
does that through its own OPC UA server, which publishes every point of the World Model the way
the plant's Ignition tag export names it. [ADR-0003](../adr/0003-opcua-compatibility-contract.md)
is the binding contract; this page is how to use it.

```
gws_api.serve (one process)
├── HTTP :8000      web app + API
├── simulation      runtime sessions (OpenModelica FMUs, power flow, services)
└── OPC UA :4840    opc.tcp://<host>:4840/graphene/twin  ◄── Ignition OPC UA connection
                                                              └── [DemoTwin] tags ── MCP ── agents
```

## How it is hosted

`uv run python -m gws_api.serve` starts the web app, the API, the simulation and the OPC UA
server in one process. There is nothing else to run.

| Setting | Value | Change it with |
| --- | --- | --- |
| Endpoint | `opc.tcp://0.0.0.0:4840/graphene/twin` (every network interface, port 4840) | `--opc-endpoint opc.tcp://0.0.0.0:<port>/graphene/twin` |
| Server name | `Graphene Demo Twin` | |
| Security | policy None, mode None, anonymous login | |
| Namespace | `urn:eetarp:graphene:demo:twin`, index 2 | |
| Turn it off | | `--no-opcua` |

The HTTP side binds `127.0.0.1` by default (`--host 0.0.0.0` to open it to other machines); the
OPC UA side always listens on every interface, so a gateway on another machine or in Docker can
reach it as long as port 4840 is open in the firewall.

## What it publishes

- **One variable per point**: all 8,905 points of the World Model (the 8,811 of the Graphene
  Ignition export, plus the points the World Model adds), whatever session is running.
- **Node ids**: `ns=2;s=point:<export path>`, for example `ns=2;s=point:Chiller/R_C1/Input Power`.
  In the path, `%` is written `%25` and `:` is written `%3A`, so
  `Genset/Genset 1/AC Voltage: L1-N` is `ns=2;s=point:Genset/Genset 1/AC Voltage%3A L1-N`.
  Folders mirror the export path from `Objects`, so browsing shows the same tree as the tag
  export.
- **Data types**: Float4 → Float, Float8 → Double, Int4 → Int32, Int8 → Int64, Boolean,
  String, DateTime; DataSet and Document as JSON text.
- **Quality**: the value's quality from the simulated instrument. Points of assets outside the
  served session's scope read **BadOutOfService**; a failed sensor reads BadSensorFailure; a lost
  network path BadCommunicationError; a value out of range UncertainEngineeringUnitsExceeded.
- **Timestamps**: the simulation time of the step that produced the value. Simulation time is
  pinned to the wall clock when the session is served, then moves with the simulation, so at
  10× it runs ahead of the gateway clock.
- **Writes**: the 381 command points (on/off, auto/manual, setpoints) are writable. A write is
  applied to the session as a command, exactly like one from the web app, and recorded in its
  event log. Every other point answers BadNotWritable.

## Which session it serves

The server publishes one session at a time. Starting a session from the web app serves it when
nothing else is served. To choose another:

- in the web app, open the session's **OPC UA & Ignition** workspace and press **Serve this
  session over OPC UA**; or
- `curl -X PUT http://127.0.0.1:8000/api/opcua/session -H 'content-type: application/json' -d '{"session": "R1"}'`
  (`{"session": null}` stops serving). `GET /api/opcua` shows what is served.

To feed Ignition every point live, serve a **Whole site** session. A smaller scope publishes
the same address space, with the points outside it reading BadOutOfService.

## Connect an Ignition gateway

These steps are for Ignition 8.3; the **OPC UA & Ignition** workspace shows them with the
endpoint filled in.

1. **OPC UA connection.** Gateway web page → **Connections → OPC → Connections** → create an
   **OPC UA** connection:
   - Name: `Graphene Demo Twin` (the generated tags expect this name; use another and set it
     when you download the tags).
   - Endpoint URL: `opc.tcp://<simulator host>:4840/graphene/twin`.
     If the gateway runs in Docker on the same computer, the host is `host.docker.internal`
     (Docker Desktop on Windows and macOS) or the Docker bridge address, usually `172.17.0.1`
     (Linux). `localhost` inside a container is the container itself.
   - Security policy **None**, security mode **None**, authentication **Anonymous**.
   The connection status turns **Connected**.
2. **Tag provider.** Create a **Standard** tag provider, for example `DemoTwin`, or use the one
   your project already has.
3. **Tags.** In the web app's **OPC UA & Ignition** workspace, press **Download Ignition tags
   (JSON)** (or `GET /api/ignition/tags?connection=Graphene%20Demo%20Twin`). In the Designer's
   Tag Browser, select the provider, choose **Import Tags**, and import the file at the
   provider's root. It contains:
   - a UDT definition under `_types_` for each Ignition UDT type, with members reading
     `nsu=urn:eetarp:graphene:demo:twin;s=point:{PointPath}/<member>` from the connection;
   - every point as a tag at its export path;
   - an alarm on each Boolean fault or alarm point;
   - with `?history=<provider>`, history on every tag (sampled at most once a second).
4. **Serve a session** in the web app. The tags turn Good within seconds.

A gateway that already has the original Graphene `[DemoTwin]` provider needs no new tags: its
item paths use the same node ids. Point its OPC UA connection at the simulator's endpoint.

From there, anything that reads tags works unchanged: Perspective views, alarm pipelines, the
historian, and the Ignition MCP Module that the AI agents use.

## Check it without Ignition

Any OPC UA client works, for example UaExpert or `asyncua`'s command line tools:

```sh
uv run uaread -u opc.tcp://127.0.0.1:4840/graphene/twin -n "ns=2;s=point:Chiller/R_C1/Input Power"
```

## Reference

- [ADR-0003](../adr/0003-opcua-compatibility-contract.md): the OPC UA compatibility contract.
- `packages/opcua-server`: the server (`gws_opcua`), which knows only point specifications.
- `packages/api/src/gws_api/opcua.py`: the bridge from a runtime session to the server.
- `packages/api/src/gws_api/ignition.py`: the Ignition tag generator.
- `tests/ignition/live.py`: the live check that connects a real gateway and reads every tag Good.
