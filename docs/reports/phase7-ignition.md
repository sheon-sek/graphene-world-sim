# Phase 7: a chiller added to the running simulation reaches Ignition

Result: **PASS** (5 of 5 checks), 2026-09-30, from `uv run python tests/ignition/live.py
--reconfigure --evidence docs/reports/phase7-ignition.json` on a gateway rebuilt by
`igdev gateway reset`. How to run it is in [`tests/ignition/README.md`](../../tests/ignition/README.md).

Setup: Ignition 8.3.8 run by igdev. The simulator serves the Phase 1 slice at five times real
time with a 5 s step, and the `DemoTwin` provider holds the tags generated for all 8,811
points. The test then edits the World Model through the API, as the Engineering workspace
does: a draft places `Chiller/R_C9` (a copy of R_C1, piped and fed like it), and the revision
is applied to the running session with `POST /sessions/{id}/swap`.

| Check | Result |
| --- | --- |
| The session keeps running while the new partition compiles | simulation time 100 s → 115 s while compiling |
| The new chiller swaps into the running session | the changed partition compiled in 43 s; swapped in at t = 295 s |
| Ignition reads the new chiller's tags | `Chiller/R_C9/Input Power` 22.83 in Ignition and in the simulator, Good |
| Nothing was restarted | same simulator process, session and OPC UA connection; R_C1 still Good |
| A removed asset reads Bad, then its source disappears | Bad_NotFound in Ignition; the server's points went from 8,828 to 8,811 |

Only the 17 new chiller tags were imported into Ignition (the UDT definitions and the new
instance); every other tag was left as it was.
