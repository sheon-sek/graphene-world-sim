"""Build the FMUs of every preset, for the prebuilt store (`compiler.PREBUILT`).

    uv run python -m gws_api.prebuild data/graphene --list new.txt

Partitions already in the cache or the prebuilt store are skipped; the names of the ones
compiled here are written to `--list`, for CI to upload to the `fmu-cache` release.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gws_api.runtime import _presets
from gws_runtime.compiler import CACHE, cached, compile_partition, plan
from gws_world_model.importers.graphene import Sources, build


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--list", type=Path, required=True)
    args = parser.parse_args(argv)
    doc = build(Sources.read(args.data))
    new: list[str] = []
    for preset in _presets(doc):
        for p in plan(doc, preset.scope).partitions:
            if cached(p) is not None:
                print(f"{preset.id}: {p.name} cached")
                continue
            fmu, seconds = compile_partition(p)
            print(
                f"{preset.id}: {p.name} {'compiled' if seconds else 'prebuilt'} ({seconds:.0f} s)"
            )
            if seconds:
                new += [str(fmu), str(fmu.with_suffix(".json"))]
    args.list.write_text("".join(f"{f}\n" for f in new), encoding="utf-8")
    print(f"{len(new) // 2} new FMUs in {CACHE}")


if __name__ == "__main__":
    main()
