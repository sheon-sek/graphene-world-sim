# Web application and 3D visualisation stack

Status: accepted. The owner accepted the visual quality of the hero data hall prototype (#73) on 2026-09-30.
Date: 2026-09-29

## Context

The web application combines engineering authoring (assets, connections, parameters, faults,
lifecycle) with operations (live 3D facility, alarms, trends, propagation). The 3D view must
impress: modern shaders and materials, animation, interaction, and scene state that follows
the simulation in real time. It must run in an ordinary browser, and it must not depend on the
simulator (ADR-0001): it reads the World Model and runtime APIs only.

## Options considered

| Option | Assessment |
| --- | --- |
| **three.js through react-three-fiber** | Runs in any browser. three.js has a WebGPU renderer with node-based shaders (TSL) and falls back to WebGL2. The pmndrs ecosystem (drei, postprocessing) covers PBR materials, HDR environment lighting, bloom, ambient occlusion, instancing and glTF loading. React integration keeps the 3D view and the authoring UI in one application and one state model. |
| Babylon.js | Comparable capability and a strong WebGPU story. Less natural inside a React application, and a smaller ecosystem of React components. A reasonable fallback. |
| Unreal or Unity with pixel streaming | Highest photorealism. Needs a GPU server per viewer, adds streaming latency, and puts the 3D view in a separate application from the authoring UI. |
| NVIDIA Omniverse Kit streaming | Photoreal RTX rendering and USD composition. Same GPU-per-viewer and streaming costs, and it pulls the visualisation towards a second engine. It can still be added later as an optional high-fidelity viewer fed from the World Model. |

## Decision

- **Application:** React, TypeScript and Vite.
- **3D:** three.js through react-three-fiber, using the WebGPU renderer where the browser
  supports it and WebGL2 otherwise; drei and pmndrs postprocessing for materials, lighting and
  effects.
- **Scene content:** generated from the World Model's floors, rooms and placements, so a new
  asset appears without code changes. Each ComponentType may carry an optional glTF model
  (Draco or Meshopt geometry, KTX2 textures); without one it is drawn procedurally.
- **Live values:** simulation values drive shader uniforms and instanced attributes directly
  each frame (fan speed, pipe flow, fluid temperature, air temperature fields, fault state),
  bypassing React re-renders, so the view stays smooth at site scale.
- **Authoring:** a React Flow schematic editor for wiring ports by domain.
- **Trends:** uPlot.
- **State and data:** zustand for client state; a TypeScript client generated from the API's
  OpenAPI spec; a WebSocket carrying per-step frame deltas.

## Consequences

- Visual quality depends mostly on art direction and 3D assets. Equipment models and the
  building shell need modelling effort, planned per ComponentType.
- The hero data hall prototype (#73) is built first in Phase 5. If it does not meet the bar,
  this record is revisited before the rest of the UI depends on the renderer.
- Photorealism at the level of path tracing is out of scope for the browser view.
