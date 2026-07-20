"""Tests for the pure Disk Advisor core — no I/O, no AI, no real filesystem."""

from __future__ import annotations

from ultimate_sacrifice.analysis import advisor as A
from ultimate_sacrifice.analysis.advisor import Safety, analyze, group_candidates, rank_groups
from ultimate_sacrifice.scanner import heuristics
from ultimate_sacrifice.scanner.model import Kind, ScanNode

NOW = 1_700_000_000.0
DAY = 86_400.0
GIB = 1024 ** 3


def node(path, gb, *, is_dir=True, mtime=NOW):
    n = ScanNode(
        path=path,
        kind=Kind.DIR if is_dir else Kind.FILE,
        size=int(gb * GIB),
        mtime=mtime,
        atime=mtime,
    )
    heuristics.annotate(n, NOW)
    return n


def test_cleanup_class_buckets():
    assert A.cleanup_class(node(r"C:\proj\node_modules", 1)) == "dependency-dirs"
    assert A.cleanup_class(node(r"C:\proj\build", 1)) == "build-output"
    assert A.cleanup_class(node(r"C:\proj\Intermediate", 1)) == "build-output"
    assert A.cleanup_class(node(r"C:\Users\me\AppData\Local\Temp\x", 1)) == "caches"
    assert A.cleanup_class(node(r"C:\dl\ubuntu.iso", 1, is_dir=False)) == "installers-archives"
    assert A.cleanup_class(node(r"C:\stuff\huge.bin", 1, is_dir=False)) == "large-loose-files"


def test_project_root_grouping():
    # Two UE projects, each with build output nested deep — should form 2 groups.
    nodes = [
        node(r"C:\Dev\U57\Engine\Intermediate\Build\Win64", 90),
        node(r"C:\Dev\U56\Projects\Env\Intermediate\Build", 30),
        node(r"C:\Dev\U57\Engine\Intermediate\Build\x64", 5),  # same project as first
    ]
    groups = group_candidates(nodes)
    build_groups = [g for g in groups if g.cleanup_class == "build-output"]
    assert len(build_groups) == 2  # U57 and U56 are distinct projects
    # U57's group aggregates both of its build dirs.
    u57 = next(g for g in build_groups if "U57" in g.key)
    assert u57.item_count == 2
    assert u57.total_bytes == int(95 * GIB)


def test_grouping_no_double_count():
    nodes = [
        node(r"C:\a\node_modules", 2),
        node(r"C:\b\build", 3),
        node(r"C:\c\cache", 1),
        node(r"C:\d\big.iso", 4, is_dir=False),
    ]
    groups = group_candidates(nodes)
    # Every actionable byte lands in exactly one group; sum is conserved.
    assert sum(g.total_bytes for g in groups) == sum(n.size for n in nodes)
    assert sum(g.item_count for g in groups) == len(nodes)


def test_containers_excluded_from_groups():
    # A plain "other" directory is a container (context, not a target).
    container = node(r"C:\Dev", 500)
    assert heuristics.is_container(container)
    groups = group_candidates([container, node(r"C:\Dev\proj\node_modules", 2)])
    all_paths = [p for g in groups for p in g.paths]
    assert r"C:\Dev" not in all_paths
    assert r"C:\Dev\proj\node_modules" in all_paths


def test_safety_classification():
    groups = {g.cleanup_class: g for g in group_candidates([
        node(r"C:\a\build", 1),
        node(r"C:\b\cache", 1),
        node(r"C:\c\setup.exe", 1, is_dir=False),
    ])}
    assert groups["build-output"].safety is Safety.SAFE
    assert groups["caches"].safety is Safety.SAFE
    assert groups["installers-archives"].safety is Safety.REVIEW


def test_ranking_biggest_safe_first():
    groups = group_candidates([
        node(r"C:\a\setup.exe", 50, is_dir=False),   # installers, REVIEW, 50 GB
        node(r"C:\b\build", 40),                      # build-output, SAFE, 40 GB
        node(r"C:\c\cache", 5),                        # caches, SAFE, 5 GB
    ])
    ranked = rank_groups(groups, NOW)
    # The 40 GB SAFE build beats the 50 GB REVIEW installer (safety-weighted),
    # and both beat the 5 GB cache.
    assert ranked[0].cleanup_class == "build-output"
    assert ranked[-1].cleanup_class == "caches"


def test_disk_summary_percentages():
    nodes = [
        node(r"C:\a\build", 3),
        node(r"C:\b\cache", 1),
    ]
    s = A.disk_summary(nodes, r"C:\a", with_drive_usage=False)
    assert s.reclaimable_bytes == int(4 * GIB)
    assert s.candidate_count == 2
    pcts = {cls: pct for cls, _b, pct in s.by_class}
    assert round(pcts["build-output"]) == 75
    assert round(pcts["caches"]) == 25


def test_analyze_end_to_end():
    nodes = [
        node(r"C:\Dev\U57\Engine\Intermediate\Build", 90),
        node(r"C:\Users\me\AppData\Local\Temp\big", 10),
        node(r"C:\dl\installer.exe", 20, is_dir=False),
        node(r"C:\Dev", 500),  # container — excluded
    ]
    plan = analyze(nodes, "C:\\", NOW, with_drive_usage=False)
    assert plan.summary.reclaimable_bytes == int(120 * GIB)  # container excluded
    assert plan.groups[0].cleanup_class == "build-output"    # biggest safe first
    assert plan.narrative == ""  # no AI in the pure core
    assert plan.narrative_source == "none"


def test_empty_scan():
    plan = analyze([], "C:\\", NOW, with_drive_usage=False)
    assert plan.groups == []
    assert plan.summary.reclaimable_bytes == 0


# ---- AI narrative layer (pure parts) ----


def test_advice_prompt_fences_data_and_lists_groups():
    from ultimate_sacrifice.analysis.advisor_prompt import build_advice_prompt

    nodes = [
        node(r"C:\Dev\U57\Engine\Intermediate\Build", 90),
        node(r"C:\dl\installer.exe", 20, is_dir=False),
    ]
    plan = analyze(nodes, "C:\\", NOW, with_drive_usage=False)
    p = build_advice_prompt(plan)
    # Untrusted group summary is fenced as data, flagged not-instructions.
    assert "<data>" in p and "</data>" in p
    assert "untrusted" in p.lower()
    # Group labels + a reclaimable total appear inside.
    assert "Build output" in p
    assert "Reclaimable" in p


def test_clean_narrative_strips_fences():
    from ultimate_sacrifice.analysis.advisor_prompt import clean_narrative

    assert clean_narrative("```\n- do X\n```") == "- do X"
    assert clean_narrative("  hello  ") == "hello"
    assert clean_narrative("") == ""


def test_narrate_returns_empty_without_provider():
    import asyncio

    from ultimate_sacrifice.analysis.advisor_prompt import narrate

    plan = analyze([node(r"C:\a\build", 1)], "C:\\", NOW, with_drive_usage=False)
    assert asyncio.run(narrate(plan, None)) == ""


def test_analyze_with_ai_degrades_to_rules(monkeypatch):
    import asyncio

    from ultimate_sacrifice.analysis import analyze_with_ai

    class DeadProvider:
        async def available(self):
            return False
        async def complete_text(self, prompt):
            return "should not be called"

    plan = asyncio.run(
        analyze_with_ai([node(r"C:\a\build", 1)], "C:\\", NOW, provider=DeadProvider(), with_drive_usage=False)
    )
    assert plan.narrative == ""
    assert plan.narrative_source == "none"
    assert plan.groups  # rule-based plan still present
