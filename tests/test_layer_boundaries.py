"""Import rules that keep the layers of ADR-0001 apart.

The OPC UA server must not know the simulator exists, and the World Model must not know about
behaviour, so neither may import the packages above or beside it.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN: dict[str, set[str]] = {
    "gws_world_model": {"gws_runtime", "gws_opcua", "gws_api"},
    "gws_runtime": {"gws_opcua", "gws_api"},
    "gws_opcua": {"gws_world_model", "gws_runtime", "gws_api"},
}


def _package_dir(package: str) -> Path:
    matches = list(ROOT.glob(f"packages/*/src/{package}"))
    assert len(matches) == 1, f"expected one source dir for {package}, found {matches}"
    return matches[0]


def _imported_roots(source: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_package_does_not_import_forbidden_layers(package: str) -> None:
    violations = [
        f"{path.relative_to(ROOT)} imports {name}"
        for path in sorted(_package_dir(package).rglob("*.py"))
        for name in sorted(_imported_roots(path) & FORBIDDEN[package])
    ]
    assert not violations, "\n".join(violations)
