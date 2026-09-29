# OPC UA compatibility contract

Status: accepted
Date: 2026-09-29

## Context

The live Ignition gateway already has a `[DemoTwin]` tag provider bound to the previous twin's
OPC UA server. Its Tags, alarms and Perspective views address points by NodeId. The new
simulator should replace that server without any change on the gateway.

## Decision

In compatibility mode (the default), the OPC UA server reproduces the existing address space:

- Namespace URI `urn:eetarp:graphene:demo:twin`.
- Every point is a variable with NodeId `ns=<index>;s=point:<encodedExportPath>`, under folders
  that mirror its export path from `Objects`. Its BrowseName is the last path segment. Folder
  NodeIds are `folder:<exportPath>`.
- In the point identifier, `%` is encoded as `%25` first, then `:` as `%3A`. All other
  characters are unchanged, so `Genset/Genset 1/AC Voltage: L1-N` is
  `point:Genset/Genset 1/AC Voltage%3A L1-N`.
- Data types: Float4 → Float, Float8 → Double, Int4 → Int32, Int8 → Int64, Boolean → Boolean,
  String → String, DateTime → DateTime. DataSet and Document are JSON text in a String.
- SourceTimestamp is the simulation time of the step that produced the value. StatusCode comes
  from the instrumentation layer's quality (Good, Uncertain, Bad).

Unlike the previous twin, command points (on/off, auto/manual, setpoints, and other points the
World Model marks as commands) are writable. A write becomes a runtime command in the event
log, exactly like a command from the web application. Every other point rejects writes.

The address space is driven only by the World Model's point bindings. The server has no
knowledge of the simulator.

`gws_opcua.nodeid` implements the identifiers, and `tests/contract/test_opcua_contract.py`
guards them. The full address-space comparison against the Asset Model is added with the
server (#45).

## Consequences

- Existing gateway configuration keeps working unchanged.
- Points added through the World Model follow the same rules, so generated Ignition tags can
  address them.
- Changing any rule above breaks the gateway and needs a new record.
