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


## Amendment 2: Phase 3 runtime (2026-09-30)

Building the runtime (Phase 3, #4) changed the decision in five places. The gate run on the
Phase 1 slice is in [`docs/reports/phase3-gate.md`](../reports/phase3-gate.md).

1. **All plant control runs as runtime blocks, not as CDL inside the FMUs.** Controllers read
   instruments, so a sensor fault (bias, freeze, failure, lost comms) reaches the controller
   exactly as it would reach the real PLC, and the controller's response is what moves the
   plant. Compiled-in CDL would read the true state and hide those faults. The blocks
   (`dp_pid`, `bypass_pid`, `chw_supply_temp`, `cw_temp`, `chw_staging`, `rotation`, `ats`,
   `genset_start`, `load_shed`) are configured only by ControlBindings. Control that lives
   inside the equipment itself (a chiller holding its own leaving temperature, undervoltage
   trips) stays in the equipment models; a sensor fault on such an internal sensor shifts
   that loop's set point by the sensor error.
2. **The compiler closes loops the World Model draws open.** The site data draws supply paths
   only. Every open water outlet drains into a generated return header with the loop's
   pressure reference, every open inlet draws from it, and a unit's open air inlet takes
   return air from the room its outlet serves. Each served room gets a generated air volume.
   Partitions are the connected physical components; weak-coupling cuts are deferred.
3. **Parameter faults are applied warm.** A fault that changes a Modelica parameter (chiller
   capacity or COP, strainer resistance) re-instantiates the partition with the scaled
   parameter and its state carried over by asset id, at a step boundary. Faults that are model
   inputs (trips, stuck actuators, head or airflow loss) are set as inputs.
4. **Faults have a time course and trips latch.** Severity scales a fault's parameters from
   healthy, a ramp grows it over time, and a duration clears it. A trip stays in effect after
   its cause clears until the asset is reset.
5. **Replay is exact; restore is to solver tolerance.** The event log reproduces a trajectory
   bit for bit. A snapshot restore re-initialises the FMUs from their state parameters, which
   carry every thermal state but not air humidity, and restarts the solver.

## Amendment 3: Operator commands (2026-09-30)

ADR-0003 makes command points writable, and a write is a runtime command. None of the World
Model's command points is a model input, though: they are the operator station of a PLC-
controlled asset (hand/auto, start, stop, enable, reset). So the master gives each simulated
asset an operator station, and a command point's signal acts on it:

1. **`Auto_Manual`** (1 or `Auto`, 0 or `Manual`). In manual, controller blocks no longer write
   the asset's model inputs, so an operator's own commands hold. The point reads the mode.
2. **`enabled`** (Boolean) is a permissive. While it is false the asset's run input is held off,
   whatever the controller or operator asks.
3. **`start` and `stop`** act on true. They switch the asset to manual and set its run input
   (`enable`, `speed`, `fanSpeed` or `position`, in that order of preference) on or off.
4. **`reset`** acts on true and is the protection reset the runtime already has.

The operator station is part of the runtime state: it is in snapshots, and the commands that
set it are in the event log, so replay stays exact. Command points whose signal is none of
these and not a model input (valve open and close commands on the buffer tanks, for example)
are rejected until their equipment is modelled.

## Amendment 4: Site services and the site's air units (2026-09-30)

Modelling the whole site (Phase 6, #7) changed the decision in four places.

1. **Site services are Python models, not Modelica partitions.** Cold water, leak detection,
   fire detection and protection, lifts, diesel fuel, and the room and weather sensors live in
   `gws_runtime.services`. They are mass balances and state machines with no stiff physics, so
   a compiled FMU would add build time and nothing else. The master steps them once per macro
   step after the thermofluid models, on the state those published. Their pumps, lifts and
   fire pumps are electrical loads like any other.
2. **The causes they react to are operating conditions or commands.** A fire in a room (smoke
   obscuration and hot-layer temperature), water leaking onto a floor, the lift traffic and the
   water main are conditions; operating a call point is a command. Every consequence follows
   from the World Model's connections: a fire zone shuts down the PAHUs its `fire` port is
   connected to and recalls the lifts, an alarm valve starts the fire pumps connected to it, a
   bulk diesel tank feeds the gensets connected to it. No rule names another asset's fault.
3. **The electrical network reports what a power meter reads.** Beyond the load flow's
   voltage and powers, it computes frequency by island (grid, genset governor droop, UPS
   oscillator), harmonic current and voltage distortion by load kind, and neutral current, so
   meters read realistic power quality from the network's state rather than constants.
   Gensets gain an engine model (coolant, oil pressure, starter battery, speed, run time) and
   stop on its protections and on an empty day tank.
4. **The compiler models only loops that reach a modelled unit.** A CRAC or PAHU takes outside
   air or rejects heat to it through the weather conditions; a CDU moves the liquid-cooled
   share of its room's IT heat into chilled water. A unit with no connection to any modelled
   loop is left out of the plan and listed as not modelled.
