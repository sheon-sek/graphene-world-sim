"""Runtime controllers: data-configured blocks declared by World Model ControlBindings.

A block is chosen by the binding's `function` and configured by its `reads`, `drives` and
`parameters`. It models logic real equipment contains (a PID loop, chiller staging, an ATS
sequence). It reads measured values through the signal bus, so a sensor fault reaches it the
way it would reach a real controller, and it writes commands, never physics.

Blocks run once per macro step, after the physical models, in binding id order.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from gws_runtime.values import Value
from gws_world_model.model import ControlBinding, WorldModel


class SignalBus(Protocol):
    """How a controller sees the plant.

    `read` takes an instrument id, a point path or an `<asset>:<signal>` reference and returns
    the value a real controller would see (the instrument's reading where one exists, which
    may be frozen, biased or None when the reading is bad). `write` takes an
    `<asset>:<input>` reference and issues a command, exactly as an operator's command would.
    """

    def read(self, reference: str) -> Value: ...

    def write(self, reference: str, value: Value) -> None: ...


class Block(ABC):
    function: ClassVar[str]
    """The ControlBinding `function` this block implements."""

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        self.binding = binding
        self.doc = doc
        self.overrides: dict[str, Any] = {}
        """Parameters an operator has set on the controller's HMI (hmi.py), which win."""

    def param(self, name: str, default: Any) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        return self.binding.parameters.get(name, default)

    @abstractmethod
    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        """Advance by one macro step ending at `t`."""

    def snapshot(self) -> dict[str, Any]:
        """Internal state (integrators, timers, latches) for a lifecycle snapshot."""
        return {}

    def restore(self, state: Mapping[str, Any]) -> None:
        del state

    def signals(self) -> dict[str, float | bool]:
        """Observable internal values, published as the controller asset's true state under
        `<function>.<name>` (for example `dp_pid.output`)."""
        return {}


BLOCKS: dict[str, type[Block]] = {}


def block(cls: type[Block]) -> type[Block]:
    """Class decorator that registers a block under its `function`."""
    if cls.function in BLOCKS:
        raise ValueError(f"two blocks implement {cls.function!r}")
    BLOCKS[cls.function] = cls
    return cls


def build(
    doc: WorldModel,
    include: Callable[[ControlBinding], bool] = lambda _: True,
) -> tuple[list[Block], list[str]]:
    """Blocks for every control binding the filter accepts, in id order, and the ids of
    bindings whose function has no block (reported, not fatal)."""
    blocks: list[Block] = []
    missing: list[str] = []
    for binding_id in sorted(doc.control_bindings):
        binding = doc.control_bindings[binding_id]
        if not include(binding):
            continue
        cls = BLOCKS.get(binding.function)
        if cls is None:
            missing.append(binding_id)
        else:
            blocks.append(cls(binding, doc))
    return blocks, missing
