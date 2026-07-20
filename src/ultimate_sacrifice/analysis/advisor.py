"""Pure analysis core: group scan candidates into ranked cleanup opportunities.

A flat list of large items doesn't tell you *what to do* on a full multi-TB disk.
This module aggregates the scan into **cleanup groups** (dependency dirs, build
output per project, caches, installers, large loose files), ranks them by impact
and safety, and produces a **disk map** — the decision-support layer on top of the
scanner's discovery. Everything here is data->data and unit-testable; the only I/O
is an optional ``shutil.disk_usage`` for free/total context, isolated in one spot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from ..scanner import heuristics
from ..scanner.model import ScanNode

# Markers that identify a project root, so build output can be grouped per project.
_PROJECT_MARKERS = (".git", ".sln", ".uproject", ".uplugin", "package.json", "CMakeLists.txt", "Cargo.toml")

# Directory names that read as build OUTPUT (regenerable by rebuilding) vs dependency dirs.
_BUILD_OUTPUT_NAMES = {
    "build", "dist", "target", "obj", "bin", "intermediate", "cmakefiles",
    "buildtrees", ".vs", ".gradle", "__pycache__", ".next", ".nuxt", ".turbo",
    "deriveddatacache", "saved",
}
_DEPENDENCY_NAMES = {"node_modules", "vendor", "packages", ".venv", "site-packages"}


class Safety(str, Enum):
    SAFE = "safe"        # regenerable / transient — reclaim freely
    REVIEW = "review"    # may hold user data — look before deleting
    CAUTION = "caution"  # ambiguous — verify


class RegenCost(str, Enum):
    FREE = "free"            # transient, comes back on its own (temp/cache)
    REBUILD = "rebuild"      # regenerates on next build (build output)
    REDOWNLOAD = "redownload"  # re-fetched on demand (dependency dirs, vault caches)
    NONE = "none"            # not regenerable — real loss if wrong (archives, user files)


# Cleanup class -> (display label, safety, regen cost, one-line rule reason).
_CLASS_META = {
    "build-output": (
        "Build output", Safety.SAFE, RegenCost.REBUILD,
        "Compiler/build artifacts — regenerate on the next build.",
    ),
    "dependency-dirs": (
        "Dependency folders", Safety.SAFE, RegenCost.REDOWNLOAD,
        "Fetched packages — restored by install/restore commands.",
    ),
    "caches": (
        "Caches & temp", Safety.SAFE, RegenCost.FREE,
        "Transient cache/temp data — rebuilt automatically when needed.",
    ),
    "installers-archives": (
        "Installers & archives", Safety.REVIEW, RegenCost.NONE,
        "Downloaded installers/archives — deletable once used, but not regenerable.",
    ),
    "large-loose-files": (
        "Large files", Safety.REVIEW, RegenCost.NONE,
        "Big individual files — review before deleting; may be irreplaceable.",
    ),
    "other": (
        "Other", Safety.CAUTION, RegenCost.NONE,
        "Uncategorized large items — verify before acting.",
    ),
}


@dataclass(slots=True)
class CleanupGroup:
    key: str                    # stable id, e.g. "build-output" or "build-output::C:\Dev\U57"
    label: str
    cleanup_class: str
    total_bytes: int
    item_count: int
    paths: list[str]
    safety: Safety
    regen_cost: RegenCost
    reason: str
    project: str = ""           # project root when sub-grouped, else ""


@dataclass(slots=True)
class DiskSummary:
    root: str
    reclaimable_bytes: int
    candidate_count: int
    by_class: list[tuple[str, int, float]]  # (class, bytes, pct-of-reclaimable)
    drive_total_bytes: int = 0
    drive_free_bytes: int = 0


@dataclass(slots=True)
class AdvicePlan:
    summary: DiskSummary
    groups: list[CleanupGroup]
    narrative: str = ""         # AI-written why/how/risk plan; "" when unavailable
    narrative_source: str = "none"  # "ai" | "none"


# ---- classification -------------------------------------------------------


def cleanup_class(node: ScanNode) -> str:
    """Coarser, action-oriented bucket than heuristics.category (which is display-level)."""
    parts = heuristics._parts(node.path)  # normalized lowercase segments
    partset = set(parts)
    if partset & _DEPENDENCY_NAMES:
        return "dependency-dirs"
    if partset & _BUILD_OUTPUT_NAMES:
        return "build-output"
    cat = node.category
    if cat == "temp-cache" or cat == "temp-file":
        return "caches"
    if cat == "build-artifact":
        # build-artifact not caught above (e.g. generic) -> treat as build output
        return "build-output"
    if cat == "archive/installer":
        return "installers-archives"
    if not node.is_dir:
        return "large-loose-files"
    return "other"


def project_root_of(path: str) -> str:
    """Nearest ancestor dir that looks like a project root, for per-project grouping.

    Pure path heuristic: walks up looking for a segment that *contains* a project
    marker as a sibling is not knowable without I/O, so we instead detect a marker
    appearing as a path segment (e.g. ``...\\U57\\Engine\\Intermediate`` -> project
    is the dir just above the first build/engine boundary). Falls back to the 3rd
    path segment (drive + top-level + project) so grouping is still meaningful.
    """
    parts = [p for p in os.path.normpath(path).replace("/", "\\").split("\\") if p]
    if not parts:
        return path
    low = [p.lower() for p in parts]
    # Cut at the first build/dependency boundary — everything above it is the project.
    boundary = None
    for i, seg in enumerate(low):
        if seg in _BUILD_OUTPUT_NAMES or seg in _DEPENDENCY_NAMES:
            boundary = i
            break
    if boundary is not None and boundary > 0:
        proj_parts = parts[:boundary]
    else:
        proj_parts = parts[: min(3, len(parts))]
    # Rebuild an absolute-ish path (drive letter keeps its backslash).
    root = proj_parts[0]
    if root.endswith(":"):
        return root + "\\" + "\\".join(proj_parts[1:]) if len(proj_parts) > 1 else root + "\\"
    return "\\".join(proj_parts)


# ---- grouping & ranking ---------------------------------------------------


def group_candidates(nodes: list[ScanNode]) -> list[CleanupGroup]:
    """Bucket actionable (non-container) candidates into cleanup groups.

    build-output and dependency-dirs are sub-grouped per project so "UE builds across
    N projects" reads as distinct, actionable lines. Every actionable byte lands in
    exactly one group (no double counting — callers pass de-duplicated candidates).
    """
    buckets: dict[str, list[ScanNode]] = {}
    for node in nodes:
        if heuristics.is_container(node):
            continue  # containers are context, never cleanup targets
        cls = cleanup_class(node)
        if cls in ("build-output", "dependency-dirs"):
            key = f"{cls}::{project_root_of(node.path)}"
        else:
            key = cls
        buckets.setdefault(key, []).append(node)

    groups: list[CleanupGroup] = []
    for key, members in buckets.items():
        cls = key.split("::", 1)[0]
        project = key.split("::", 1)[1] if "::" in key else ""
        label, safety, regen, reason = _CLASS_META.get(cls, _CLASS_META["other"])
        display_label = f"{label} — {_short_project(project)}" if project else label
        groups.append(
            CleanupGroup(
                key=key,
                label=display_label,
                cleanup_class=cls,
                total_bytes=sum(n.size for n in members),
                item_count=len(members),
                paths=[n.path for n in members],
                safety=safety,
                regen_cost=regen,
                reason=reason,
                project=project,
            )
        )
    return groups


def _short_project(project: str) -> str:
    parts = [p for p in project.replace("/", "\\").split("\\") if p]
    if len(parts) <= 2:
        return project
    return "…\\" + "\\".join(parts[-2:])


# Priority weighting: size dominates (GB), safety multiplies, staleness nudges.
_SAFETY_WEIGHT = {Safety.SAFE: 1.0, Safety.REVIEW: 0.6, Safety.CAUTION: 0.4}


def rank_groups(groups: list[CleanupGroup], now: float, stale_by_key: dict[str, float] | None = None) -> list[CleanupGroup]:
    """Order groups by cleanup priority: biggest safe reclaim first.

    Deterministic and explainable. ``stale_by_key`` (optional) maps group key -> mean
    staleness days for a small recency bonus; absent, staleness is ignored.
    """
    stale_by_key = stale_by_key or {}

    def priority(g: CleanupGroup) -> float:
        gb = g.total_bytes / (1024 ** 3)
        weight = _SAFETY_WEIGHT.get(g.safety, 0.4)
        stale = stale_by_key.get(g.key, 0.0)
        stale_bonus = 1.0 + min(0.5, stale / 365.0 * 0.5)  # up to +50% for >=1y old
        return gb * weight * stale_bonus

    return sorted(groups, key=priority, reverse=True)


def disk_summary(nodes: list[ScanNode], root: str, with_drive_usage: bool = True) -> DiskSummary:
    """Reclaimable total + per-class breakdown, plus optional drive free/total."""
    actionable = [n for n in nodes if not heuristics.is_container(n)]
    reclaimable = sum(n.size for n in actionable)

    by_class_bytes: dict[str, int] = {}
    for n in actionable:
        by_class_bytes[cleanup_class(n)] = by_class_bytes.get(cleanup_class(n), 0) + n.size
    by_class = sorted(
        (
            (cls, b, (b / reclaimable * 100.0) if reclaimable else 0.0)
            for cls, b in by_class_bytes.items()
        ),
        key=lambda t: t[1],
        reverse=True,
    )

    total = free = 0
    if with_drive_usage:
        try:
            import shutil

            usage = shutil.disk_usage(os.path.abspath(os.path.expanduser(root)))
            total, free = usage.total, usage.free
        except OSError:
            pass

    return DiskSummary(
        root=root,
        reclaimable_bytes=reclaimable,
        candidate_count=len(actionable),
        by_class=by_class,
        drive_total_bytes=total,
        drive_free_bytes=free,
    )


def analyze(nodes: list[ScanNode], root: str, now: float, with_drive_usage: bool = True) -> AdvicePlan:
    """Full rule-based analysis: disk summary + ranked cleanup groups (no AI here)."""
    groups = group_candidates(nodes)
    # Mean staleness per group for the ranking recency bonus.
    stale_by_key: dict[str, float] = {}
    by_path = {n.path: n for n in nodes}
    for g in groups:
        days = [heuristics.staleness_days(by_path[p], now) for p in g.paths if p in by_path]
        stale_by_key[g.key] = sum(days) / len(days) if days else 0.0
    ranked = rank_groups(groups, now, stale_by_key)
    summary = disk_summary(nodes, root, with_drive_usage=with_drive_usage)
    return AdvicePlan(summary=summary, groups=ranked)
