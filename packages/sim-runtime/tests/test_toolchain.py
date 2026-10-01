"""The OpenModelica toolchain check names the fix before a build can fail on missing libraries."""

from __future__ import annotations

from pathlib import Path

from gws_runtime.compiler import LIBRARIES, STOCK_IMAGE, toolchain_problem


def test_a_host_library_tree_with_every_library_passes(tmp_path: Path) -> None:
    for lib in LIBRARIES:
        (tmp_path / lib).mkdir()
    assert toolchain_problem(STOCK_IMAGE, str(tmp_path)) is None


def test_a_host_library_tree_names_what_it_lacks(tmp_path: Path) -> None:
    (tmp_path / "Modelica 4.0.0").mkdir()
    problem = toolchain_problem(STOCK_IMAGE, str(tmp_path))
    assert problem is not None
    lacks = problem.split(" lacks ")[1].split(". ")[0]
    assert lacks == "ModelicaServices 4.0.0, Complex 4.0.0.mo, Buildings 11.1.0"


def test_the_stock_image_without_a_library_tree_is_refused() -> None:
    problem = toolchain_problem(STOCK_IMAGE, "/opt/omlib")
    assert problem is not None and "docker build -t gws-omc:1.25 spikes/phase1" in problem
