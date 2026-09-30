within ;
package GwsLib "Equipment models of the Graphene World Simulator: one model per behaviour"
  extends Modelica.Icons.Package;

  package MediumW = Buildings.Media.Water "Water";
  package MediumA = Buildings.Media.Air "Moist air";

  function TWater "Temperature of the water arriving at a port"
    input Modelica.Units.SI.AbsolutePressure p;
    input Modelica.Units.SI.SpecificEnthalpy h;
    output Modelica.Units.SI.Temperature T;
  algorithm
    T := MediumW.temperature(MediumW.setState_phX(p, h));
  end TWater;

  function TAir "Temperature of the air arriving at a port"
    input Modelica.Units.SI.AbsolutePressure p;
    input Modelica.Units.SI.SpecificEnthalpy h;
    input Modelica.Units.SI.MassFraction Xi[1];
    output Modelica.Units.SI.Temperature T;
  algorithm
    T := MediumA.temperature(MediumA.setState_phX(p, h, Xi));
  end TAir;

  model Chiller "Electric chiller with EIR performance curves and an evaporator flow switch"
    parameter Buildings.Fluid.Chillers.Data.ElectricEIR.Generic per
      "Performance record (manufacturer curves, scaled to the asset's rating)";
    parameter Modelica.Units.SI.PressureDifference dpChw_nominal = 60000;
    parameter Modelica.Units.SI.PressureDifference dpCw_nominal = 60000;
    parameter Real flowSwitchFraction = 0.2
      "Runs only while evaporator flow is above this fraction of nominal";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the drive stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature TChw_start = 287.15;
    parameter Modelica.Units.SI.Temperature TCw_start = 303.15;

    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_a cw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b cw_out(redeclare package Medium = MediumW);

    Modelica.Blocks.Interfaces.BooleanInput enable "Run command";
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput TChwSet(unit="K") "Leaving CHW set point";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W") "Electrical power";
    Modelica.Blocks.Interfaces.RealOutput QEva(unit="W") "Cooling delivered (positive)";
    Modelica.Blocks.Interfaces.RealOutput PLR(unit="1") "Cooling delivered over nominal";
    Modelica.Blocks.Interfaces.RealOutput TChwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TCwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TCwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mChw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput mCw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.BooleanOutput running;

    Buildings.Fluid.Chillers.ElectricEIR chi(
      redeclare package Medium1 = MediumW,
      redeclare package Medium2 = MediumW,
      per = per,
      dp1_nominal = dpCw_nominal,
      dp2_nominal = dpChw_nominal,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T1_start = TCw_start,
      T2_start = TChw_start);
    Buildings.Fluid.Sensors.MassFlowRate mChw(redeclare package Medium = MediumW);
    Buildings.Fluid.Sensors.MassFlowRate mCw(redeclare package Medium = MediumW);
  equation
    running = enable and V_pu > VTrip_pu and mChw.m_flow > flowSwitchFraction * per.mEva_flow_nominal;
    chi.on = running;
    chi.TSet = TChwSet;
    P = chi.P;
    QEva = -chi.QEva_flow;
    PLR = QEva / (-per.QEva_flow_nominal);
    TChwEnt = TWater(chw_in.p, inStream(chw_in.h_outflow));
    TCwEnt = TWater(cw_in.p, inStream(cw_in.h_outflow));
    TChwLvg = chi.vol2.T;
    TCwLvg = chi.vol1.T;
    mChw_flow = mChw.m_flow;
    mCw_flow = mCw.m_flow;
    connect(chw_in, mChw.port_a);
    connect(mChw.port_b, chi.port_a2);
    connect(chi.port_b2, chw_out);
    connect(cw_in, mCw.port_a);
    connect(mCw.port_b, chi.port_a1);
    connect(chi.port_b1, cw_out);
  end Chiller;

  model Pump "Variable-speed pump: flow emerges from its curve and the loop resistance"
    replaceable package Medium = MediumW;
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal;
    parameter Modelica.Units.SI.Temperature T_start = 293.15;
    parameter Modelica.Units.SI.Density rho = 1000 "Density used to size the curve";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the drive stops below this supply voltage";

    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = Medium);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = Medium);
    Modelica.Blocks.Interfaces.RealInput speed(unit="1") "Speed command, 0 to 1";
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput headFactor(unit="1")
      "Fraction of the design head the impeller still makes (wear); 1 when healthy";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W");
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput dp(unit="Pa") "Head: discharge minus suction";
    Modelica.Blocks.Interfaces.RealOutput pSuc(unit="Pa");
    Modelica.Blocks.Interfaces.RealOutput pDis(unit="Pa");
    Modelica.Blocks.Interfaces.RealOutput TEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput y(unit="1") "Actual speed";

    Buildings.Fluid.Movers.SpeedControlled_y mov(
      redeclare package Medium = Medium,
      per(pressure(
        V_flow = m_flow_nominal / rho * {0, 1, 1.5},
        dp = dp_nominal * {1.5, 1, 0.5})),
      use_inputFilter = false,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
  equation
    // Head scales with the square of speed, so a worn impeller making a fraction f of its
    // head behaves like the same impeller at sqrt(f) of the speed.
    y = if V_pu > VTrip_pu then speed * sqrt(max(headFactor, 0)) else 0;
    mov.y = y;
    P = mov.P;
    m_flow = mov.m_flow;
    pSuc = inlet.p;
    pDis = outlet.p;
    dp = outlet.p - inlet.p;
    TEnt = mov.heatPort.T;
    connect(inlet, mov.port_a);
    connect(mov.port_b, outlet);
  end Pump;

  model Fan "Variable-speed fan"
    extends Pump(redeclare package Medium = MediumA, rho = 1.2);
  end Fan;

  model Valve "Isolation or control valve"
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal = 10000;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealInput position(unit="1") "0 closed, 1 open";
    Modelica.Blocks.Interfaces.RealOutput y(unit="1") "Actual position";
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput dp(unit="Pa");
    Buildings.Fluid.Actuators.Valves.TwoWayLinear val(
      redeclare package Medium = MediumW,
      m_flow_nominal = m_flow_nominal,
      dpValve_nominal = dp_nominal,
      use_inputFilter = false);
  equation
    y = position;
    val.y = position;
    m_flow = val.m_flow;
    dp = val.dp;
    connect(inlet, val.port_a);
    connect(val.port_b, outlet);
  end Valve;

  model Branch "Pipework branch with a fixed flow resistance, instrumented"
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal = 20000;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput TEnt(unit="K") "Supply temperature";
    Modelica.Blocks.Interfaces.RealOutput dp(unit="Pa");
    Buildings.Fluid.FixedResistances.PressureDrop res(
      redeclare package Medium = MediumW, m_flow_nominal = m_flow_nominal, dp_nominal = dp_nominal);
  equation
    m_flow = res.m_flow;
    dp = res.dp;
    TEnt = TWater(inlet.p, inStream(inlet.h_outflow));
    connect(inlet, res.port_a);
    connect(res.port_b, outlet);
  end Branch;

  model CoolingTower "Open cooling tower cell with a variable-speed fan (York correlation)"
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal = 30000;
    parameter Modelica.Units.SI.Power PFan_nominal = 15000;
    parameter Modelica.Units.SI.Temperature TAirInWB_nominal = 300.15;
    parameter Modelica.Units.SI.TemperatureDifference TApp_nominal = 5;
    parameter Modelica.Units.SI.TemperatureDifference TRan_nominal = 5;
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the fan stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature T_start = 303.15;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput TWetBulb(unit="K") "Outdoor wet-bulb temperature";
    Modelica.Blocks.Interfaces.RealInput capFactor(unit="1")
      "Fraction of the fill's air contact that remains (fouling); 1 when healthy";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");
    Buildings.Fluid.HeatExchangers.CoolingTowers.YorkCalc tow(
      redeclare package Medium = MediumW,
      m_flow_nominal = m_flow_nominal,
      dp_nominal = dp_nominal,
      PFan_nominal = PFan_nominal,
      TAirInWB_nominal = TAirInWB_nominal,
      TApp_nominal = TApp_nominal,
      TRan_nominal = TRan_nominal,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
  equation
    // Fouled fill contacts less air: modelled as a fraction of the design air flow.
    tow.y = if V_pu > VTrip_pu then fanSpeed * capFactor else 0;
    tow.TAir = TWetBulb;
    PFan = tow.PFan;
    TLvg = tow.TLvg;
    TEnt = TWater(inlet.p, inStream(inlet.h_outflow));
    m_flow = tow.m_flow;
    connect(inlet, tow.port_a);
    connect(tow.port_b, outlet);
  end CoolingTower;

  model BufferTank "Well-mixed chilled-water buffer tank"
    parameter Modelica.Units.SI.Volume V = 10;
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal = 5000;
    parameter Modelica.Units.SI.Temperature T_start = 287.15;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealOutput T(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput p(unit="Pa") "Water pressure in the tank";
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumW,
      V = V,
      m_flow_nominal = m_flow_nominal,
      nPorts = 2,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      massDynamics = Modelica.Fluid.Types.Dynamics.SteadyState,
      T_start = T_start);
    Buildings.Fluid.Sensors.MassFlowRate sen(redeclare package Medium = MediumW);
    Buildings.Fluid.FixedResistances.PressureDrop res(
      redeclare package Medium = MediumW, m_flow_nominal = m_flow_nominal, dp_nominal = dp_nominal)
      "Nozzles and connecting pipework; tanks in parallel need it to share flow";
  equation
    T = vol.T;
    TEnt = TWater(inlet.p, inStream(inlet.h_outflow));
    m_flow = sen.m_flow;
    p = outlet.p;
    connect(inlet, sen.port_a);
    connect(sen.port_b, res.port_a);
    connect(res.port_b, vol.ports[1]);
    connect(vol.ports[2], outlet);
  end BufferTank;

  model CoilValve "Chilled-water coil valve with the unit's own supply-air controller"
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal;
    parameter Real k = 0.1 "Controller gain, per K";
    parameter Modelica.Units.SI.Time Ti = 120 "Controller integral time";
    parameter Real xi_start = 5 "Controller integrator at start: its output over its gain";
    Modelica.Fluid.Interfaces.FluidPort_a port_a(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b port_b(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealInput TSet(unit="K") "Supply-air set point";
    Modelica.Blocks.Interfaces.RealInput TMea(unit="K") "Supply-air temperature";
    Modelica.Blocks.Interfaces.BooleanInput on "Unit running: the valve closes when it stops";
    Modelica.Blocks.Interfaces.RealOutput y(unit="1") "Valve position";
    Buildings.Controls.Continuous.LimPID con(
      controllerType = Modelica.Blocks.Types.SimpleController.PI,
      k = k,
      Ti = Ti,
      yMax = 1,
      yMin = 0,
      reverseActing = false,
      initType = Modelica.Blocks.Types.Init.NoInit)
      "Opens the valve while the supply air is warmer than its set point";
    Buildings.Fluid.Actuators.Valves.TwoWayEqualPercentage val(
      redeclare package Medium = MediumW,
      m_flow_nominal = m_flow_nominal,
      dpValve_nominal = dp_nominal,
      use_inputFilter = false,
      l = 0.002);
  initial equation
    // Set here rather than through xi_start, so the start stays a settable FMU parameter.
    con.I.y = xi_start;
  equation
    con.u_s = TSet;
    con.u_m = TMea;
    y = if on then con.y else 0;
    val.y = y;
    connect(port_a, val.port_a);
    connect(val.port_b, port_b);
  end CoilValve;

  model FanCoil "Chilled-water fan coil: supply fan, dry coil and a valve holding the supply air"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Design cooling (positive)";
    parameter Modelica.Units.SI.MassFlowRate mWat_flow_nominal;
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dpWat_nominal = 40000;
    parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 300;
    parameter Modelica.Units.SI.Temperature TWatEnt_nominal = 287.15;
    parameter Modelica.Units.SI.Temperature TAirEnt_nominal = 305.15;
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the fan stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature TRet_start = 303.15 "Return air, fan volume";
    parameter Real xiVal_start = 5 "Valve controller integrator at start";
    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_a air_in(redeclare package Medium = MediumA);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput airFactor(unit="1")
      "Fraction of the design air flow the filter still passes; 1 when clean";
    Modelica.Blocks.Interfaces.RealInput TSupSet(unit="K") "Supply-air set point";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Cooling delivered (positive)";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TSupAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TRetAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mChw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput mAir_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput yVal(unit="1") "CHW valve position";
    Modelica.Blocks.Interfaces.RealOutput yFan(unit="1") "Fan speed";
    Modelica.Blocks.Interfaces.RealOutput dpFan(unit="Pa") "Unit static pressure";
    Modelica.Blocks.Interfaces.RealOutput phiSup(unit="1") "Supply-air relative humidity";
    Modelica.Blocks.Interfaces.RealOutput phiRet(unit="1") "Return-air relative humidity";
    Modelica.Blocks.Interfaces.BooleanOutput filterAlarm "Filter differential pressure switch";
    Buildings.Utilities.Psychrometrics.Phi_pTX rhSup;
    Buildings.Utilities.Psychrometrics.Phi_pTX rhRet;
    Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU coi(
      redeclare package Medium1 = MediumW,
      redeclare package Medium2 = MediumA,
      m1_flow_nominal = mWat_flow_nominal,
      m2_flow_nominal = mAir_flow_nominal,
      dp1_nominal = dpWat_nominal / 2,
      dp2_nominal = dpAir_nominal,
      Q_flow_nominal = Q_flow_nominal,
      configuration = Buildings.Fluid.Types.HeatExchangerConfiguration.CounterFlow,
      T_a1_nominal = TWatEnt_nominal,
      T_a2_nominal = TAirEnt_nominal);
    CoilValve val(
      m_flow_nominal = mWat_flow_nominal,
      dp_nominal = dpWat_nominal / 2,
      xi_start = xiVal_start);
    Fan fan(
      m_flow_nominal = mAir_flow_nominal,
      dp_nominal = 2 * dpAir_nominal,
      T_start = TRet_start,
      VTrip_pu = VTrip_pu);
  equation
    fan.speed = fanSpeed * airFactor;
    fan.V_pu = V_pu;
    fan.headFactor = 1;
    val.TSet = TSupSet;
    val.TMea = TSupAir;
    val.on = fan.y > 0.01;
    yVal = val.y;
    Q = coi.Q1_flow;
    PFan = fan.P;
    TSupAir = TAir(air_out.p, air_out.h_outflow, air_out.Xi_outflow);
    TRetAir = TAir(air_in.p, inStream(air_in.h_outflow), inStream(air_in.Xi_outflow));
    TChwEnt = TWater(chw_in.p, inStream(chw_in.h_outflow));
    TChwLvg = TWater(chw_out.p, chw_out.h_outflow);
    mChw_flow = coi.m1_flow;
    mAir_flow = fan.m_flow;
    yFan = fan.y;
    dpFan = fan.dp;
    // The filter's switch sees the choked filter's extra resistance as lost airflow.
    filterAlarm = airFactor < 0.8;
    rhSup.p = air_out.p;
    rhSup.T = TSupAir;
    rhSup.X_w = air_out.Xi_outflow[1];
    phiSup = rhSup.phi;
    rhRet.p = air_in.p;
    rhRet.T = TRetAir;
    rhRet.X_w = inStream(air_in.Xi_outflow[1]);
    phiRet = rhRet.phi;
    connect(chw_in, val.port_a);
    connect(val.port_b, coi.port_a1);
    connect(coi.port_b1, chw_out);
    connect(air_in, fan.inlet);
    connect(fan.outlet, coi.port_a2);
    connect(coi.port_b2, air_out);
  end FanCoil;

  model AirHandler "Primary air-handling unit: outdoor air through a fan and a wet CHW coil"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Design cooling (positive)";
    parameter Modelica.Units.SI.MassFlowRate mWat_flow_nominal;
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dpWat_nominal = 40000;
    parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 300;
    parameter Modelica.Units.SI.Temperature TWatEnt_nominal = 287.15;
    parameter Modelica.Units.SI.Temperature TAirEnt_nominal = 305.15;
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the fan stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature TFan_start = 303.15 "Outdoor air, fan volume";
    parameter Real xiVal_start = 5 "Valve controller integrator at start";
    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput airFactor(unit="1")
      "Fraction of the design air flow the filter still passes; 1 when clean";
    Modelica.Blocks.Interfaces.RealInput TSupSet(unit="K") "Supply-air set point";
    Modelica.Blocks.Interfaces.RealInput TOut(unit="K") "Outdoor dry bulb";
    Modelica.Blocks.Interfaces.RealInput XOut(unit="1") "Outdoor water vapour mass fraction";
    Modelica.Blocks.Interfaces.BooleanInput fireStop "Fire shutdown signal from its fire zone";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Cooling delivered, sensible plus latent";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TSupAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TOutAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mChw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput mAir_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput yVal(unit="1") "CHW valve position";
    Modelica.Blocks.Interfaces.RealOutput phiSup(unit="1") "Supply-air relative humidity";
    Modelica.Blocks.Interfaces.RealOutput phiOut(unit="1") "Outdoor-air relative humidity";
    Modelica.Blocks.Interfaces.RealOutput yFan(unit="1") "Fan speed";
    Modelica.Blocks.Interfaces.BooleanOutput filterAlarm "Filter differential pressure switch";
    Modelica.Blocks.Interfaces.BooleanOutput fireAlarm "Fire shutdown in force";
    Buildings.Utilities.Psychrometrics.Phi_pTX rhOut;
    Buildings.Fluid.Sources.Boundary_pT out(
      redeclare package Medium = MediumA, use_T_in = true, use_X_in = true, nPorts = 1)
      "Outdoor air intake";
    Buildings.Fluid.HeatExchangers.WetCoilEffectivenessNTU coi(
      redeclare package Medium1 = MediumW,
      redeclare package Medium2 = MediumA,
      m1_flow_nominal = mWat_flow_nominal,
      m2_flow_nominal = mAir_flow_nominal,
      dp1_nominal = dpWat_nominal / 2,
      dp2_nominal = dpAir_nominal,
      use_Q_flow_nominal = true,
      Q_flow_nominal = -Q_flow_nominal,
      T_a1_nominal = TWatEnt_nominal,
      T_a2_nominal = TAirEnt_nominal,
      w_a2_nominal = 0.015,
      configuration = Buildings.Fluid.Types.HeatExchangerConfiguration.CounterFlow);
    CoilValve val(
      m_flow_nominal = mWat_flow_nominal,
      dp_nominal = dpWat_nominal / 2,
      xi_start = xiVal_start);
    Fan fan(
      m_flow_nominal = mAir_flow_nominal,
      dp_nominal = 2 * dpAir_nominal,
      T_start = TFan_start,
      VTrip_pu = VTrip_pu);
    Buildings.Utilities.Psychrometrics.Phi_pTX phi;
  equation
    out.T_in = TOut;
    out.X_in = {XOut, 1 - XOut};
    fan.speed = if fireStop then 0 else fanSpeed * airFactor;
    fan.V_pu = V_pu;
    fan.headFactor = 1;
    val.TSet = TSupSet;
    val.TMea = TSupAir;
    val.on = fan.y > 0.01;
    yVal = val.y;
    Q = -coi.Q2_flow;
    PFan = fan.P;
    TOutAir = TOut;
    TSupAir = TAir(air_out.p, air_out.h_outflow, air_out.Xi_outflow);
    phi.p = air_out.p;
    phi.T = TSupAir;
    phi.X_w = air_out.Xi_outflow[1];
    phiSup = phi.phi;
    TChwEnt = TWater(chw_in.p, inStream(chw_in.h_outflow));
    TChwLvg = TWater(chw_out.p, chw_out.h_outflow);
    mChw_flow = coi.m1_flow;
    mAir_flow = fan.m_flow;
    yFan = fan.y;
    filterAlarm = airFactor < 0.8;
    fireAlarm = fireStop;
    rhOut.p = out.p;
    rhOut.T = TOut;
    rhOut.X_w = XOut;
    phiOut = rhOut.phi;
    connect(chw_in, val.port_a);
    connect(val.port_b, coi.port_a1);
    connect(coi.port_b1, chw_out);
    connect(out.ports[1], fan.inlet);
    connect(fan.outlet, coi.port_a2);
    connect(coi.port_b2, air_out);
  end AirHandler;

  model DXCoil
    "Variable-speed DX cooling coil whose condensate drains as it forms (Buildings'
    VariableSpeed without the re-evaporation model)"
    // Buildings.Fluid.DXSystems.Cooling.AirSource.VariableSpeed with its parent
    // PartialDXCoolingCoil, except for the water balance. The Buildings evaporation model solves
    // a wet-bulb iteration whenever the compressor is off, which fails to converge from a
    // stopped compressor or at a stage change in a whole-site model; and its switch that turns
    // re-evaporation off leaves a variable without an equation (Buildings 11.1.0). Condensate
    // left on the coil of a sensible-duty CRAC is small, so here it drains immediately.
    extends Buildings.Fluid.DXSystems.Cooling.BaseClasses.PartialDXCoil(
      redeclare Buildings.Fluid.DXSystems.Cooling.BaseClasses.DXCooling dxCoi(
        redeclare final package Medium = Medium,
        redeclare Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.DXCoil datCoi,
        final variableSpeedCoil = true,
        wetCoi(redeclare Buildings.Fluid.DXSystems.BaseClasses.CapacityAirSource coiCap),
        dryCoi(redeclare Buildings.Fluid.DXSystems.BaseClasses.CapacityAirSource coiCap)),
      redeclare final Buildings.Fluid.MixingVolumes.MixingVolumeMoistAir vol(
        prescribedHeatFlowRate = true),
      redeclare Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.DXCoil datCoi,
      use_mCon_flow = false,
      computeReevaporation = false);
    parameter Real minSpeRat(final min = 0, final max = 1) "Minimum speed ratio";
    parameter Real speRatDeaBan = 0.05 "Deadband for minimum speed ratio";
    Modelica.Blocks.Interfaces.RealInput speRat(final unit = "1") "Speed ratio";
    Modelica.Blocks.Interfaces.RealOutput QLat_flow(final quantity = "Power", final unit = "W")
      "Latent heat flow rate";
  protected
    Buildings.Fluid.DXSystems.Cooling.BaseClasses.InputPower pwr;
    Modelica.Blocks.Sources.RealExpression X(final y = XIn[i_x]);
    Modelica.Blocks.Sources.RealExpression h(final y = hIn);
    Modelica.Blocks.Sources.RealExpression p(final y = port_a.p);
    Modelica.Blocks.Logical.Hysteresis deaBan(
      final uLow = minSpeRat - speRatDeaBan / 2,
      final uHigh = minSpeRat + speRatDeaBan / 2);
    Modelica.Blocks.Math.BooleanToInteger onSwi(final integerTrue = 1, final integerFalse = 0);
  equation
    connect(p.y, dxCoi.p);
    connect(dxCoi.SHR, pwr.SHR);
    connect(pwr.QLat_flow, QLat_flow);
    connect(X.y, dxCoi.XEvaIn);
    connect(h.y, dxCoi.hEvaIn);
    connect(T.y, dxCoi.TEvaIn);
    connect(TOut, dxCoi.TConIn);
    connect(dxCoi.mWat_flow, vol.mWat_flow);
    connect(pwr.P, P);
    connect(pwr.QSen_flow, QSen_flow);
    connect(dxCoi.Q_flow, q.Q_flow);
    connect(dxCoi.EIR, pwr.EIR);
    connect(dxCoi.Q_flow, pwr.Q_flow);
    connect(speRat, dxCoi.speRat);
    connect(speRat, deaBan.u);
    connect(onSwi.y, dxCoi.stage);
    connect(deaBan.y, onSwi.u);
  end DXCoil;

  model DXUnit "Direct-expansion CRAC: supply fan and a variable-speed DX coil with an air-cooled condenser"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Rated total cooling (positive)";
    parameter Real COP_nominal = 3;
    parameter Real SHR_nominal = 0.9 "Sensible heat ratio at rated conditions";
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 300;
    parameter Modelica.Units.SI.Temperature TAmbTrip = 319.15
      "Outdoor temperature above which the high-pressure switch stops the compressors";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the unit stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature TRet_start = 303.15 "Return air, fan volume";
    parameter Real xiCom_start = 5 "Compressor controller integrator at start";
    parameter Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.DXCoil dat(
      nSta = 2,
      sta = {
        Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.Stage(
          spe = 1200,
          nomVal = Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.NominalValues(
            Q_flow_nominal = -Q_flow_nominal / 2,
            COP_nominal = COP_nominal * 0.88,
            SHR_nominal = SHR_nominal,
            m_flow_nominal = mAir_flow_nominal),
          perCur = Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.PerformanceCurve(
            capFunT = {1.0046573, -0.0098458, 0.0011934, -0.0002426, -0.0000424, -0.0002981},
            capFunFF = {0.8014888, 0.2708850, -0.0723739},
            EIRFunT = {0.5020269, 0.0044036, -0.0003075, 0.0120302, 0.0003200, -0.0004203},
            EIRFunFF = {1.1811735, -0.2541256, 0.0729521},
            TConInMin = 273.15 + 10,
            TConInMax = 273.15 + 50,
            TEvaInMin = 273.15 + 12,
            TEvaInMax = 273.15 + 26,
            ffMin = 0.2,
            ffMax = 1.2)),
        Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.Stage(
          spe = 2400,
          nomVal = Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.NominalValues(
            Q_flow_nominal = -Q_flow_nominal,
            COP_nominal = COP_nominal,
            SHR_nominal = SHR_nominal,
            m_flow_nominal = mAir_flow_nominal),
          perCur = Buildings.Fluid.DXSystems.Cooling.AirSource.Data.Generic.BaseClasses.PerformanceCurve(
            capFunT = {0.9947501, -0.0138816, 0.0013836, 0.0028728, -0.0000586, -0.0004070},
            capFunFF = {0.7729100, 0.3275723, -0.1004823},
            EIRFunT = {0.4914020, 0.0298133, -0.0007899, -0.0052059, 0.0006735, -0.0006127},
            EIRFunFF = {1.1954441, -0.2809494, 0.0855053},
            TConInMin = 273.15 + 10,
            TConInMax = 273.15 + 50,
            TEvaInMin = 273.15 + 12,
            TEvaInMax = 273.15 + 26,
            ffMin = 0.2,
            ffMax = 1.2))})
      "Two compressor stages; performance curves of a two-stage Lennox unit (Buildings data)";
    Modelica.Fluid.Interfaces.FluidPort_a air_in(redeclare package Medium = MediumA);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput airFactor(unit="1")
      "Fraction of the design air flow the filter still passes; 1 when clean";
    Modelica.Blocks.Interfaces.RealInput capFactor(unit="1")
      "Fraction of the compressor capacity available (a failed compressor); 1 when healthy";
    Modelica.Blocks.Interfaces.RealInput TSupSet(unit="K") "Supply-air set point";
    Modelica.Blocks.Interfaces.RealInput TOut(unit="K") "Outdoor dry bulb at the condenser";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Cooling delivered, sensible plus latent";
    Modelica.Blocks.Interfaces.RealOutput QSen(unit="W") "Sensible cooling delivered";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W") "Compressors and fan";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TSupAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TRetAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mAir_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput speRat(unit="1") "Compressor speed ratio";
    Modelica.Blocks.Interfaces.BooleanOutput compressor1 "First compressor running";
    Modelica.Blocks.Interfaces.BooleanOutput compressor2 "Second compressor running";
    Modelica.Blocks.Interfaces.BooleanOutput highPressure "High head pressure lockout";
    Modelica.Blocks.Interfaces.RealOutput yFan(unit="1") "Fan speed";
    Modelica.Blocks.Interfaces.RealOutput phiSup(unit="1") "Supply-air relative humidity";
    Modelica.Blocks.Interfaces.RealOutput phiRet(unit="1") "Return-air relative humidity";
    Modelica.Blocks.Interfaces.BooleanOutput filterAlarm "Filter differential pressure switch";
    Buildings.Utilities.Psychrometrics.Phi_pTX rhSup;
    Buildings.Utilities.Psychrometrics.Phi_pTX rhRet;
    Fan fan(
      m_flow_nominal = mAir_flow_nominal,
      dp_nominal = 2 * dpAir_nominal,
      T_start = TRet_start,
      VTrip_pu = VTrip_pu);
    DXCoil coi(
      redeclare package Medium = MediumA,
      datCoi = dat,
      minSpeRat = 0.1,
      dp_nominal = dpAir_nominal,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = TRet_start)
      "The coil's air volume keeps its own temperature, so the coil's outlet is a state rather
      than an iteration through the coil model at every compressor change";
    Buildings.Controls.Continuous.LimPID con(
      controllerType = Modelica.Blocks.Types.SimpleController.PI,
      k = 0.1,
      Ti = 120,
      yMax = 1,
      yMin = 0,
      reverseActing = false,
      initType = Modelica.Blocks.Types.Init.NoInit)
      "The unit's own control: compressor speed holds the supply air at its set point";
  initial equation
    // Set here rather than through xi_start, so the start stays a settable FMU parameter.
    con.I.y = xiCom_start;
  equation
    highPressure = TOut > TAmbTrip;
    fan.speed = fanSpeed * airFactor;
    fan.V_pu = V_pu;
    fan.headFactor = 1;
    con.u_s = TSupSet;
    con.u_m = TSupAir;
    speRat = if fan.y > 0.01 and not highPressure then min(con.y, max(capFactor, 0)) else 0;
    coi.speRat = speRat;
    coi.TOut = TOut;
    compressor1 = speRat > 0.1;
    compressor2 = speRat > 0.5;
    Q = -coi.QSen_flow - coi.QLat_flow;
    QSen = -coi.QSen_flow;
    PFan = fan.P;
    P = coi.P + fan.P;
    TSupAir = TAir(air_out.p, air_out.h_outflow, air_out.Xi_outflow);
    TRetAir = TAir(air_in.p, inStream(air_in.h_outflow), inStream(air_in.Xi_outflow));
    mAir_flow = fan.m_flow;
    yFan = fan.y;
    filterAlarm = airFactor < 0.8;
    rhSup.p = air_out.p;
    rhSup.T = TSupAir;
    rhSup.X_w = air_out.Xi_outflow[1];
    phiSup = rhSup.phi;
    rhRet.p = air_in.p;
    rhRet.T = TRetAir;
    rhRet.X_w = inStream(air_in.Xi_outflow[1]);
    phiRet = rhRet.phi;
    connect(air_in, fan.inlet);
    connect(fan.outlet, coi.port_a);
    connect(coi.port_b, air_out);
  end DXUnit;

  model CDU "Coolant distribution unit: the liquid-cooled racks' coolant loop and its facility-water heat exchanger"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Design heat removal";
    parameter Modelica.Units.SI.MassFlowRate mWat_flow_nominal "Facility water at design";
    parameter Modelica.Units.SI.PressureDifference dpWat_nominal = 40000;
    parameter Integer nPumps = 3 "Secondary coolant pumps";
    parameter Modelica.Units.SI.Power PPump_nominal = 2500 "Each secondary pump at design";
    parameter Modelica.Units.SI.HeatCapacity CCoolant = 2e6 "Coolant, manifolds and cold plates";
    parameter Real eps = 0.8 "Heat exchanger effectiveness";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the pumps stop below this supply voltage";
    parameter Modelica.Units.SI.Temperature TSec_start = 298.15;
    parameter Real xiVal_start = 5 "Valve controller integrator at start";
    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealInput QIt(unit="W") "Heat the liquid-cooled IT puts into the coolant";
    Modelica.Blocks.Interfaces.RealInput pumps(unit="1") "Secondary pumps commanded to run";
    Modelica.Blocks.Interfaces.RealInput pumpFactor(unit="1")
      "Fraction of the secondary pumps available; 1 when healthy";
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput TSecSet(unit="K") "Coolant supply set point";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Heat passed to the facility water";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W") "Secondary pumps";
    Modelica.Blocks.Interfaces.RealOutput PPump(unit="W") "Each running secondary pump";
    Modelica.Blocks.Interfaces.RealOutput TSec(unit="K") "Coolant temperature";
    Modelica.Blocks.Interfaces.RealOutput TChwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mChw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput yVal(unit="1");
    Modelica.Blocks.Interfaces.BooleanOutput running;
    Modelica.Thermal.HeatTransfer.Components.HeatCapacitor coolant(
      C = CCoolant, T(start = TSec_start, fixed = true));
    Modelica.Thermal.HeatTransfer.Sources.PrescribedHeatFlow it;
    Modelica.Thermal.HeatTransfer.Sources.PrescribedHeatFlow hxCoolant;
    Buildings.HeatTransfer.Sources.PrescribedHeatFlow hxWater;
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumW,
      V = 0.2,
      m_flow_nominal = mWat_flow_nominal,
      nPorts = 2,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      massDynamics = Modelica.Fluid.Types.Dynamics.SteadyState,
      T_start = 293.15);
    Buildings.Fluid.FixedResistances.PressureDrop res(
      redeclare package Medium = MediumW,
      m_flow_nominal = mWat_flow_nominal,
      dp_nominal = dpWat_nominal / 2);
    CoilValve val(
      m_flow_nominal = mWat_flow_nominal,
      dp_nominal = dpWat_nominal / 2,
      xi_start = xiVal_start);
    Buildings.Fluid.Sensors.MassFlowRate sen(redeclare package Medium = MediumW);
    Real flowFraction(unit="1") "Coolant flow over design";
    Modelica.Units.SI.Temperature TWatIn;
  equation
    running = V_pu > VTrip_pu and pumps > 0.01;
    flowFraction = if running then min(pumps * max(pumpFactor, 0), 1) else 0;
    it.Q_flow = QIt;
    connect(it.port, coolant.port);
    TWatIn = TWater(chw_in.p, inStream(chw_in.h_outflow));
    // Effectiveness-NTU with the facility water as the smaller capacity stream: the coolant
    // flow scales how much of the exchanger's surface the heat reaches.
    Q = eps * flowFraction * max(sen.m_flow, 0) * 4184 * (coolant.T - TWatIn);
    hxCoolant.Q_flow = -Q;
    hxWater.Q_flow = Q;
    connect(hxCoolant.port, coolant.port);
    connect(hxWater.port, vol.heatPort);
    TSec = coolant.T;
    val.TSet = TSecSet;
    val.TMea = TSec;
    val.on = true;
    yVal = val.y;
    PPump = PPump_nominal * flowFraction ^ 3;
    P = nPumps * PPump;
    TChwEnt = TWatIn;
    TChwLvg = vol.T;
    mChw_flow = sen.m_flow;
    connect(chw_in, sen.port_a);
    connect(sen.port_b, val.port_a);
    connect(val.port_b, res.port_a);
    connect(res.port_b, vol.ports[1]);
    connect(vol.ports[2], chw_out);
  end CDU;

  model Hall "Room air volume with IT heat load and the thermal mass of equipment and structure"
    parameter Integer nPorts = 0 "Air connections: one supply and one return per cooling unit";
    parameter Modelica.Units.SI.Volume V "Room air volume";
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.HeatCapacity CMass = 50e6
      "Thermal mass of IT equipment and structure";
    parameter Modelica.Units.SI.ThermalConductance GMass = 50e3 "Air-to-mass heat transfer";
    parameter Modelica.Units.SI.Temperature T_start = 303.15;
    parameter Modelica.Units.SI.Temperature TMass_start = 303.15
      "Literal default: a binding to T_start would make this a calculated, unsettable FMU parameter";
    Modelica.Fluid.Vessels.BaseClasses.VesselFluidPorts_b ports[nPorts](
      redeclare each package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput QIt(unit="W") "IT electrical load, all of it becomes heat";
    Modelica.Blocks.Interfaces.RealOutput TAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TMass(unit="K");
    Modelica.Blocks.Interfaces.RealOutput phi(unit="1") "Relative humidity";
    Modelica.Blocks.Interfaces.RealOutput X(unit="1") "Water vapour mass fraction";
    Buildings.Utilities.Psychrometrics.Phi_pTX rh;
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumA,
      V = V,
      m_flow_nominal = mAir_flow_nominal,
      nPorts = nPorts + 1,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      massDynamics = Modelica.Fluid.Types.Dynamics.SteadyState,
      T_start = T_start);
    Buildings.Fluid.Sources.Boundary_pT ref(redeclare package Medium = MediumA, nPorts = 1)
      "Room pressure reference";
    Modelica.Thermal.HeatTransfer.Sources.PrescribedHeatFlow it;
    Modelica.Thermal.HeatTransfer.Components.HeatCapacitor mass(
      C = CMass, T(start = TMass_start, fixed = true));
    Modelica.Thermal.HeatTransfer.Components.ThermalConductor airToMass(G = GMass);
  equation
    it.Q_flow = QIt;
    TAir = vol.T;
    TMass = mass.T;
    X = vol.Xi[1];
    rh.p = ref.ports[1].p;
    rh.T = vol.T;
    rh.X_w = vol.Xi[1];
    phi = rh.phi;
    connect(it.port, mass.port);
    connect(mass.port, airToMass.port_a);
    connect(airToMass.port_b, vol.heatPort);
    for i in 1:nPorts loop
      connect(ports[i], vol.ports[i]);
    end for;
    connect(ref.ports[1], vol.ports[nPorts + 1]);
  end Hall;

  model ReturnHeader "Common return pipework of a closed loop: every open outlet drains into it"
    parameter Integer nPorts = 0;
    parameter Modelica.Units.SI.Volume V = 1;
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.Temperature T_start = 293.15;
    Modelica.Fluid.Vessels.BaseClasses.VesselFluidPorts_b ports[nPorts](
      redeclare each package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealOutput T(unit="K");
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumW,
      V = V,
      m_flow_nominal = m_flow_nominal,
      nPorts = nPorts,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      massDynamics = Modelica.Fluid.Types.Dynamics.SteadyState,
      T_start = T_start);
  equation
    T = vol.T;
    for i in 1:nPorts loop
      connect(ports[i], vol.ports[i]);
    end for;
  end ReturnHeader;

  model Expansion "Pressure reference for a closed water loop (expansion vessel)"
    parameter Modelica.Units.SI.AbsolutePressure pRef = 300000;
    Modelica.Fluid.Interfaces.FluidPort_a port(redeclare package Medium = MediumW);
    Buildings.Fluid.Sources.Boundary_pT bou(redeclare package Medium = MediumW, p = pRef, nPorts = 1);
  equation
    connect(port, bou.ports[1]);
  end Expansion;

end GwsLib;
