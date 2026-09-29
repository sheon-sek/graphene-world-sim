from __future__ import annotations

import pytest

from gws_world_model.model import (
    Asset,
    AssetCategory,
    AssetSignal,
    ChangeClass,
    ComponentType,
    Connection,
    Direction,
    Domain,
    Endpoint,
    Floor,
    Location,
    ParameterSpec,
    PointBinding,
    PointClass,
    PortSpec,
    Room,
    Site,
    WorldModel,
)


def small_world() -> WorldModel:
    """A pump feeding a chiller in one plant room, with one point."""
    pump = ComponentType(
        id="Pump",
        name="Pump",
        category=AssetCategory.EQUIPMENT,
        parameters={
            "m_flow_nominal": ParameterSpec(unit="kg/s", default=30.0, min=0, assumed=True),
            "speed_setpoint": ParameterSpec(
                unit="1", default=1.0, min=0, max=1, change_class=ChangeClass.LIVE
            ),
        },
        ports={
            "chw_in": PortSpec(domain=Domain.CHW, direction=Direction.IN),
            "chw_out": PortSpec(domain=Domain.CHW, direction=Direction.OUT),
            "power_in": PortSpec(domain=Domain.POWER, direction=Direction.IN),
        },
    )
    chiller = ComponentType(
        id="Chiller",
        name="Chiller",
        category=AssetCategory.EQUIPMENT,
        parameters={
            "capacity": ParameterSpec(
                unit="kW", default=1000.0, min=0, change_class=ChangeClass.WARM
            )
        },
        ports={
            "chw_in": PortSpec(domain=Domain.CHW, direction=Direction.IN),
            "chw_out": PortSpec(domain=Domain.CHW, direction=Direction.OUT),
        },
    )
    return WorldModel(
        site=Site(
            id="test",
            name="Test site",
            floors=(Floor(id="Roof", index=0),),
            rooms={
                "PLANT": Room(
                    id="PLANT", floor="Roof", name="Plant", kind="cooling", x=0, y=0, w=10, h=10
                )
            },
        ),
        component_types={"Pump": pump, "Chiller": chiller},
        assets={
            "P1": Asset(id="P1", type="Pump", name="P1", location=Location(room="PLANT")),
            "C1": Asset(id="C1", type="Chiller", name="C1", location=Location(room="PLANT")),
        },
        connections={
            "chw:P1->C1": Connection(
                id="chw:P1->C1",
                domain=Domain.CHW,
                source=Endpoint(node="P1", port="chw_out"),
                target=Endpoint(node="C1", port="chw_in"),
            )
        },
        point_bindings={
            "Chiller/C1/Power": PointBinding(
                path="Chiller/C1/Power",
                data_type="Float4",
                unit="kW",
                point_class=PointClass.PROCESS_VALUE,
                source=AssetSignal(asset="C1", signal="P"),
            )
        },
    )


@pytest.fixture
def world() -> WorldModel:
    return small_world()
