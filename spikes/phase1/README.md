# Phase 1 stack spike

Throwaway code that tests ADR-0002 end to end on one chiller leg and one data hall. The
findings are in [`docs/spikes/phase1-report.md`](../../docs/spikes/phase1-report.md). Nothing
here is imported by the packages; the real runtime is built in Phase 2 onwards.

| Path | What it is |
| --- | --- |
| `worldmodel/chiller_leg_dh01.json` | World Model fragment: chiller CH-001, its pumps, valves, buffer tank and five tower cells, FCU/L1_FCU1 and hall DH01, plus a small electrical network. Asset ids and topology come from the inventory; sizes are assumptions. |
| `modelica/GwsLib/package.mo` | One hand-written Modelica model per ComponentType, wrapping Modelica Buildings Library 11.1.0 components behind uniform ports, inputs and outputs. |
| `gws_spike/generate.py` | Turns a fragment into a Modelica model and a point map (FMU variable to asset and signal). |
| `gws_spike/compile.py` | Compiles the model to an FMI 2.0 FMU with OpenModelica in a container. |
| `gws_spike/cosim.py` | Co-simulation master: FMU plus pandapower, fault injection, state snapshot. |
| `gws_spike/scenarios.py` | Steady state, three injected causes and a structural rebuild; writes `results.json`. |
| `Dockerfile` | OpenModelica 1.25 with MSL 4.0.0 and Buildings 11.1.0 at pinned tags. |

## Run

```sh
docker build -t gws-omc:1.25 spikes/phase1
cd spikes/phase1
uv run --with fmpy==0.3.32 --with pandapower==3.5.5 python -m gws_spike.scenarios /tmp/gws-spike
```

To use a host library tree instead of the image's, set `GWS_OMLIB` to a directory holding
`Modelica 4.0.0`, `ModelicaServices 4.0.0`, `Complex 4.0.0.mo` and `Buildings 11.1.0`, and
`GWS_OMC_IMAGE=openmodelica/openmodelica:v1.25.0-minimal`.
