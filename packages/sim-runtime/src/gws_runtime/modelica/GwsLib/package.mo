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
    connect(inlet, sen.port_a);
    connect(sen.port_b, res.port_a);
    connect(res.port_b, vol.ports[1]);
    connect(vol.ports[2], outlet);
  end BufferTank;

  model FanCoil "Chilled-water fan coil: dry coil plus supply fan"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Design cooling (positive)";
    parameter Modelica.Units.SI.MassFlowRate mWat_flow_nominal;
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dpWat_nominal = 40000;
    parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 300;
    parameter Modelica.Units.SI.Temperature TWatEnt_nominal = 287.15;
    parameter Modelica.Units.SI.Temperature TAirEnt_nominal = 305.15;
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: the fan stops below this supply voltage";
    parameter Modelica.Units.SI.Temperature TRet_start = 303.15 "Return air, fan volume";
    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_a air_in(redeclare package Medium = MediumA);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage, per unit";
    Modelica.Blocks.Interfaces.RealInput airFactor(unit="1")
      "Fraction of the design air flow the filter still passes; 1 when clean";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Cooling delivered (positive)";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TSupAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TRetAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwEnt(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput mChw_flow(unit="kg/s");
    Modelica.Blocks.Interfaces.RealOutput mAir_flow(unit="kg/s");
    Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU coi(
      redeclare package Medium1 = MediumW,
      redeclare package Medium2 = MediumA,
      m1_flow_nominal = mWat_flow_nominal,
      m2_flow_nominal = mAir_flow_nominal,
      dp1_nominal = dpWat_nominal,
      dp2_nominal = dpAir_nominal,
      Q_flow_nominal = Q_flow_nominal,
      configuration = Buildings.Fluid.Types.HeatExchangerConfiguration.CounterFlow,
      T_a1_nominal = TWatEnt_nominal,
      T_a2_nominal = TAirEnt_nominal);
    Fan fan(
      m_flow_nominal = mAir_flow_nominal,
      dp_nominal = 2 * dpAir_nominal,
      T_start = TRet_start,
      VTrip_pu = VTrip_pu);
  equation
    fan.speed = fanSpeed * airFactor;
    fan.V_pu = V_pu;
    fan.headFactor = 1;
    Q = coi.Q1_flow;
    PFan = fan.P;
    TSupAir = TAir(air_out.p, air_out.h_outflow, air_out.Xi_outflow);
    TRetAir = TAir(air_in.p, inStream(air_in.h_outflow), inStream(air_in.Xi_outflow));
    TChwEnt = TWater(chw_in.p, inStream(chw_in.h_outflow));
    TChwLvg = TWater(chw_out.p, chw_out.h_outflow);
    mChw_flow = coi.m1_flow;
    mAir_flow = fan.m_flow;
    connect(chw_in, coi.port_a1);
    connect(coi.port_b1, chw_out);
    connect(air_in, fan.inlet);
    connect(fan.outlet, coi.port_a2);
    connect(coi.port_b2, air_out);
  end FanCoil;

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
