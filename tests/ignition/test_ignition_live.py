"""The Ignition integration test, when a gateway may be started (`GWS_IGNITION_LIVE=1`)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(
    os.environ.get("GWS_IGNITION_LIVE") != "1",
    reason="starts a local Ignition gateway: set GWS_IGNITION_LIVE=1 (tests/ignition/README.md)",
)
def test_ignition_reads_writes_and_alarms_on_the_simulator() -> None:
    done = subprocess.run(
        [sys.executable, str(HERE / "live.py")], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
