# Phase 8: AI agents investigate incidents through Ignition

Result: the analyst named the right root cause in **10 of 10** scenarios, and the right impact in
9 of them, 2026-09-30 to 2026-10-01. The evidence is in
[`phase8-agents.json`](phase8-agents.json), which `uv run python tests/agents/report.py
docs/reports/phase8-agents.json` writes from the graded answers.

## Method

Each scenario of [`data/scenarios/incidents.json`](../../data/scenarios/incidents.json) (#22) is
played into a real Ignition 8.3.8 gateway by
[`tests/agents/campaign.py`](../../tests/agents/campaign.py), which runs
[`evaluate.py`](../../tests/agents/evaluate.py) and then the analyst:

1. igdev rebuilds the gateway (`igdev gateway reset`) with the OPC UA, WebDev, Historian and MCP
   modules. The simulator serves the scenario scope (the slice plus every asset without a
   thermofluid `GwsLib` behaviour) over OPC UA, and the gateway imports a tag for every point
   that reads Good after the warm-up (about 6,490 tags), each recording to a Core Historian.
2. ignition-mcp's setup CLI installs its Runtime MCP server for the `analysis` role. Its setup
   needs the token's security level ticked under Designer; the owner approved that for the local
   igdev test gateway only.
3. Five plant minutes of normal operation, then the scenario's faults at their offsets, then its
   observation window. The session is then paused, and the gateway holds the final state.
4. The analyst is Claude Code in print mode, given ignition-mcp's BMS/EMS analyst assistant as
   its persona and the brief in [`analyst.md`](../../tests/agents/analyst.md): an operator says
   something has been wrong since a time, and asks what happened, what caused it and what it
   affects. Its only access to the plant is the Runtime MCP server, through
   [`tests/agents/mcp.py`](../../tests/agents/mcp.py). It may not read this repository and has
   no web access.
5. Each answer is graded by hand against the scenario's truth: the root cause (asset and
   failure) and the impact are each `correct`, `partial` or `wrong`.

The first five scenarios were played at real time on a gateway rebuilt for each run. The owner
then chose (2026-10-01) to play at ten times real time and to reuse a gateway for as many runs
as its two-hour trial allows. With both changes, a run plus its analysis takes about 8 minutes
instead of 35.

## Results

| Scenario | Truth | Root cause | Impact | MCP calls | Speed |
| --- | --- | --- | --- | --- | --- |
| chiller-trip | R_C1 trips | correct | correct | 157 | 1x |
| secondary-pump-trip | R_CP9 trips | correct | correct | 168 | 1x |
| condenser-fouling | R_C1 condenser fouls (COP 60 %) | correct | correct | 190 | 10x |
| header-sensor-bias | HDR/TS-02 reads +1.5 K | correct | correct | 135 | 1x |
| ups-rectifier-failure | UPS 1 rectifier fails | correct | partial | 22 | 1x |
| utility-loss | utility supply lost | correct | correct | 177 | 1x |
| utility-loss-genset-fail | utility lost and Genset 1 fails to start | correct (both) | correct | 135 | 10x |
| gateway-failure | GATEWAY A fails | correct | correct | 108 | 10x |
| fouling-and-tower-fan | CT1 fan trips, then R_C1 condenser fouls | correct (both) | correct | 400 | 10x |
| fcu-filter-choke | L1_FCU1 filter chokes | correct | correct | 239 | 10x |

The MCP call counts are the analysts' own estimates. Most calls came from the analysts' own
scripts, which swept the tag tree and the historian in batches.

