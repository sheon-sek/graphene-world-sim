"""The World Model schema: what exists and how it is connected, as versioned data.

Every entity is an immutable pydantic model. A `WorldModel` document is one revision: it is
self-contained (it carries its own ComponentTypes), so a revision always rebuilds the same
simulation. Vocabulary follows CONTEXT.md.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["1"] = "1"

type Scalar = float | int | bool | str
"""A parameter or point value. Numbers carry their unit in the declaring spec."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- enumerations ----------------------------------------------------------------------------


class Domain(StrEnum):
    """What a Connection carries. A Port accepts exactly one domain."""

    POWER = "power"
    CHW = "chw"
    """Chilled water."""
    CW = "cw"
    """Condenser water."""
    AIR = "air"
    WATER = "water"
    """Domestic, makeup and fire water."""
    FUEL = "fuel"
    NET = "net"
    """Control network."""
    CONTROL = "control"
    """A hard-wired control or interlock signal."""
    FIRE = "fire"
    """Fire alarm signalling and life-safety interlocks."""


class Direction(StrEnum):
    """Which way a Port faces in its domain: supplies downstream, takes from upstream, or both."""

    IN = "in"
    OUT = "out"
    BOTH = "both"


class ChangeClass(StrEnum):
    """How an edit reaches a running simulation, from cheapest to most disruptive."""

    LIVE = "live"
    """Applied at the next step."""
    WARM = "warm"
    """Affected partition rebuilt with its state carried over."""
    STRUCTURAL = "structural"
    """Affected partition recompiled in the background and swapped."""
    REINITIALISE = "reinitialise"
    """Whole simulation rebuilt."""

    @property
    def rank(self) -> int:
        return list(ChangeClass).index(self)


class PointClass(StrEnum):
    """What kind of evidence a Point carries, and so what must drive its value."""

    COMMAND = "command"
    FEEDBACK = "feedback"
    PROCESS_VALUE = "process_value"
    EQUIPMENT_STATE = "equipment_state"
    FAULT_ALARM = "fault_alarm"
    ENERGY_INTEGRAL = "energy_integral"
    NETWORK_STATE = "network_state"
    STATIC_METADATA = "static_metadata"
    SUPPORT = "support"


class Access(StrEnum):
    READ = "read"
    READ_WRITE = "read_write"


class FaultKind(StrEnum):
    TRIP = "trip"
    """The asset stops and ignores its command until reset."""
    DEGRADE = "degrade"
    """A capacity or efficiency parameter is scaled down."""
    STUCK = "stuck"
    """An actuator holds its current position."""
    SENSOR = "sensor"
    """An instrument reads wrong: bias, drift, frozen or failed."""
    LOSS_OF_SUPPLY = "loss_of_supply"
    """A source (utility, fuel, water) is no longer available."""
    NETWORK = "network"
    """A network element stops forwarding."""


class AssetCategory(StrEnum):
    EQUIPMENT = "equipment"
    """Physical equipment with behaviour."""
    DEVICE = "device"
    """A sensor, detector, meter or network device: it reports, it does not act on the plant."""
    CONTROLLER = "controller"
    """Control logic that real equipment contains (a plant controller, a PLC sequence)."""
    AGGREGATE = "aggregate"
    """A view that computes KPIs from other points; it has no physical state."""
    SUPPORT = "support"
    """An Ignition-side artefact with no physical counterpart (alarm logic demos, caches)."""


# --- ComponentType ---------------------------------------------------------------------------


class ParameterSpec(_Frozen):
    unit: str | None = None
    """SI or engineering unit (`kW`, `kg/s`, `Pa`, `degC`); None for counts, flags and text."""
    default: Scalar
    min: float | None = None
    max: float | None = None
    change_class: ChangeClass = ChangeClass.WARM
    assumed: bool = False
    """True when the default is an engineering assumption, not a value from the source data."""
    description: str = ""

    @model_validator(mode="after")
    def _default_within_limits(self) -> ParameterSpec:
        if isinstance(self.default, int | float) and not isinstance(self.default, bool):
            if self.min is not None and self.default < self.min:
                raise ValueError(f"default {self.default} below min {self.min}")
            if self.max is not None and self.default > self.max:
                raise ValueError(f"default {self.default} above max {self.max}")
        return self


class PortSpec(_Frozen):
    domain: Domain
    direction: Direction = Direction.BOTH
    description: str = ""


