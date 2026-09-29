# Simulation stack

Status: proposed (to be accepted or superseded by the Phase 1 spike, #2)
Date: 2026-09-29

## Context

The first facility is a datacenter: chilled and condenser water, cooling towers, buffer
tanks, room air, a 2N electrical system with gensets, ATS and UPS, control networks, fire and
water. The engine must:

- solve coupled thermofluid networks (pump curves, valve authority, heat balance, thermal
  inertia);
- solve an electrical network with source changeover and UPS battery state;
- run the control logic real equipment contains;
- couple domains (supply enables equipment, equipment draws power for its duty, network paths
  decide whether a reading arrives);
- rebuild from data when the topology changes, and accept parameter changes while running.

## Options considered

| Option | Assessment |
| --- | --- |
| NVIDIA Omniverse / OpenUSD / PhysX | PhysX covers rigid bodies, articulations and particles. It has no model of heat transfer, hydraulics or power flow, so every behaviour would have to be written by hand on top of it, which is the scripted behaviour this project exists to avoid. Kit also needs RTX GPUs on the server. OpenUSD remains an option for exporting the 3D layout later. |
| **Modelica Buildings Library compiled to FMUs + pandapower + networkx, co-simulated in Python** | Buildings (LBNL, BSD) provides chillers, cooling towers, pumps, valves, heat exchangers, room air models, a data-centre application package, and ASHRAE Guideline 36 / CDL control sequences. OpenModelica compiles generated models to FMUs, which FMPy runs. pandapower (BSD) solves AC power flow on a data-defined network. networkx computes network reachability. |
| Julia ModelingToolkit | Acausal models assembled from data at runtime, fast rebuilds, strong solvers. Its HVAC component library is much thinner than Buildings, so more components would be ours. |
| Hand-written Python physics | What graphene-demo-twin-2 did. Rejected: every behaviour becomes our own code. |

## Decision (proposed)

Use the Modelica Buildings + pandapower + networkx option:

- A model compiler generates Modelica source from the World Model and splits it into
  partitions along weak couplings (per hydraulic loop, per group of air zones, water system).
  Each partition compiles to one FMU, cached by a hash of its content.
- A co-simulation master steps at a fixed macro step (1 s by default). Each step it solves the
  electrical network with the previous step's loads, passes energised flags and voltages to the
  FMUs, steps the FMUs, exchanges boundary variables between partitions, steps runtime
  controllers, and publishes one consistent frame.
- Control sequences available in Buildings/CDL are compiled into the FMUs. Cross-domain
  controllers (ATS, genset start, load shed) run as data-configured runtime blocks.
- The same World Model revision, seed, conditions and event log produce the same trajectory.

## What the spike must show

The spike (Phase 1) uses one chiller leg and one Data Hall generated from data. It is accepted
if it shows:

1. the generated model compiles, and changing the data changes the model;
2. stepping runs at or faster than real time at a 1 s macro step, with an extrapolation to the
   whole site;
3. state carries across a rebuild without a visible step in untouched variables;
4. a capacity fault on one cooling unit produces downstream effects with no fault-response
   code, and recovery follows the fault's removal.

If compile time or stepping speed rules this out, the fallback is Julia ModelingToolkit, which
needs a new record superseding this one.

## Consequences

- The runtime depends on OpenModelica in a container image, not on developer machines.
- Engineering data the asset source lacks (pump curves, pipe sizes, valve Kv, room volumes,
  cable impedances) starts from typed defaults, and the UI flags every assumed value.
