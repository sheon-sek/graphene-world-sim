# Simulation stack

Status: accepted with amendments (Phase 1 spike, #2; see the amendment section)
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

## Why not Omniverse / PhysX as the engine

The behaviour this platform must produce is thermofluid flow, heat transfer, power flow and
control logic. PhysX simulates rigid bodies, collisions, articulated joints and particles. It
has no concept of a pump curve, a pipe network's pressure drop, a chiller's heat balance, a
room's thermal inertia or a bus losing supply. Building on it would mean writing every one of
those behaviours ourselves, which is exactly the hand-authored behaviour ADR-0001 rules out.

Omniverse's strengths are photoreal RTX rendering, USD scene composition, multi-user
collaboration and sensor simulation for robotics. None of them is needed by Ignition, which
only sees OPC UA, or by engineers observing a plant's response. Omniverse Kit also needs RTX
GPUs on the server, and each browser viewer is a video stream (see ADR-0004).

As we understand it, NVIDIA's own data-centre digital twin work takes its thermal and
electrical behaviour from partner solvers (CFD and power-system tools), not from PhysX, which
matches the split proposed here: a domain solver computes behaviour, and a renderer shows it.

By contrast, Modelica is equation-based and acausal: the World Model's topology is generated
into a system of equations, and the solver determines flows, temperatures and loads. The
Buildings Library supplies validated component models and real control sequences, and FMI
keeps the engine replaceable per partition.

Omniverse remains possible later in two roles that do not affect this decision: an optional
high-fidelity viewer fed from the World Model (exported to USD), and offline CFD studies of a
hall's airflow.

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

## Amendment 1: Phase 1 spike results (2026-09-29)

The spike met all four criteria above. The results are in
[`docs/spikes/phase1-report.md`](../spikes/phase1-report.md). It changes the decision in four
places:

1. **FMUs run as Model Exchange under the master's own CVODE**, not through OpenModelica's
   Co-Simulation wrapper. In OpenModelica 1.25 that wrapper leaks about 11 kB per
   `fmi2DoStep` and aborts the process. Model Exchange under FMPy's CVODE shows no growth.
2. **State crosses a rebuild through World Model identity.** Each ComponentType declares a
   state map (start parameter to internal variable). A snapshot reads those variables, and the
   rebuilt model receives them as start parameters keyed by asset id. Start parameters must be
   literal defaults, and loading a state that cannot be set is an error.
3. **The electrical solver passes one supply voltage per asset.** Equipment models own their
   protection, such as undervoltage trips and flow switches. The load flow re-solves when a
   breaker changes or a load moves beyond a deadband, and lags the thermofluid step by one
   step.
4. **Structural changes compile in the background and swap at a step boundary.** For one
   chiller leg and one hall, compilation took 65–104 s and the swap took 0.26 s.

