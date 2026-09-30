# Phase 6: whole site through Ignition

Result: **PASS** (3 of 3 checks), 2026-09-30, from `uv run python tests/ignition/live.py --site
--evidence docs/reports/phase6-ignition.json` on a gateway rebuilt by `igdev gateway reset`.
How to run it is in [`tests/ignition/README.md`](../../tests/ignition/README.md).

Setup: Ignition 8.3.8 run by igdev, in the same 4-core container as the simulator. One runtime
session covers every asset of the Graphene World Model (the whole-site thermofluid FMU plus
the electrical, services, network and controller layers) at a 1 s step, running at 1x and
served over OPC UA. The `DemoTwin` provider holds the tags generated for all 8,811 points.

| Check | Result |
| --- | --- |
| Every [DemoTwin] tag reads Good in Ignition | 8,811 of 8,811 Good |
| The whole site keeps real time (1 s step) | 600.0 simulated s in 600.0 wall s (RTF 1.00) |
| Every tag is still Good after the run | 8,811 of 8,811 Good |

Reaching real time took two changes, both measured on this run:

- The OPC UA server publishes by exception (ADR-0003 Amendment 2). About 1,300 of the 8,811
  points change in a 1 s step, but a new SourceTimestamp made every point a write: publishing
  took 0.3 s a frame and the served run managed 0.65x.
- The runner keeps the pace on average rather than per step, so an occasional step that runs
  over its second is made up by the next ones (0.93x before, 1.00x after).
