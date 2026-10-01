from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import pytest

from gws_runtime import compiler
from gws_runtime.compiler import Build, Partition, compile_partition, fetch_prebuilt


def _partition(name: str = "P_test") -> Partition:
    return Partition(name, "hash", "model M end M;", ("a", "b"), (), (), (), {}, {}, {})


def test_prebuilt_fmus_are_fetched_by_name_and_a_missing_one_is_none(tmp_path: Path) -> None:
    store, cache = tmp_path / "store", tmp_path / "cache"
    store.mkdir()
    with zipfile.ZipFile(store / "P_test.fmu", "w") as z:
        z.writestr("modelDescription.xml", "<fmiModelDescription/>")
    (store / "P_junk.fmu").write_text("not a zip")

    fmu = fetch_prebuilt(_partition(), cache, store.as_uri())
    assert fmu == cache / "P_test.fmu" and (cache / "P_test.json").exists()
    assert fetch_prebuilt(_partition("P_none"), cache, store.as_uri()) is None
    assert fetch_prebuilt(_partition("P_junk"), cache, store.as_uri()) is None
    assert not list(cache.glob("*.part")) and not (cache / "P_junk.fmu").exists()


def test_one_build_runs_per_partition_and_a_second_caller_gets_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    release = threading.Event()

    def build(p: Partition, cache: Path, b: Build) -> tuple[Path, float]:
        calls.append(p.name)
        assert compiler.builds()[0]["partition"] == p.name
        release.wait(5)
        (cache / f"{p.name}.fmu").write_text("fmu")
        return cache / f"{p.name}.fmu", 1.0

    monkeypatch.setattr(compiler, "fetch_prebuilt", lambda p, cache: None)
    monkeypatch.setattr(compiler, "_compile", build)
    results: list[tuple[Path, float]] = []
    threads = [
        threading.Thread(target=lambda: results.append(compile_partition(_partition(), tmp_path)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    while not calls:
        threading.Event().wait(0.01)
    release.set()
    for t in threads:
        t.join(5)
    assert calls == ["P_test"] and sorted(s for _, s in results) == [0.0, 1.0]
    assert compiler.builds() == []


def test_progress_follows_the_files_omc_writes(tmp_path: Path) -> None:
    b = Build("P_test", 2, 1.0)
    compiler._progress(tmp_path, b)
    assert b.phase == "translating"
    sources = tmp_path / "P_test.fmutmp" / "sources"
    sources.mkdir(parents=True)
    for i in range(4):
        (sources / f"f{i}.c").write_text("")
    compiler._progress(tmp_path, b)
    assert (b.phase, b.done) == ("generating", 4)
    (sources / "build_cmake_static").mkdir()
    (sources / "build_cmake_static" / "f0.c.o").write_text("")
    compiler._progress(tmp_path, b)
    assert (b.phase, b.done, b.total) == ("compiling", 1, 4)
    (tmp_path / "P_test.fmutmp" / "binaries" / "linux64").mkdir(parents=True)
    compiler._progress(tmp_path, b)
    assert b.phase == "packaging"


def test_memory_needed_grows_with_the_model() -> None:
    small = compiler.memory_needed_gb(_partition())
    site = Partition("P_site", "h", "x" * compiler.SITE_SOURCE_CHARS, (), (), (), (), {}, {}, {})
    assert small < 2.1 and compiler.memory_needed_gb(site) == 8.9


def test_c_compiles_are_limited_by_the_memory_left_beside_omc() -> None:
    site = Partition("P_site", "h", "x" * compiler.SITE_SOURCE_CHARS, (), (), (), (), {}, {}, {})
    assert compiler.cc_jobs(site, 16.0, cpus=16) == 5
    assert compiler.cc_jobs(site, 8.0, cpus=16) == 1
    assert compiler.cc_jobs(_partition(), 16.0, cpus=4) == 4
    assert compiler.cc_jobs(_partition(), None, cpus=16) == 2
