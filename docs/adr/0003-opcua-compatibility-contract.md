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

## Amendment 1: The server (2026-09-30)

Building the server (Phase 4, #5) fixed the parts of the contract the first version left open.
`tests/contract/test_opcua_contract.py` now serves the whole Graphene World Model and checks
that a client browses exactly the Asset Model: all 8,811 points, at their paths, with their
data types.

- **Endpoint.** `opc.tcp://<host>:4840/graphene/twin`, server name `Graphene Demo Twin`,
  security policy None with anonymous access, as the gateway's existing connection expects.
  The namespace is registered first, so its index is 2 (`ns=2;s=point:…`).
- **Status codes.** The quality selects Good, Uncertain or Bad, and the reason selects the
  specific code where there is one: `comm_lost` → BadCommunicationError, `sensor_failed` →
  BadSensorFailure, `out_of_range` → UncertainEngineeringUnitsExceeded, `unbound` and
  `not_simulated` → BadConfigurationError. A point with no value yet reads
  BadWaitingForInitialData.
- **Scope.** The address space always holds every point binding of the World Model. A point
  outside the served session's scope reads BadOutOfService.
- **Time.** The served session's simulation time at the moment it is attached maps to the wall
  clock; SourceTimestamps then advance with simulation time, so they run faster or slower than
  the wall clock with the session's speed.
- **Writes.** A command point has CurrentRead and CurrentWrite access. A write is converted to
  the point's data type (BadTypeMismatch otherwise) and applied to the served session as a
  runtime command; a command the runtime rejects answers BadOutOfRange. Every other point
  answers BadNotWritable, whoever the client is.
- **Model change.** When the served World Model changes (a reinit to another revision, or
  another session), nodes are added and removed in place and a GeneralModelChangeEvent is
  fired from the Server object, listing each change (or, above 1,000 changes, one change on
  the Objects folder).
- **Generated tags.** Ignition tags generated from point bindings (`gws_api.ignition`) address
  points as `nsu=urn:eetarp:graphene:demo:twin;s=point:<encodedExportPath>`, so they do not
  depend on the namespace index.

The serving process (`python -m gws_api.serve`) runs the API and the OPC UA server together;
`PUT /api/opcua/session` chooses which runtime session is served.

## Amendment 2: Report by exception (2026-09-30)

Serving the whole site through Ignition (Phase 6, #66) showed that rewriting all 8,811 points
every step costs more than the step itself: only about 1,300 points change in a 1 s step, but a
new SourceTimestamp made every point a change, and the served run fell to 0.65x real time.

- **Publishing.** A point is written only when its value, quality or reason changes. An
  unchanged point keeps the SourceTimestamp of the step that last changed it, so
  SourceTimestamp now reads "the simulation time of the step that produced the current value",
  not "the latest step". This is how OPC UA devices report by exception, and it is what an
  Ignition subscription sees anyway, since it only delivers changed values.
- **Liveness.** A client that needs to know the simulation is still running reads a point that
  changes every step (for example a clock or an energy counter), not the timestamp of an
  arbitrary point.

## Amendment 3: Structural edits (2026-09-30)

When the served session swaps to another revision (ADR-0002 Amendment 5):

- Points the new revision keeps keep their values until the next frame; only new points read
  BadOutOfService until then.
- Points the new revision removes read **BadNotFound** for ten seconds, so a client sees them
  go Bad, and then their nodes are removed with a GeneralModelChangeEvent. An Ignition tag on
  a removed point then reports that its item no longer exists.

## Amendment 4: A ratio with nothing to divide by (2026-10-01)

A ratio aggregate whose denominator is zero, such as `Dashboard/Transformer Efficiency` while
the site runs on gensets and every incomer reads 0 kW, has no value. It reads
**BadNoData** (reason `undefined`), not plain Bad. An analyst then sees "no data", not a fault
in the data chain (#81).
