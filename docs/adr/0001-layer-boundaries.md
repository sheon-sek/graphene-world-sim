# Layer boundaries

Status: accepted
Date: 2026-09-29

## Context

The platform must simulate real industrial infrastructure that engineers can reconfigure at
runtime. Ignition and the AI agents above it must see it exactly as they would see physical
equipment. The previous twin (graphene-demo-twin-2) mixed topology, behaviour and fault
responses in application code, so every new asset or fault meant new code. This repository
inherits only its asset data, never its architecture.

The governing principle is: **the application injects the cause; the simulation engine
computes the consequences.**

## Decision

The system is split into these layers. Each one talks only to its neighbours, through a named
interface.

| Layer | Owns | Never contains |
| --- | --- | --- |
| Asset Model | Asset types, identities, member points, data types, units, Ignition export paths | Behaviour or topology |
| World Model | What exists and how it connects: instances, engineering parameters, ports, typed connections, controller wiring, instruments, point bindings, operating conditions, placement | Equations or fault responses |
| Simulation & Domain Models | How each component type behaves: model classes, electrical element mappings, controller blocks, fault inputs | Facility-specific wiring or Ignition paths |
| Simulation Runtime | Time, stepping, co-simulation, fault activation, lifecycle, snapshots, reconfiguration | Per-asset "if X fails then do Y" logic |
| Instrumentation | Turning true physical state into measured points: sensor error, communication loss, quality | Physics |
| OPC UA | Address space, subscriptions, writes to command points | Anything simulator-specific |
| Web application | Authoring, lifecycle control, fault injection, 3D, trends, alarms, diagnostics | Simulation logic; it only calls the World Model and runtime APIs |

Below OPC UA sit Ignition (Tags, Alarms, Historian, Perspective), then Ignition MCP, then the
engineering and analysis AI agents. None of them may depend on the simulator.

Faults describe only what happens to the selected asset: a changed parameter or input of that
asset's own model, an instrument's error, or a network element's failure. Nothing in a fault
definition names another asset. Consequences come from the modelled topology, physics and
control logic. Control logic that real equipment contains (staging, PID loops, ATS sequences,
trip latches) is modelled as equipment, which is not a fault script.

In code, the layers map to packages:

| Package | Layer |
| --- | --- |
| `gws_world_model` | World Model (the Asset Model is imported into it as data) |
| `gws_runtime` | Simulation & Domain Models, Simulation Runtime, Instrumentation |
| `gws_opcua` | OPC UA |
| `gws_api` | HTTP API the web application uses |
| `apps/web` | Web application |

`tests/test_layer_boundaries.py` enforces the import rules: `gws_world_model` imports no other
layer, `gws_runtime` does not import `gws_opcua` or `gws_api`, and `gws_opcua` imports none of
the others. The process that serves OPC UA passes it points through its own interface.

## Consequences

- The simulator can be replaced below the OPC UA line without Ignition noticing.
- Adding an asset or a fault mode is a data change plus, at most, a new component type. It is
  never a new rule about another asset.
- A new layer dependency needs an amendment to this record and to the boundary test.
