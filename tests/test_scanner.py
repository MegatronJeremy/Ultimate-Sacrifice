"""Scanner aggregation tests against a real temp tree."""

from __future__ import annotations

import os

from ultimate_sacrifice.scanner.model import Kind
from ultimate_sacrifice.scanner.walker import Scanner, normalize_root


def test_normalize_root_bare_drive_letter():
    # The bug: "C:" resolves to the cwd on C, not the drive root. normalize_root
    # must map a bare drive letter to the actual root.
    assert normalize_root("C:") == "C:\\"
    assert normalize_root("c:") == "c:\\"
    assert normalize_root("D:") == "D:\\"
    # Whitespace tolerated.
    assert normalize_root("  C:  ") == "C:\\"


def test_normalize_root_passes_through_real_paths(tmp_path):
    p = str(tmp_path)
    assert normalize_root(p) == os.path.abspath(p)
    # An explicit drive root stays a drive root.
    assert normalize_root("C:\\") == os.path.abspath("C:\\")
    assert normalize_root("C:/") == os.path.abspath("C:/")


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


def test_aggregates_and_thresholds(tmp_path):
    root = str(tmp_path)
    big = 2 * 1024 * 1024  # 2 MB
    small = 1024  # 1 KB
    _write(os.path.join(root, "bigdir", "a.bin"), big)
    _write(os.path.join(root, "bigdir", "b.bin"), big)
    _write(os.path.join(root, "small.txt"), small)

    # Threshold 1 MB: bigdir aggregates both files; small.txt is below threshold.
    scanner = Scanner(min_size_bytes=1024 * 1024, top_n=100)
    nodes = scanner.scan(root)

    paths = {os.path.basename(n.path.rstrip("\\/")): n for n in nodes}
    assert "small.txt" not in paths

    # De-dup: the two files live inside bigdir (itself a candidate), so they are
    # collapsed into it — only the shallowest cleanable unit remains, and its size
    # equals the real bytes on disk (no double counting).
    bigdir = next(n for n in nodes if n.path.endswith("bigdir"))
    assert bigdir.size == 2 * big
    assert not any(n.path.endswith(("a.bin", "b.bin")) for n in nodes)
    # Total reported size == actual bytes, not inflated by nested rows.
    assert sum(n.size for n in nodes) == 2 * big


def test_sorted_largest_first_and_top_n(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "a.bin"), 3 * 1024 * 1024)
    _write(os.path.join(root, "b.bin"), 5 * 1024 * 1024)
    _write(os.path.join(root, "c.bin"), 1 * 1024 * 1024)

    scanner = Scanner(min_size_bytes=1024 * 1024, top_n=100)
    nodes = scanner.scan(root)
    # The scan root itself is never a candidate; only the three files remain.
    assert all(os.path.normcase(n.path) != os.path.normcase(root) for n in nodes)
    sizes = [n.size for n in nodes]
    assert sizes == sorted(sizes, reverse=True)
    # The largest *file* is b.bin.
    files = [n for n in nodes if n.kind is Kind.FILE]
    assert max(files, key=lambda n: n.size).path.endswith("b.bin")

    # top_n truncates to the largest N nodes, and flags that it did.
    capped_scanner = Scanner(min_size_bytes=1024 * 1024, top_n=2)
    capped = capped_scanner.scan(root)
    assert len(capped) == 2
    assert capped[0].size >= capped[1].size
    assert capped_scanner.truncated is True


def test_truncated_flag_false_when_under_cap(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "a.bin"), 3 * 1024 * 1024)
    _write(os.path.join(root, "b.bin"), 2 * 1024 * 1024)
    scanner = Scanner(min_size_bytes=1024 * 1024, top_n=1000)
    scanner.scan(root)
    assert scanner.truncated is False


def test_cancel_stops_scan(tmp_path):
    root = str(tmp_path)
    for i in range(5):
        _write(os.path.join(root, f"f{i}.bin"), 2 * 1024 * 1024)
    scanner = Scanner(min_size_bytes=1, top_n=100)
    scanner.cancel = True
    nodes = scanner.scan(root)
    # Cancelled before walking children: no candidates collected.
    assert nodes == []


def test_cancel_sets_progress_cancelled_flag(tmp_path):
    # Issue #5: a cancelled walk reports cancelled=True on the final progress emit.
    root = str(tmp_path)
    for i in range(5):
        _write(os.path.join(root, f"f{i}.bin"), 2 * 1024 * 1024)

    seen = []
    scanner = Scanner(min_size_bytes=1, top_n=100, progress_cb=seen.append)
    scanner.cancel = True
    scanner.scan(root)
    assert seen, "expected at least one progress emit"
    assert seen[-1].cancelled is True
    assert seen[-1].done is True


def test_progress_reports_elapsed(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "a.bin"), 2 * 1024 * 1024)
    seen = []
    Scanner(min_size_bytes=1, top_n=100, progress_cb=seen.append).scan(root)
    # Final emit carries a non-negative elapsed time and is not marked cancelled.
    assert seen[-1].elapsed_s >= 0.0
    assert seen[-1].cancelled is False


