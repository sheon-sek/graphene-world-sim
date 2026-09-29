"""OPC UA exposure of simulated points (ADR-0003).

Knows only points, values, quality and timestamps. It must not import the runtime or the
World Model, so the simulator stays replaceable behind this interface.
"""
