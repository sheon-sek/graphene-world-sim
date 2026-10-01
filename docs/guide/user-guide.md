# User guide

Graphene World Sim simulates the Graphene data centre and publishes it over OPC UA, so an
Ignition gateway reads it as if it were the real plant. You use the web application to choose
what to simulate, run it, inject faults, and watch what they cause.

```
web app  ──►  simulation engine  ──►  OPC UA server  ──►  Ignition  ──►  MCP  ──►  AI agents
(causes)      (consequences)          opc.tcp://<host>:4840/graphene/twin
```

**The application injects the cause; the simulation computes the consequences.** You never
script what a fault does. You trip a chiller and the models work out what happens to the water
temperatures, the halls, the alarms and the power.

## 1. Start the application

```sh
pnpm install && pnpm --filter @gws/web build
uv sync
uv run python -m gws_api.serve --import-graphene data/graphene --web apps/web/dist
```

That one process serves:

| What | Where |
| --- | --- |
| The web application | http://127.0.0.1:8000/ |
| The HTTP API | http://127.0.0.1:8000/api (interactive docs at http://127.0.0.1:8000/docs) |
| The OPC UA server | `opc.tcp://<this machine>:4840/graphene/twin` |

Starting a session compiles its models with OpenModelica in Docker the first time. Docker must
be running and the Modelica libraries available; the README's *Development* section has the
setup. Compiled models are cached (`~/.cache/gws-world-sim/fmu`), so later starts are fast.

The World Model is kept in `world.sqlite` in the folder you start from. After you pull a newer
version, the server adds the newer import as a new revision on start, so an old store does not
run the old wiring. If you have made your own revisions in Engineering, it keeps them as the
head and prints a warning instead; start with `--reimport` to add the import on top, or delete
`world.sqlite` to start again.

## 2. Choose what to simulate: sessions

The first page lists **presets**. A **session** simulates one scope of the World Model (a set of
assets) from one revision. Pick by what you need:

| Preset | Assets | Points live over OPC UA | First start | Use it for |
| --- | --- | --- | --- | --- |
| **Whole site** | all (777) | all 8,905 | compiles about 15 min, needs about 8 GB of RAM | Feeding a complete Ignition project; the real building |
| **Incident scope** | 638 | about 6,600 | about 1 min | The Phase 8 fault scenarios: the whole electrical network, network devices, services, with chillers 1 and 2 and their towers |
| **DH01 cooling slice** | 18 | about 880 | under 1 min | A quick look at one cooling chain in hall DH01 |

The whole site is the one that simulates the entire building. The slice is only one chiller
chain and one hall's units: its other points are published but read Bad (out of service).

When you start a session and no other session is being served over OPC UA, the new session is
served automatically. **Open sessions** below the presets lets you go back to one that is
already running.

## 3. The session bar

Every session page has the same bar at the top:

- `R1 · rev 1 · 777 assets`: the session id, the World Model revision it runs, and its scope.
- **Operations**, **Engineering**, **Diagnostics**, **OPC UA & Ignition**: the workspaces.
- The clock is simulated time. **Run** and **Pause**; **Step** advances one step, **+60** sixty
  steps (only while paused). The speed list sets how fast simulated time runs against the wall
  clock: 1× is real time, **Max** runs as fast as the computer can.

A red banner shows the last error from the server. Dismiss it with ×.

## 4. Operations: watch, inspect and inject faults

| Area | What it does |
| --- | --- |
| **Hall view** (centre) | The data hall in 3D (or the 2D plan: **Plan view**). Colours on the floor are the air temperature; pipes pulse with water flow; red rings mark assets with an active fault. |
| **Alarms / Events** (left) | Alarms raised by the fault and alarm points, each with the event that most likely caused it. Events lists everything you did to the session. Click an alarm to select its asset. |
| **Inspector** (right) | The selected asset: its live state, its points as Ignition reads them, and trends. Click a state row to trend it. |
| **Faults** (bottom left) | Choose an asset, a fault mode (trip, filter choke, sensor failure, capacity loss and so on), its severity, ramp and duration, then **Inject**. Active faults are listed with **Clear** (remove the cause) and **Reset** (acknowledge a latched trip). |
| **Propagation** (bottom right) | After a fault, every asset whose state moved, in the order it moved. Expand a row to see which signals changed and by how much. |

