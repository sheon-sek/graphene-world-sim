# Phase 1 spike report: Modelica Buildings + pandapower

Epic #2. Tasks #13, #14, #15, #26, #27, #28, #29. Code in [`spikes/phase1`](../../spikes/phase1).
Date: 2026-09-29.

## Verdict

All four acceptance criteria in ADR-0002 are met, and the stack is recommended with the four
amendments in the last section. One finding changes the runtime design. OpenModelica's own
Co-Simulation wrapper leaks memory on every step. So the master integrates the FMUs itself
in Model Exchange mode.

## What was built

- **World Model fragment** (`worldmodel/chiller_leg_dh01.json`). Chiller CH-001 (`Chiller/R_C1`)
  is fed by its CHW pump (`R_CP1`), CW pump (`R_CP5`), isolation valves (`R_CV1`, `R_CV5`),
  buffer tank `R_BT1` and tower cells `R_P1_CT1`–`CT5`. It serves `FCU/L1_FCU1` in Data Hall
  DH01. Asset ids and topology come from the graphene-demo-twin-2 inventory. The following
  are assumptions, because the source has no values for them:
  - sizes and pump curves;
  - the hall's air volume and thermal mass;
  - a small electrical network: an 11 kV grid, a 2.5 MVA transformer, and four 0.4 kV boards
    behind breakers.
- **Equipment library** (`modelica/GwsLib`). This is one Modelica model per ComponentType,
  wrapping Buildings 11.1.0 components:
  - `ElectricEIR` chiller with Carrier 19XR 1,076 kW manufacturer curves;
  - `SpeedControlled_y` pumps and fans on pressure curves;
  - `TwoWayLinear` valves;
  - `YorkCalc` towers;
  - a `DryCoilEffectivenessNTU` fan-coil;
  - a mixing-volume hall with IT heat and equipment thermal mass.

  Each wrapper exposes uniform ports, command inputs, point outputs and a `V_pu` supply-voltage
  input. The equipment's own protection is modelled inside the wrapper:
  - the chiller's evaporator flow switch;
  - an undervoltage trip on every motor.
- **Generator** (`gws_spike/generate.py`). It turns the fragment into a Modelica model and a
  point map. It adds one expansion vessel per closed water loop, found by following the
  connections.
- **Master** (`gws_spike/cosim.py`). It steps at 1 s. Each step it solves a pandapower load
  flow from the equipment's electrical demand, writes each asset's bus voltage into its
  `V_pu` input, then integrates the thermofluid model.

## Results

Numbers are from one run on this container. Raw output is in
`/mnt/project-files/sim-platform/phase1-spike-results.json`.

### Steady state (4 h from default start values)

| Point | Value |
| --- | --- |
| Hall DH01 air | 23.7 °C |
| CHW supply | 7.0 °C (set point) |
| CW leaving chiller / leaving tower | 33.3 °C / 28.0 °C (wet bulb 24 °C) |
| Chiller | 892 kW cooling, 144.5 kW electrical, COP 6.2 |
| Pumps | CHW 31.8 kg/s, CW 47.4 kg/s (from pump curve against loop resistance) |
| MCC-CH1 bus voltage | 0.977 pu |

None of these values is written anywhere in the code. Each one comes from the equipment
models, the loop resistances and the 800 kW IT load.

### Criterion 1: the generated model compiles, and changing the data changes the model

| Model | Equations | Compile (OpenModelica 1.25, FMI 2.0, CVODE) |
| --- | --- | --- |
| Buildings data-centre example (reference) | 4,444 variables | 4 min 25 s to 4 min 35 s (3 runs) |
| Generated chiller leg + DH01 | 1,617 | 65 s to 104 s (6 runs) |
| Same with a sixth tower cell added in the data | – | 82 s |

### Criterion 2: stepping speed

| Mode | Plant time per wall second |
| --- | --- |
| FMU alone, Model Exchange under FMPy CVODE | about 22,000× real time (40,000 s in 1.8 s) |
| FMU plus pandapower, steady | about 2,500× (4 h in 5.8 s; 133 load-flow solves took 4.2 s of it) |
| FMU plus pandapower, one fault scenario | an hour of plant time in 0.8 to 1.1 s |

The load flow takes about 33 ms per solve without numba. It re-solves only when a breaker
changes or a load moves by more than 1 %, and it ran 133 times in 4 simulated hours.

**Extrapolation to the whole site** is an estimate, not a measurement. The site has 639 assets,
about 50 times this fragment. With one FMU per partition and partitions stepping
independently, it would still run at hundreds of times real time on one core. A load flow
over the full single-line diagram (a few hundred buses) should stay under 100 ms. That is
affordable even if every step re-solves. The cost to plan for is compile time: about 50
partitions at 1–2 minutes each. That is roughly an hour cold on one core, and it parallelises
and caches by partition hash. A structural edit recompiles one partition.

### Criterion 3: state carries across a rebuild

- **Snapshot and restore.** A plant restarted from a snapshot tracks the original within
  0.001 K and 0.001 kW over 10 minutes.
