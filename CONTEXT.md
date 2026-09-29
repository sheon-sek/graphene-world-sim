# Graphene World Simulator

A runtime-configurable simulation of real industrial infrastructure. Engineers define the world
and inject causes; the simulation engine computes the consequences. Ignition consumes the
result over OPC UA as if it were physical equipment.

## Language

### What exists

**Asset Model**:
The asset types, identities, member points, data types and Ignition export paths imported from
the Ignition tag export. It is data inside the World Model, not a separate runtime layer.
_Avoid_: tag list, schema

**World Model**:
The versioned description of what exists and how it is connected: assets, their parameters,
ports, connections, control bindings, instruments, point bindings and operating conditions.
_Avoid_: topology (too narrow), scenario

**ComponentType**:
A kind of equipment. It declares the behaviour model it binds to, its parameters (with units,
limits and change class), its ports, its fault modes and its point template.
_Avoid_: UDT (that is Ignition's term), class

**Asset**:
One instance of a ComponentType in the World Model, with a stable id, parameters and a location.
_Avoid_: device, node

**Port**:
A typed connection point declared by a ComponentType, such as `chw_in` or `power_in`, with a
domain and medium.

**Connection**:
A link from one Port to another in one domain (`power`, `chw`, `cw`, `air`, `water`, `fuel`,
`net`, `control`, `fire`), with its own parameters such as pipe length or cable impedance.
_Avoid_: edge, relation

**ControlBinding**:
The wiring of a controller: which instruments it reads and which actuators it drives.

**Instrument**:
A sensor or status reading on an asset: which model variable it measures, its range and
accuracy, and the device and network path it reports through.
_Avoid_: tag, signal

**Point**:
One value Ignition reads or writes, at an export path.
_Avoid_: tag, datapoint

**PointBinding**:
The mapping from an Instrument or command to a Point: export path, data type, unit and access
level. Ignition compatibility lives here.

**Operating Conditions**:
Inputs from outside the facility that engineers can change live: weather, IT load profiles,
utility availability.

### How it changes

**Revision**:
An immutable version of the World Model. The running simulation always names the revision it
was built from.

**Draft**:
Unapplied edits on top of a revision. Applying a draft creates a new revision.

**Change Class**:
How an edit reaches the running simulation: **Live** (applied at the next step), **Warm**
(affected partition rebuilt with state carried over), **Structural** (partition recompiled in
the background and swapped) or **Reinitialise** (whole simulation rebuilt).

**Partition**:
A part of the World Model compiled into one simulation unit (one FMU), split along weak
couplings so a change rebuilds only what it touches.

### What happens

**Fault**:
Something that happens to one selected asset: a trip, degraded capacity, a stuck actuator, a
sensor error, a lost supply or a failed network element. It changes only that asset's own
model inputs, parameters or instruments. It never names another asset.
_Avoid_: scenario, incident script

**Consequence**:
Any change in another asset's state that follows a fault. Consequences are computed by the
simulation, never authored.
_Avoid_: effect rule, response

**Event Log**:
The ordered record of commands and faults. With a revision, a seed and conditions, it
determines the trajectory.

**Frame**:
The consistent state of every point at one simulation step, as published to OPC UA and the web
application.