### Moving around in 3D

| Do | To |
| --- | --- |
| Left-drag | Orbit around the point you look at |
| Right-drag, middle-drag, or Shift + left-drag | Pan |
| Wheel | Zoom towards the cursor |
| Arrow keys (click the view first) | Pan |
| Click an asset | Select it and fly the camera to it |
| Click empty floor | Deselect and go back to the overview |

**3D quality.** The list next to **Plan view** switches between **High quality** and **Low
GPU**. Low GPU drops ambient occlusion, bloom, antialiasing, high-DPI rendering and the rack
lights, and draws at most 30 frames a second; colours, flows and alarms are unchanged.
Computers with integrated graphics (Intel UHD/Iris, AMD Radeon Graphics APUs) or software
rendering start on Low GPU, and the view steps down by itself if High quality stays below 24
frames a second. Your choice is remembered in the browser; `?quality=high` or `?quality=low`
in the address overrides it. If 3D is still too heavy, **Plan view** has no 3D at all.

### A first exercise

1. Start **DH01 cooling slice** and press **Run**.
2. In the 3D view, click the fan-coil unit **L1_FCU1**.
3. In **Faults**, pick **trip** and press **Inject**.
4. Watch the alarms arrive, and **Propagation** list the assets the trip reached.
5. Press **Clear**, then **Reset**, and watch the unit come back.

## 5. Engineering: change the World Model

Engineering edits a **draft** of the World Model on top of its latest revision:

- **Library**: add an asset of a component type to a room.
- **Schematic**: drag assets to place them; drag from an outlet to an inlet to connect them;
  select a connection and press Delete to remove it.
- **Properties**: rename, move, change parameters, connect and disconnect ports.
- **Draft**: shows whether the draft validates and how each change reaches a running session
  (its change class: Live, Warm, Structural or Reinitialise). Applying the draft creates a new
  revision; **Apply revision N to the running session** rebuilds the session on it. Structural
  changes compile in the background while the session keeps running.

## 6. Diagnostics

How the session is running: the real-time factor (how many simulated seconds per wall second
it could achieve), the models (partitions) and their solver work, step timings, what the scope
leaves out and why, the last error, and **snapshots** you can take and restore.

## 7. OPC UA & Ignition

This workspace shows the OPC UA endpoint, which session is being served, how many points are
published, and the steps to connect an Ignition gateway, with a download of the Ignition tag
import file. The details are in [OPC UA and Ignition](opcua-ignition.md).

## 8. The hero data hall

`#/hero` replays a recorded fan-coil trip in DH01 in 3D without a running simulation. It is a
quick way to see the 3D view and to check how a computer copes with it.

## Troubleshooting

| You see | Do |
| --- | --- |
| Starting a session fails with a message about the Modelica libraries or the Docker image | Follow the README's *Development* section to set up OpenModelica. |
| Starting the whole site takes a long time | Expected on the first start (about 15 minutes). Keep the page open. Later starts use the cache. |
| The whole site fails to compile with an out-of-memory error | Close other heavy programs (an Ignition gateway in Docker uses about 2 GB) and try again; the compiler needs about 8 GB. |
| A red banner saying a control binding "needs 4 per chiller", or `not enough values to unpack` | The World Model store is from an older version. Restart the server (it re-imports), or start it with `--reimport`. |
| The 3D view is slow | Choose **Low GPU**, or **Plan view**. |
| Ignition tags read Bad (out of service) | No session is served, or the point is outside the served session's scope. Serve a session in **OPC UA & Ignition**, or start **Whole site**. |