- **Structural change while running.** Tower cell `R_P1_CT6` was added to the data. The new
  model compiled in the background (82 s) while the old plant kept stepping. The state then
  moved across by World Model asset id, and the new plant took over. The swap took 0.26 s of
  wall time.
  - Untouched variables showed no step: hall air stayed at 23.696 °C and CHW supply at 7.0 °C.
  - The change had its physical effect. CW flow rose from 47.4 to 49.0 kg/s through the extra
    parallel path. CW leaving the chiller fell from 33.3 to 32.3 °C. Chiller power fell from
    144.6 to 141.5 kW.

State is transferred at the World Model level, not as raw solver state. Each ComponentType
declares which of its internal temperatures seed which start parameters. New assets start from
defaults, and removed ones are dropped.

### Criterion 4: faults produce consequences with no fault-response code

Each fault sets one breaker open or trips one asset, and nothing else. No code refers to
another asset's reaction. The table below is what the models computed.

| Cause (15 min, then cleared) | What followed |
| --- | --- |
| Breaker CB-MCC-CH1 opens | MCC-CH1 drops to 0 V. Chiller, CHW pump and CW pump trip on undervoltage. The FCU fan keeps running on its own board but moves uncooled air. Hall air goes from 23.7 to 36.6 °C in 90 s and peaks at 53.9 °C. On reclose the plant restarts, the chiller runs at its limit (1,091 kW cooling, 214 kW electrical), and the hall is back to 27.3 °C 30 min later while the thermal mass discharges. |
| CHW pump R_CP1 trips | CHW flow stops. The chiller's flow switch stops the chiller, although its supply is healthy. The CW loop keeps circulating and cools to 24.6 °C. The hall follows the same path as above. |
| Tower cell R_P1_CT1 fan trips | Leaving water from that cell rises from 28.0 to 33.8 °C. Mixed CW leaving the chiller rises from 33.3 to 35.0 °C, and chiller power rises 4.8 % (144.5 to 151.4 kW) for the same cooling. The hall is unaffected. Everything returns to baseline after the fan restarts. |

**Caveat.** The hall's rate of temperature rise depends on its thermal parameters. These are
placeholders: 50 MJ/K of equipment mass coupled at 50 kW/K. The direction and ordering of
events are physical, but the magnitudes need calibration against real data-hall loss-of-cooling
curves before anyone quotes them.

## Findings that change the design

1. **OpenModelica's Co-Simulation wrapper leaks memory.** In version 1.25 with the CVODE
   Co-Simulation FMU, every `fmi2DoStep` allocates about 11 kB from a process-wide pool that is
   never released. That is 109 MB per 10,000 steps. The pool eventually aborts the process with
   `memory_pool.c: pool_expand` (SIGSEGV). In our runs that happened within about 150,000
   steps, which is under two days of plant time at a 1 s step. The same FMU run as Model
   Exchange under FMPy's CVODE stays flat: 181 MB from step 10,000 to step 40,000. In the
   rebuild test, one such process then ran 1.66 million steps (19 simulated days) without
   failing. The master
   therefore integrates FMUs itself. This also gives it control of event handling and solver
   restarts when inputs change.
2. **Start parameters must be literal defaults.** A start parameter bound to another parameter
   (`TMass_start = T_start`) is exported as a calculated parameter and silently ignored when
   set. The master now refuses to load a state it cannot set.
3. **Every state-bearing sub-model needs a declared start parameter.** A fan volume left
   unmapped caused a 270 kW transient on restore until it was added. Phase 2 should make the
   generator check that every continuous state in the FMU is covered by some component's
   state map.
4. **Electrical coupling fits one input per asset.** The only thing crossing from the
   electrical solver to thermofluid equipment is the supply voltage. The equipment's response
   (stop below 0.85 pu, restart when supply returns) lives in its own model. No asset refers to
   another asset.

## Not covered by the spike

- **Dockerfile (#13).** It fetches MSL 4.0.0 and Buildings 11.1.0 from GitHub tag archives.
  This container's proxy blocks those downloads, so the image was not built here. The same
  libraries, cloned at those tags and mounted from the host, were used for every result above.
- **Restart behaviour.** Chiller restart delays, soft starters and UPS ride-through are not
  modelled.
- **Controls.** Pumps and towers run at fixed commands. There are no Buildings or CDL control
  sequences yet.
- **Other domains.** networkx comm reachability, the water system and more than one partition
  are left to Phase 2.

## Amendments proposed to ADR-0002

1. The master integrates each FMU in Model Exchange mode with its own CVODE instance. It does
   not use OpenModelica's Co-Simulation wrapper.
2. State crosses a rebuild through World Model identity. Each ComponentType declares a state
   map (start parameter to internal variable). Start parameters are literal, and loading
   validates that each one is settable.
3. The electrical solver passes a per-asset supply voltage. Equipment models own their
   protection (undervoltage, flow switch). The load flow re-solves on breaker changes or load
   changes above a deadband, with a one-step lag.
4. Structural changes compile in the background and swap at a step boundary. The measured
   swap was 0.26 s for this fragment.