def test_drill_reveals_small_files_that_add_up(tmp_path):
    # Issues #3+#4: a folder full of files each BELOW the base threshold shows only as
    # the aggregate folder at a high threshold; drilling in (re-scan at a low threshold)
    # surfaces the individual files.
    from ultimate_sacrifice.scanner import heuristics

    root = str(tmp_path)
    mb = 1024 * 1024
    blob = os.path.join(root, "cache")
    for i in range(20):  # 20 × 3 MB = 60 MB aggregate, each file well under 50 MB
        _write(os.path.join(blob, f"chunk{i}.dat"), 3 * mb)

    # Base scan at 50 MB: no individual file qualifies; only the aggregate folder does.
    base = Scanner(min_size_bytes=50 * mb, top_n=100).scan(root)
    base_files = [n for n in base if n.kind is Kind.FILE]
    assert base_files == []  # small files hidden at the high threshold
    cache_node = next(n for n in base if n.path.endswith("cache"))
    assert cache_node.size == 60 * mb

    # Drill into the cache folder with the auto-scaled threshold.
    threshold = heuristics.drill_threshold(cache_node.size)
    drilled = Scanner(min_size_bytes=threshold, top_n=100).scan(cache_node.path)
    drilled_files = [n for n in drilled if n.kind is Kind.FILE]
    assert len(drilled_files) == 20  # now the individual chunks are visible & actionable


def test_root_excluded_and_junk_ranks_above_container(tmp_path, monkeypatch):
    from ultimate_sacrifice.scanner import heuristics

    # pytest's tmp_path is under %TEMP%; neutralize the temp signal so "Videos"
    # is classified as a plain container rather than a (correct-in-reality) temp dir.
    monkeypatch.setattr(heuristics, "is_temp_path", lambda p: False)

    root = str(tmp_path)
    mb = 1024 * 1024
    # A recognized-junk dir (small) and a larger plain "container" dir.
    _write(os.path.join(root, "proj", "node_modules", "dep.bin"), 2 * mb)
    _write(os.path.join(root, "Videos", "movie.bin"), 8 * mb)
    _write(os.path.join(root, "old.zip"), 3 * mb)

    nodes = Scanner(min_size_bytes=1 * mb, top_n=100).scan(root)
    by_name = {os.path.basename(n.path.rstrip("\\/")): n for n in nodes}

    # Root and intermediate container "proj" behavior:
    assert all(os.path.normcase(n.path) != os.path.normcase(root) for n in nodes)

    # node_modules present and NOT a container; Videos present and IS a container.
    assert not heuristics.is_container(by_name["node_modules"])
    assert heuristics.is_container(by_name["Videos"])
    assert "old.zip" in by_name  # large loose file surfaces

    # Ranking: every non-container sorts before every container, even though the
    # container (Videos, 8 MB) is larger than the junk dir (node_modules, 2 MB).
    kinds = [heuristics.is_container(n) for n in nodes]
    assert kinds == sorted(kinds)  # False (actionable) before True (containers)
    first_container = kinds.index(True)
    assert not any(kinds[:first_container])  # nothing actionable after a container


def _node(path, size, is_dir=True):
    from ultimate_sacrifice.scanner.model import Kind, ScanNode

    n = ScanNode(path=path, kind=Kind.DIR if is_dir else Kind.FILE, size=size)
    return n


def test_dedup_drops_nested_actionable(monkeypatch):
    from ultimate_sacrifice.scanner import heuristics, walker

    # Everything build-artifact -> actionable. A parent Build and its nested children.
    nodes = [
        _node(r"C:\proj\build", 100),
        _node(r"C:\proj\build\Win64", 100),
        _node(r"C:\proj\build\Win64\x64", 100),
        _node(r"C:\proj\build\out.o", 90, is_dir=False),
    ]
    kept = walker.deduplicate_nested(nodes)
    kept_paths = {n.path for n in kept}
    # Only the shallowest survives; the rest are the same bytes double-counted.
    assert kept_paths == {r"C:\proj\build"}


def test_dedup_keeps_actionable_inside_container(monkeypatch):
    from ultimate_sacrifice.scanner import heuristics, walker

    # A container (C:\Dev) with a deletable node_modules nested inside it, plus a
    # deeper dep dir under node_modules that must be collapsed away.
    monkeypatch.setattr(heuristics, "is_temp_path", lambda p: False)
    nodes = [
        _node(r"C:\Dev", 500),                              # container
        _node(r"C:\Dev\proj\node_modules", 200),            # actionable, keep
        _node(r"C:\Dev\proj\node_modules\dep", 200),        # actionable, drop (nested)
    ]
    kept = {n.path for n in walker.deduplicate_nested(nodes)}
    assert r"C:\Dev" in kept                       # container survives independently
    assert r"C:\Dev\proj\node_modules" in kept     # shallowest actionable survives
    assert r"C:\Dev\proj\node_modules\dep" not in kept  # nested dup dropped


def test_dedup_reclaimable_total_not_double_counted(tmp_path):
    # Real scan: the reported sizes of the de-duplicated candidates must not exceed
    # the actual bytes on disk (which nested rows would blow past).
    root = str(tmp_path)
    mb = 1024 * 1024
    _write(os.path.join(root, "proj", "build", "win64", "out.bin"), 5 * mb)
    nodes = Scanner(min_size_bytes=1 * mb, top_n=100).scan(root)
    # The single 5 MB file lives under build/win64; only ONE actionable root should
    # remain in that chain, so the summed candidate size stays ~5 MB, not 15-20 MB.
    from ultimate_sacrifice.scanner import heuristics

    actionable = [n for n in nodes if not heuristics.is_container(n)]
    # No actionable node is an ancestor of another actionable node.
    paths = [os.path.normcase(n.path) for n in actionable]
    for p in paths:
        assert not any(p != q and p.startswith(q + os.sep) for q in paths)