class FaultModeSpec(_Frozen):
    kind: FaultKind
    description: str = ""
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    """Severity and similar settings of the fault itself (for example a capacity fraction)."""


class PointTemplate(_Frozen):
    """One member point every instance of the type exposes."""

    data_type: str
    """Ignition data type (`Float4`, `Boolean`, `Int4`, `String`, …)."""
    unit: str | None = None
    point_class: PointClass
    access: Access = Access.READ
    signal: str | None = None
    """The model signal the point reports or drives, once the behaviour model defines it."""


class ComponentType(_Frozen):
    id: str
    name: str
    category: AssetCategory
    description: str = ""
    behaviour: str | None = None
    """Behaviour model it binds to (`GwsLib.Chiller`); None until one exists."""
    parent: str | None = None
    """ComponentType this one extends (mirrors Ignition UDT inheritance)."""
    ignition_type_id: str | None = None
    """Ignition UDT typeId whose instances are assets of this type, if any."""
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    ports: dict[str, PortSpec] = Field(default_factory=dict)
    fault_modes: dict[str, FaultModeSpec] = Field(default_factory=dict)
    point_template: dict[str, PointTemplate] = Field(default_factory=dict)
    """Member path relative to the asset (`Ports/Port 01/Speed`) to its template."""


# --- Site layout -----------------------------------------------------------------------------


class Floor(_Frozen):
    id: str
    index: int
    """Position in the floor stack, from 0 at the lowest floor."""
    elevation_m: float = 0.0
    height_m: float = 4.5


class Room(_Frozen):
    id: str
    floor: str
    name: str
    kind: str
    """Use of the room: `hall`, `electrical`, `cooling`, `airside`, `water`, `core`, …"""
    x: float
    y: float
    """Plan position of the room's corner, in metres."""
    w: float
    h: float
    fire_zone: str | None = None
    outdoor: bool = False


class Shaft(_Frozen):
    id: str
    name: str
    x: float
    y: float
    floors: tuple[str, ...]
    carries: tuple[Domain, ...]


class Site(_Frozen):
    id: str
    name: str
    floors: tuple[Floor, ...] = ()
    rooms: dict[str, Room] = Field(default_factory=dict)
    shafts: dict[str, Shaft] = Field(default_factory=dict)


# --- Instances and wiring --------------------------------------------------------------------


class Location(_Frozen):
    room: str | None = None
    x: float | None = None
    y: float | None = None
    """Plan position in metres; the floor is the room's."""


class Asset(_Frozen):
    id: str
    """Stable identity. For an exported asset this is its Ignition export path."""
    type: str
    name: str
    parameters: dict[str, Scalar] = Field(default_factory=dict)
    """Overrides of the type's parameter defaults."""
    location: Location = Location()
    system: str = ""
    """Discipline (`Cooling`, `Electrical`, `Airside`, …)."""
    role: str = ""
    exported: bool = True
    """False for a physical thing with no Ignition UDT instance, observed only via other points."""


ROOM_PREFIX = "room:"
"""An Endpoint whose node starts with this names a Room, not an Asset."""


class Endpoint(_Frozen):
    node: str
    """Asset id, or `room:<room id>` for a room (the air it holds, the zone it belongs to)."""
    port: str
    """Port name on the asset's type. For a room, the domain name (`air`, `fire`)."""

    @property
    def is_room(self) -> bool:
        return self.node.startswith(ROOM_PREFIX)

    @property
    def room(self) -> str:
        return self.node.removeprefix(ROOM_PREFIX)

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"


class Connection(_Frozen):
    id: str
    domain: Domain
    source: Endpoint
    """Upstream end: supplies the target."""
    target: Endpoint
    parameters: dict[str, Scalar] = Field(default_factory=dict)
    """Pipe length, cable impedance and similar; typed defaults apply when absent."""
    label: str = ""


class Instrument(_Frozen):
    """A sensor or status reading on an asset, and the path it reports through."""

    id: str
    asset: str
    quantity: str
    """Model signal it measures (`TChwLvg`)."""
    unit: str | None = None
    range_min: float | None = None
    range_max: float | None = None
    accuracy: float | None = None
    """Absolute accuracy in the instrument's unit."""
    reports_via: tuple[str, ...] = ()
    """Asset ids of the devices and network elements between the sensor and Ignition."""


