within ;
package GwsLib "Equipment wrappers for the Phase 1 spike: one Modelica model per ComponentType"
  extends Modelica.Icons.Package;

  package MediumW = Buildings.Media.Water "Water";
  package MediumA = Buildings.Media.Air "Moist air";

  model Chiller "Electric chiller with manufacturer EIR curves and an evaporator flow switch"
    parameter Buildings.Fluid.Chillers.Data.ElectricEIR.Generic per
      "Performance record (manufacturer curves)";
    parameter Modelica.Units.SI.PressureDifference dpChw_nominal = 60000;
    parameter Modelica.Units.SI.PressureDifference dpCw_nominal = 60000;
    parameter Real flowSwitchFraction = 0.2
      "Chiller runs only while evaporator flow is above this fraction of nominal";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: drives stop below this supply voltage";
    parameter Modelica.Units.SI.Temperature TChw_start = 280.15;
    parameter Modelica.Units.SI.Temperature TCw_start = 298.15;

    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_a cw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b cw_out(redeclare package Medium = MediumW);

    Modelica.Blocks.Interfaces.BooleanInput enable "Run command";
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage at the motor terminals, per unit";
    Modelica.Blocks.Interfaces.RealInput TChwSet(unit="K") "Leaving CHW set point";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W") "Electrical power";
    Modelica.Blocks.Interfaces.RealOutput QEva(unit="W") "Cooling delivered (positive)";
    Modelica.Blocks.Interfaces.RealOutput TChwLvg(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TCwLvg(unit="K");
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
  equation
    running = enable and V_pu > VTrip_pu and mChw.m_flow > flowSwitchFraction * per.mEva_flow_nominal;
    chi.on = running;
    chi.TSet = TChwSet;
    P = chi.P;
    QEva = -chi.QEva_flow;
    TChwLvg = chi.vol2.T;
    TCwLvg = chi.vol1.T;
    connect(chw_in, mChw.port_a);
    connect(mChw.port_b, chi.port_a2);
    connect(chi.port_b2, chw_out);
    connect(cw_in, chi.port_a1);
    connect(chi.port_b1, cw_out);
  end Chiller;

  model Pump "Variable-speed pump: flow emerges from its curve and the loop resistance"
    replaceable package Medium = MediumW;
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal;
    parameter Modelica.Units.SI.Temperature T_start = 293.15;
    parameter Modelica.Units.SI.Density rho = 1000 "Density used to size the curve";
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: drives stop below this supply voltage";

    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = Medium);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = Medium);
    Modelica.Blocks.Interfaces.RealInput speed(unit="1") "Speed command, 0 to 1";
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage at the motor terminals, per unit";
    Modelica.Blocks.Interfaces.RealOutput P(unit="W");
    Modelica.Blocks.Interfaces.RealOutput m_flow(unit="kg/s");

    Buildings.Fluid.Movers.SpeedControlled_y mov(
      redeclare package Medium = Medium,
      per(pressure(
        V_flow = m_flow_nominal / rho * {0, 1, 1.5},
        dp = dp_nominal * {1.5, 1, 0.5})),
      use_inputFilter = false,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
  equation
    mov.y = if V_pu > VTrip_pu then speed else 0;
    P = mov.P;
    m_flow = mov.m_flow;
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
    Buildings.Fluid.Actuators.Valves.TwoWayLinear val(
      redeclare package Medium = MediumW,
      m_flow_nominal = m_flow_nominal,
      dpValve_nominal = dp_nominal,
      use_inputFilter = false);
  equation
    val.y = position;
    connect(inlet, val.port_a);
    connect(val.port_b, outlet);
  end Valve;

  model CoolingTower "Open cooling tower with variable-speed fan (York correlation)"
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dp_nominal = 30000;
    parameter Modelica.Units.SI.Power PFan_nominal = 15000;
    parameter Real VTrip_pu = 0.85 "Undervoltage trip: drives stop below this supply voltage";
    parameter Modelica.Units.SI.Temperature T_start = 298.15;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage at the motor terminals, per unit";
    Modelica.Blocks.Interfaces.RealInput TWetBulb(unit="K") "Outdoor wet-bulb temperature";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TLvg(unit="K");
    Buildings.Fluid.HeatExchangers.CoolingTowers.YorkCalc tow(
      redeclare package Medium = MediumW,
      m_flow_nominal = m_flow_nominal,
      dp_nominal = dp_nominal,
      PFan_nominal = PFan_nominal,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
  equation
    tow.y = if V_pu > VTrip_pu then fanSpeed else 0;
    tow.TAir = TWetBulb;
    PFan = tow.PFan;
    TLvg = tow.TLvg;
    connect(inlet, tow.port_a);
    connect(tow.port_b, outlet);
  end CoolingTower;

  model BufferTank "Well-mixed chilled-water buffer tank"
    parameter Modelica.Units.SI.Volume V = 10;
    parameter Modelica.Units.SI.MassFlowRate m_flow_nominal;
    parameter Modelica.Units.SI.Temperature T_start = 280.15;
    Modelica.Fluid.Interfaces.FluidPort_a inlet(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b outlet(redeclare package Medium = MediumW);
    Modelica.Blocks.Interfaces.RealOutput T(unit="K");
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumW,
      V = V,
      m_flow_nominal = m_flow_nominal,
      nPorts = 2,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
  equation
    T = vol.T;
    connect(inlet, vol.ports[1]);
    connect(vol.ports[2], outlet);
  end BufferTank;

  model FanCoil "Chilled-water fan-coil: dry coil plus supply fan"
    parameter Modelica.Units.SI.HeatFlowRate Q_flow_nominal "Design cooling (positive)";
    parameter Modelica.Units.SI.MassFlowRate mWat_flow_nominal;
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.PressureDifference dpWat_nominal = 40000;
    parameter Modelica.Units.SI.PressureDifference dpAir_nominal = 300;
    parameter Modelica.Units.SI.Temperature TRet_start = 297.15 "Return air, fan volume";
    parameter Modelica.Units.SI.Temperature TSup_start = 288.15 "Supply air sensor";
    Modelica.Fluid.Interfaces.FluidPort_a chw_in(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_b chw_out(redeclare package Medium = MediumW);
    Modelica.Fluid.Interfaces.FluidPort_a air_in(redeclare package Medium = MediumA);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput fanSpeed(unit="1");
    Modelica.Blocks.Interfaces.RealInput V_pu(unit="1") "Supply voltage at the motor terminals, per unit";
    Modelica.Blocks.Interfaces.RealOutput Q(unit="W") "Cooling delivered (positive)";
    Modelica.Blocks.Interfaces.RealOutput PFan(unit="W");
    Modelica.Blocks.Interfaces.RealOutput TSupAir(unit="K");
    Buildings.Fluid.HeatExchangers.DryCoilEffectivenessNTU coi(
      redeclare package Medium1 = MediumW,
      redeclare package Medium2 = MediumA,
      m1_flow_nominal = mWat_flow_nominal,
      m2_flow_nominal = mAir_flow_nominal,
      dp1_nominal = dpWat_nominal,
      dp2_nominal = dpAir_nominal,
      Q_flow_nominal = Q_flow_nominal,
      configuration = Buildings.Fluid.Types.HeatExchangerConfiguration.CounterFlow,
      T_a1_nominal = 280.15,
      T_a2_nominal = 300.15);
    Fan fan(m_flow_nominal = mAir_flow_nominal, dp_nominal = 2 * dpAir_nominal, T_start = TRet_start);
    Buildings.Fluid.Sensors.TemperatureTwoPort TSup(
      redeclare package Medium = MediumA, m_flow_nominal = mAir_flow_nominal, T_start = TSup_start);
  equation
    fan.speed = fanSpeed;
    fan.V_pu = V_pu;
    Q = coi.Q1_flow;
    PFan = fan.P;
    TSupAir = TSup.T;
    connect(chw_in, coi.port_a1);
    connect(coi.port_b1, chw_out);
    connect(air_in, fan.inlet);
    connect(fan.outlet, coi.port_a2);
    connect(coi.port_b2, TSup.port_a);
    connect(TSup.port_b, air_out);
  end FanCoil;

  model DataHall "Data hall air volume with IT heat load and equipment thermal mass"
    parameter Modelica.Units.SI.Volume V "Room air volume";
    parameter Modelica.Units.SI.MassFlowRate mAir_flow_nominal;
    parameter Modelica.Units.SI.HeatCapacity CMass = 50e6
      "Thermal mass of IT equipment and structure";
    parameter Modelica.Units.SI.ThermalConductance GMass = 50e3
      "Air-to-mass heat transfer";
    parameter Modelica.Units.SI.PressureDifference dpDuct_nominal = 300;
    parameter Modelica.Units.SI.Temperature T_start = 297.15;
    parameter Modelica.Units.SI.Temperature TMass_start = 297.15
      "Literal default: a binding to T_start would make this a calculated, unsettable FMU parameter";
    Modelica.Fluid.Interfaces.FluidPort_a air_in(redeclare package Medium = MediumA);
    Modelica.Fluid.Interfaces.FluidPort_b air_out(redeclare package Medium = MediumA);
    Modelica.Blocks.Interfaces.RealInput QIt(unit="W") "IT electrical load, all becomes heat";
    Modelica.Blocks.Interfaces.RealOutput TAir(unit="K");
    Modelica.Blocks.Interfaces.RealOutput TMass(unit="K");
    Buildings.Fluid.MixingVolumes.MixingVolume vol(
      redeclare package Medium = MediumA,
      V = V,
      m_flow_nominal = mAir_flow_nominal,
      nPorts = 3,
      energyDynamics = Modelica.Fluid.Types.Dynamics.FixedInitial,
      T_start = T_start);
    Buildings.Fluid.FixedResistances.PressureDrop duct(
      redeclare package Medium = MediumA,
      m_flow_nominal = mAir_flow_nominal,
      dp_nominal = dpDuct_nominal);
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
    connect(air_in, duct.port_a);
    connect(duct.port_b, vol.ports[1]);
    connect(vol.ports[2], air_out);
    connect(ref.ports[1], vol.ports[3]);
  end DataHall;

  model Expansion "Pressure reference for a closed water loop (expansion vessel)"
    Modelica.Fluid.Interfaces.FluidPort_a port(redeclare package Medium = MediumW);
    Buildings.Fluid.Sources.Boundary_pT bou(redeclare package Medium = MediumW, nPorts = 1);
  equation
    connect(port, bou.ports[1]);
  end Expansion;

end GwsLib;
