"""Pure, local heuristics that pre-rank cleanup candidates.

Everything here is data->data with no I/O beyond reading the timestamps already on
a ``ScanNode``. These signals do double duty: they give the UI a useful ranking
even with no AI available, and they are fed to the AI as context so it decides
against real metrics instead of guessing. Kept pure so tests exercise them with no
GPU, network, or real filesystem.
"""

from __future__ import annotations

import re
from os.path import normcase, normpath

from .model import ScanNode

SECONDS_PER_DAY = 86_400.0

# Directory names that are almost always regenerable build output / dependency caches.
_BUILD_ARTIFACT_NAMES = {
    "node_modules",
    "build",
    "dist",
    "target",
    "__pycache__",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "bin",
    "obj",
    ".vs",
    "cmakefiles",
    "buildtrees",
    "packages",
    ".next",
    ".nuxt",
    ".turbo",
    "vendor",
}

# Path fragments that indicate temporary / cache data.
_TEMP_FRAGMENTS = (
    "temp",
    "tmp",
    "cache",
    ".cache",
    "appdata\\local\\temp",
    "windows\\temp",
    "logs",
    "crashdumps",
    "downloads",
)

# Extensions that read as disposable when large.
_TEMP_EXTS = {".tmp", ".temp", ".log", ".dmp", ".old", ".bak", ".cache", ".part", ".crdownload"}
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".iso", ".tar", ".gz", ".msi", ".exe"}

_STANDALONE_NAME_RE = re.compile(r"[\\/]")


def _parts(path: str) -> list[str]:
    return [p for p in normcase(normpath(path)).replace("/", "\\").split("\\") if p]


def is_build_artifact(path: str) -> bool:
    """True if any path segment is a well-known regenerable build/dependency dir."""
    return any(part in _BUILD_ARTIFACT_NAMES for part in _parts(path))


def is_temp_path(path: str) -> bool:
    """True if the path lives under a temp/cache/logs/downloads location."""
    low = normcase(path).replace("/", "\\")
    return any(frag in low for frag in _TEMP_FRAGMENTS)


def staleness_days(node: ScanNode, now: float) -> float:
    """Days since the node was last modified (or accessed, whichever is later).

    ``now`` is a POSIX timestamp passed in so the function stays pure/testable.
    Returns 0.0 when no usable timestamp is present.
    """
    ref = max(node.mtime, node.atime)
    if ref <= 0:
        return 0.0
    return max(0.0, (now - ref) / SECONDS_PER_DAY)


def _extension(path: str) -> str:
    from os.path import splitext

    return splitext(path)[1].lower()


def category(node: ScanNode) -> str:
    """Coarse bucket used for display and as an AI hint."""
    if is_build_artifact(node.path):
        return "build-artifact"
    ext = _extension(node.path)
    if not node.is_dir and ext in _TEMP_EXTS:
        return "temp-file"
    if is_temp_path(node.path):
        return "temp-cache"
    if not node.is_dir and ext in _ARCHIVE_EXTS:
        return "archive/installer"
    return "other"


def junk_score(node: ScanNode, now: float) -> float:
    """Heuristic 0..1 estimate of how safe/worthwhile the node is to delete.

    Higher = more likely disposable. Combines category, staleness, and size. This
    is intentionally simple and conservative; the AI refines it.
    """
    cat = category(node)
    base = {
        "build-artifact": 0.75,
        "temp-file": 0.7,
        "temp-cache": 0.6,
        "archive/installer": 0.35,
        "other": 0.15,
    }[cat]

    # Staleness bonus: nothing touched in >180 days trends toward disposable.
    days = staleness_days(node, now)
    if days >= 365:
        base += 0.2
    elif days >= 180:
        base += 0.12
    elif days >= 90:
        base += 0.06
    elif days <= 7:
        # Recently touched -> pull back; likely in active use.
        base -= 0.15

    # Size bonus: bigger reclaim is marginally more worthwhile, capped.
    gb = node.size / (1024 ** 3)
    if gb >= 5:
        base += 0.1
    elif gb >= 1:
        base += 0.05

    return max(0.0, min(1.0, base))


def is_container(node: ScanNode) -> bool:
    """True for a directory that is large only because of its contents.

    A *container* is a directory not recognized as disposable junk (not a
    build-artifact, temp, or cache dir) — e.g. ``C:\\Dev`` or ``C:\\Users\\me\\
    Videos``. These are big because they hold valuable things, so they are shown
    for context but never treated as cleanup targets.
    """
    return node.is_dir and category(node) == "other"


def flags_for(node: ScanNode, now: float) -> list[str]:
    """Human-readable signal tags shown in the UI and passed to the AI."""
    out: list[str] = []
    if is_container(node):
        out.append("container")
    if is_build_artifact(node.path):
        out.append("build-artifact")
    if is_temp_path(node.path):
        out.append("temp-location")
    days = staleness_days(node, now)
    if days >= 365:
        out.append("stale>1y")
    elif days >= 180:
        out.append("stale>180d")
    elif days >= 90:
        out.append("stale>90d")
    elif days <= 7 and days > 0:
        out.append("recent<=7d")
    return out


def annotate(node: ScanNode, now: float) -> ScanNode:
    """Fill in ``category``, ``junk_score`` and ``flags`` on the node in place."""
    node.category = category(node)
    node.junk_score = junk_score(node, now)
    node.flags = flags_for(node, now)
    return node
