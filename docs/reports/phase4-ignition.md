# Phase 4: Ignition integration run

Result: **PASS** (8 of 8 checks), 2026-09-30, from `uv run python tests/ignition/live.py
--evidence docs/reports/phase4-ignition.json`. How to run it is in
[`tests/ignition/README.md`](../../tests/ignition/README.md).

Setup: Ignition 8.3.8 (standard edition) run by igdev, with the OPC UA and WebDev modules. The
simulator serves the Phase 1 slice over OPC UA at five times real time. The gateway's
`Graphene Demo Twin` connection reaches it with security None, and the `DemoTwin` provider
holds the tags `gws_api.ignition` generates for all 8,811 points: 625 UDT instances plus plain
tags, imported as 4,273 tags with no failures.

| Check | Result |
| --- | --- |
| The OPC UA connection to the simulator is healthy | PASS |
| Ignition reads a UDT member (`Chiller/R_C1/Input Power`) | 68.90 kW in Ignition, 68.78 kW in the simulator a moment earlier, Good |
| Ignition reads a plain tag (`Environment Monitoring/Level 1/DH01/IT Load`) | 300 kW, Good |
| A point outside the session's scope | Bad_OutOfService |
| A write to a measured point | refused with Bad_NotWritable |
| An Ignition write to `CH-001/Commands/Stop` | Good, and recorded as a runtime command in the event log |
| The chiller stops and reads manual | `On_Off` 0 and `Auto_Manual` 0 in Ignition |
| A trip fault on `Chiller/R_C1` | the generated `Active` alarm on `System Failure_Trip` is Active, Unacknowledged |