class ControlBinding(_Frozen):
    """The wiring of a controller: what it reads and what it drives."""

    id: str
    controller: str
    """Asset id of the controller."""
    function: str
    """What this binding does (`chw_staging`, `dp_pid`, `rotation`)."""
    reads: tuple[str, ...] = ()
    """Instrument ids or `<asset>:<signal>` references it reads."""
    drives: tuple[str, ...] = ()
    """`<asset>:<input>` references it writes."""
    parameters: dict[str, Scalar] = Field(default_factory=dict)


# --- Point bindings --------------------------------------------------------------------------


class AssetSignal(_Frozen):
    kind: Literal["asset_signal"] = "asset_signal"
    asset: str
    signal: str
    """Member path or model signal name on that asset."""


class InstrumentSource(_Frozen):
    kind: Literal["instrument"] = "instrument"
    instrument: str


class Aggregate(_Frozen):
    """A value computed from other points (a KPI, a hall average)."""

    kind: Literal["aggregate"] = "aggregate"
    function: Literal[
        "sum", "mean", "max", "min", "ratio", "count_true", "first", "product", "elapsed"
    ]
    inputs: tuple[str, ...] = ()
    """Export paths or `<asset>:<signal>` references. `ratio` divides the first by the sum of
    the rest; `product` multiplies the first by the sum of the rest, each in its own unit, and by
    `scale`. `elapsed` takes no inputs: the simulated time since the run began."""
    scale: float = 1.0
    """Multiplier on a `product`, for the units its inputs leave unconverted."""


class StaticValue(_Frozen):
    kind: Literal["static"] = "static"
    value: Scalar | None = None


class Unbound(_Frozen):
    """A point with no source yet. It is served with bad quality until it is bound."""

    kind: Literal["unbound"] = "unbound"
    reason: str = ""


type PointSource = Annotated[
    AssetSignal | InstrumentSource | Aggregate | StaticValue | Unbound,
    Field(discriminator="kind"),
]


class PointBinding(_Frozen):
    """One Point Ignition reads or writes. Ignition compatibility lives here."""

    path: str
    """Export path, byte-identical to the Ignition export."""
    data_type: str
    unit: str | None = None
    point_class: PointClass
    access: Access = Access.READ
    ignition_type_id: str | None = None
    """typeId of the nearest enclosing UDT instance, kept for the Ignition contract checksum."""
    source: PointSource = Unbound()


# --- Operating conditions --------------------------------------------------------------------


class Weather(_Frozen):
    dry_bulb_c: float = 30.0
    wet_bulb_c: float = 25.0
    relative_humidity: float = Field(default=70.0, ge=0, le=100)


class ItLoad(_Frozen):
    design_kw: float = Field(ge=0)
    fraction: float = Field(ge=0, le=1.5)
    """Operating load as a fraction of design."""
    liquid_fraction: float = Field(default=0.0, ge=0, le=1)


class Conditions(_Frozen):
    """Inputs from outside the facility that engineers change live."""

    weather: Weather = Weather()
    it_load: dict[str, ItLoad] = Field(default_factory=dict)
    """Room id of a Data Hall to its IT load."""
    utility_available: bool = True


# --- The document ----------------------------------------------------------------------------


COLLECTIONS: tuple[str, ...] = (
    "component_types",
    "assets",
    "connections",
    "instruments",
    "control_bindings",
    "point_bindings",
)
"""Keyed collections of a WorldModel, in dependency order."""


class WorldModel(_Frozen):
    schema_version: Literal["1"] = SCHEMA_VERSION
    site: Site
    component_types: dict[str, ComponentType] = Field(default_factory=dict)
    assets: dict[str, Asset] = Field(default_factory=dict)
    connections: dict[str, Connection] = Field(default_factory=dict)
    instruments: dict[str, Instrument] = Field(default_factory=dict)
    control_bindings: dict[str, ControlBinding] = Field(default_factory=dict)
    point_bindings: dict[str, PointBinding] = Field(default_factory=dict)
    conditions: Conditions = Conditions()

    @model_validator(mode="after")
    def _keys_match_ids(self) -> WorldModel:
        for name in COLLECTIONS:
            key_field = "path" if name == "point_bindings" else "id"
            for key, item in getattr(self, name).items():
                if getattr(item, key_field) != key:
                    raise ValueError(
                        f"{name}[{key!r}] has {key_field} {getattr(item, key_field)!r}"
                    )
        for key, room in self.site.rooms.items():
            if room.id != key:
                raise ValueError(f"site.rooms[{key!r}] has id {room.id!r}")
        return self

    def canonical_json(self) -> str:
        """Deterministic serialisation: sorted keys, no whitespace. Hash this, not dumps."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()
