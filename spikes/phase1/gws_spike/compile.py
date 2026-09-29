"""Compile a generated model to an FMI 2.0 FMU with OpenModelica in a container.

The image comes from spikes/phase1/Dockerfile (OpenModelica 1.25 + MSL 4.0.0 + Buildings
11.1.0). Set GWS_OMC_IMAGE to use another image and GWS_OMLIB to use a host library tree
mounted at the same path instead of the one baked into the image.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

IMAGE = os.environ.get("GWS_OMC_IMAGE", "gws-omc:1.25")
LIBDIR = os.environ.get("GWS_OMLIB", "/opt/omlib")
GWSLIB = Path(__file__).resolve().parent.parent / "modelica" / "GwsLib" / "package.mo"

SCRIPT = """setModelicaPath("{libdir}");
loadModel(Modelica, {{"4.0.0"}}); getErrorString();
loadModel(Buildings, {{"11.1.0"}}); getErrorString();
loadFile("{gwslib}"); getErrorString();
loadFile("{model}.mo"); getErrorString();
setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
buildModelFMU({model}, version="2.0", fmuType="me_cs", fileNamePrefix="{model}",
  platforms={{"static"}}); getErrorString();
"""


def compile_fmu(model_dir: Path, model: str) -> tuple[Path, float]:
    """Build <model_dir>/<model>.fmu and return its path and the wall-clock seconds taken."""
    model_dir = model_dir.resolve()
    (model_dir / "build.mos").write_text(SCRIPT.format(libdir=LIBDIR, gwslib=GWSLIB, model=model))
    mounts = {str(model_dir), str(GWSLIB.parent)}
    if not LIBDIR.startswith("/opt/"):
        mounts.add(LIBDIR)
        # A host library tree may hold symlinks to checkouts elsewhere; mount their targets.
        mounts.update(str(p.resolve()) for p in Path(LIBDIR).iterdir() if p.is_symlink())
    cmd = ["docker", "run", "--rm", "-w", str(model_dir)]
    for m in sorted(mounts):
        cmd += ["-v", f"{m}:{m}"]
    cmd += [IMAGE, "omc", "build.mos"]
    fmu = model_dir / f"{model}.fmu"
    fmu.unlink(missing_ok=True)
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = time.monotonic() - t0
    (model_dir / "build.log").write_text(result.stdout + result.stderr)
    if result.returncode != 0 or not fmu.exists():
        raise RuntimeError(f"omc failed for {model}; see {model_dir / 'build.log'}")
    return fmu, elapsed
