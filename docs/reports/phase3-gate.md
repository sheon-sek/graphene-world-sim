# Phase 3 gate: the slice through the runtime

Result: **PASS**. 4.68 simulated hours at a 5 s macro step in 58.0 s (291× real time); session start 0.6 s with the FMU cached; replay 56.7 s.

Scope: 18 assets, partitions `P_8a873f80b585a6fd` (17 assets). Controllers: `ATS-A/ats`, `ATS-A/genset_start`, `ATS-B/ats`, `ATS-B/genset_start`, `PLC-01/chw_staging`, `PLC-01/chw_supply_temp`, `PLC-01/cw_temp`, `PLC-01/dp_pid`.

DH01 runs at 30% of design IT load: the slice holds only FCU1 and CCU-001 of the hall's cooling.

| Scenario | Cause | Check | Result | Evidence |
|---|---|---|---|---|
| Steady state | DH01 at 30 % IT load for an hour | dp loop holds 85 kPa | pass | 85.0 kPa |
| Steady state | DH01 at 30 % IT load for an hour | header supply at 14 °C | pass | 14.00 °C |
| Steady state | DH01 at 30 % IT load for an hour | condenser water at 29 °C | pass | 29.0 °C |
| Chiller trip | trip on Chiller/R_C1, clear, reset | chiller stops | pass | running=False |
| Chiller trip | trip on Chiller/R_C1, clear, reset | hall warms | pass | 26.01 → 26.57 °C |
| Chiller trip | trip on Chiller/R_C1, clear, reset | PLC sees the trip | pass | System Failure_Trip point |
| Chiller trip | trip on Chiller/R_C1, clear, reset | trip latches after the cause clears | pass | still stopped |
| Chiller trip | trip on Chiller/R_C1, clear, reset | restarts after reset and recovers | pass | 26.57 → 26.2 °C |
| Sensor bias | +1.5 K bias on header supply sensor HDR/TS-02 | controller trims to the wrong reading | pass | true supply 13.97 → 12.5 °C while the sensor reads 14.00 °C |
| Hot humid day | wet bulb 25 → 29 °C | towers work harder | pass | fan 0.00 → 15.00 kW |
| Hot humid day | wet bulb 25 → 29 °C | chiller works harder | pass | 61.9 → 70.0 kW, CW entering 30.17 °C |
| Condenser fouling | COP to 60 %, ramped over 10 min | same duty at more power (warm rebuild) | pass | 68 → 113 kW, cooling 380 → 381 kW |
| Secondary pump trip | trip on Chiller/R_CP9 | secondary flow collapses | pass | cooling block flow 20.3 → 5.5 kg/s |
| Secondary pump trip | trip on Chiller/R_CP9 | hall warms | pass | 26.42 → 29.34 °C |
| Utility loss | utility supply lost | chiller loses supply, then runs on the gensets | pass | lowest chiller supply 0.00 pu, ATS-A source 2.0 |
| Utility loss | utility supply lost | IT load never drops (UPS) | pass | lowest IT draw 300 kW |
| Gateway failure | GATEWAY A fails | cooling readings lose comms | pass | HDR/DPS-01 bad comm_lost |
| Gateway failure | GATEWAY A fails | dp loop holds its output | pass | CP9 speed held at 0.637 |
| Replay | re-run the event log from the start | identical trajectory | pass | 17 events, step 3372 |
