"""Tests for the pure local heuristics — no GPU, network, or real filesystem."""

from __future__ import annotations

import time

from ultimate_sacrifice.scanner import heuristics as h
from ultimate_sacrifice.scanner.model import Kind, ScanNode

NOW = 1_700_000_000.0
DAY = 86_400.0


def make(path: str, *, size: int = 200 * 1024 * 1024, mtime: float = NOW, is_dir: bool = False) -> ScanNode:
    return ScanNode(
        path=path,
        kind=Kind.DIR if is_dir else Kind.FILE,
        size=size,
        mtime=mtime,
        atime=mtime,
    )


def test_build_artifact_detection():
    assert h.is_build_artifact(r"C:\proj\node_modules\left-pad")
    assert h.is_build_artifact(r"C:\proj\build\out.o")
    assert h.is_build_artifact(r"C:\proj\__pycache__")
    assert not h.is_build_artifact(r"C:\proj\src\main.py")


def test_temp_path_detection():
    assert h.is_temp_path(r"C:\Users\me\AppData\Local\Temp\x")
    assert h.is_temp_path(r"C:\Windows\Temp\a.tmp")
    assert h.is_temp_path(r"C:\Users\me\Downloads\setup.exe")
    assert not h.is_temp_path(r"C:\Users\me\Documents\thesis.docx")


def test_staleness_days():
    old = make("x", mtime=NOW - 200 * DAY)
    assert round(h.staleness_days(old, NOW)) == 200
    fresh = make("y", mtime=NOW)
    assert h.staleness_days(fresh, NOW) == 0.0
    # No timestamp -> 0, not negative.
    assert h.staleness_days(make("z", mtime=0), NOW) == 0.0


def test_category_buckets():
    assert h.category(make(r"C:\p\node_modules\a", is_dir=True)) == "build-artifact"
    assert h.category(make(r"C:\p\app.log")) == "temp-file"
    assert h.category(make(r"C:\Temp\stuff", is_dir=True)) == "temp-cache"
    assert h.category(make(r"C:\dl\ubuntu.iso")) == "archive/installer"
    assert h.category(make(r"C:\docs\report.docx")) == "other"


def test_junk_score_ordering():
    stale_build = make(r"C:\p\node_modules\big", mtime=NOW - 400 * DAY)
    recent_doc = make(r"C:\docs\thesis.docx", mtime=NOW)
    assert h.junk_score(stale_build, NOW) > h.junk_score(recent_doc, NOW)
    # Scores stay within [0, 1].
    assert 0.0 <= h.junk_score(recent_doc, NOW) <= 1.0
    assert 0.0 <= h.junk_score(stale_build, NOW) <= 1.0


def test_recent_pulls_score_down():
    build_recent = make(r"C:\p\build\a", mtime=NOW - 1 * DAY)
    build_stale = make(r"C:\p\build\a", mtime=NOW - 400 * DAY)
    assert h.junk_score(build_stale, NOW) > h.junk_score(build_recent, NOW)


def test_annotate_fills_fields():
    node = make(r"C:\p\node_modules\x", mtime=NOW - 400 * DAY)
    h.annotate(node, NOW)
    assert node.category == "build-artifact"
    assert node.junk_score > 0
    assert "build-artifact" in node.flags
    assert "stale>1y" in node.flags


def test_is_container():
    # A plain "other" directory is a container.
    assert h.is_container(make(r"C:\Dev", is_dir=True))
    assert h.is_container(make(r"C:\Users\me\Videos", is_dir=True))
    # Recognized-junk dirs are NOT containers.
    assert not h.is_container(make(r"C:\p\node_modules", is_dir=True))
    assert not h.is_container(make(r"C:\Temp\cache", is_dir=True))
    # Files are never containers, even "other" ones.
    assert not h.is_container(make(r"C:\docs\report.docx"))


def test_container_flag_in_annotate():
    cont = make(r"C:\Dev", is_dir=True)
    h.annotate(cont, NOW)
    assert "container" in cont.flags

    nm = make(r"C:\p\node_modules", is_dir=True)
    h.annotate(nm, NOW)
    assert "container" not in nm.flags


def test_drill_threshold_scales_and_floors():
    MIB = 1024 * 1024
    GIB = 1024 * MIB
    # Large folder -> ~1% of its size.
    assert h.drill_threshold(40 * GIB) == 40 * GIB // 100
    # Small folder -> floored at 1 MiB so contents still surface.
    assert h.drill_threshold(10 * MIB) == MIB
    assert h.drill_threshold(0) == MIB
    # Monotonic: bigger folder -> threshold never decreases.
    assert h.drill_threshold(GIB) <= h.drill_threshold(100 * GIB)