- **Chiller trip:** found the trip at the exact scan and ruled out supply, flow, head pressure
  and load. It also saw that the staging controller asked for R_C2, which nothing starts (#84).
- **Secondary pump trip:** separated the pump stopping from a supply loss by reading the pump's
  meter voltage. It could not tell a drive trip from a lost run command, because the pump
  publishes no trip point.
- **Condenser fouling:** the first run was graded `partial`. The analyst rightly ruled fouling
  out because head pressure stayed flat, which showed that the model cut the COP without
  raising head pressure. After the fix (0829524), the rerun named fouling from the rising
  condenser pressure (774 to 978 kPa), discharge temperature and motor current at flat load.
- **Header sensor bias:** found the biased instrument by cross-checking four other supply
  readings, and saw that the controls acted on the false reading.
- **UPS rectifier failure:** found the rectifier failure and the 104 kW drop on the UPS's
  upstream meter. It could not confirm that the UPS was running on battery, because the
  Graphene source has no battery, mode or bypass points for the UPS. It asked for that check
  in the field instead, so impact is graded `partial`.
- **Utility loss, and with Genset 1 failing to start:** placed the cause upstream of all four
  incomers, and traced the genset start, the UPS ride-through and the cooling restart. In the
  second scenario it reported Genset 1's over-crank as an independent failure, because Gensets 2
  to 6 started on the same command.
- **Gateway failure:** called it a data-path fault, not a plant fault. 393 tags went Bad
  together behind GATEWAY A while power and hall temperatures stayed Good. It named what is now
  blind: cooling and leak detection.
- **Fouling and tower fan:** the first 10x run tied the two causes together (see Limitations).
  The rerun found both, arguing that the tower water temperatures never moved, so the chiller's
  rising head pressure was not caused by the lost tower.
- **FCU filter choke:** found the choke from falling static pressure and airflow before the
  alarm raised. It traced the hall warming from 25.5 to 26.9 °C and the chilled-water side
  backing off.

## Gaps the analysts found, and what was done

| Finding | Where | Done |
| --- | --- | --- |
| Controller set points read 0.085 kPa, -259 °C, 3000 % | the model | fixed, 849691f (HMI registers carry their units) |
| Condenser fouling cut the COP without raising head pressure | the model | fixed, 0829524 (the chiller lifts further) |
| Chiller starts counted per second | the model | fixed, f582735 (#80) |
| Transformer Efficiency read Bad with nothing to divide | the OPC UA server | fixed, 5312d9f: reads BadNoData, ADR-0003 Amendment 4 (#81) |
| Plant load and cooling demand fall to 0 when the header reads above return | the model | fixed after this report (#83): load is the heat the chillers take out of the water |
| No standby chiller in the scenario scope; staging asks for R_C2 in vain | the scenario scope | fixed after this report (#84): R_C2, its pumps, valves and towers are in scope |
| Modelica parameter warnings at compile time | the model | fixed after this report (#82), ADR-0002 Amendment 6 |
| No pump trip point, no UPS battery, mode or bypass points, no loss-of-voltage alarm on incomers, no chiller high-pressure cut-out set point, FCU flow without a unit, UPS "Input Power" carrying volts | the Graphene source data | pump trip, UPS battery charge, on-battery and input active power in kW, incomer loss of voltage and the FCU flow unit added after this report (#85), ADR-0003 Amendment 5; the UPS mode, bypass and the cut-out set point remain |
| Historian quality 192 reads `good: false`; `historian_browse` fails; `tag_query` continuation stops after the first page | ignition-mcp | noted only; ignition-mcp is reference only here |

## Limitations

- **10x clocks.** SourceTimestamps are simulation time (ADR-0003 Amendment 2), so at 10x they
  run ahead of the gateway's own clock. The first 10x run of fouling-and-tower-fan got its window
  in gateway time while the history was in plant time. The analyst then tied the second fault
  to the first, so that run was graded `partial` for both cause and impact. Since dca0253 the
  brief gives the window in plant time and says that the gateway's own stamps lag it. The
  utility-loss-genset-fail and gateway-failure runs came before that fix and were still
  graded correct. A gateway-stamped quality change still lags the plant stamps by a few
  minutes.
- **Grading.** One grader, by hand, against a short written truth. Partial credit is a
  judgement, recorded with its reason in the evidence.
- **One answer per scenario.** Each scenario has one analyst run, except condenser fouling and
  fouling-and-tower-fan, which were rerun after a fix. Earlier runs are kept in the evidence
  as `earlier`.
- **The truth names the cause, not the wording.** For the utility loss, the truth's asset is
  the utility transformer `~TX-1`, which has no tags. "Upstream of all four incomers" is graded
  as correct.
