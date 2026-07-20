"""Cancellable, os.scandir-based directory walker with size aggregation.

Walks a root path iteratively (no recursion-depth limit), aggregates directory
sizes bottom-up, skips reparse points / symlinks to avoid cycles, and tolerates
per-entry ``PermissionError`` (Windows system dirs) by counting them rather than
aborting. Emits progress via an optional callback so the TUI can show live totals.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from . import heuristics
from .model import Kind, ScanNode, ScanProgress

ProgressCb = Callable[[ScanProgress], None]


def normalize_root(root: str) -> str:
    """Resolve a scan root to an absolute path, fixing the bare-drive-letter trap.

    On Windows ``C:`` means "the current directory on drive C", NOT the drive root —
    so ``os.path.abspath("C:")`` returns the cwd, silently scanning the wrong place.
    A bare drive letter (``C:`` / ``C:``+spaces) is treated as the drive root (``C:\\``).
    Everything else goes through the usual expanduser/abspath.
    """
    root = os.path.expanduser(root.strip())
    # Bare drive letter like "C:" or "c:" (len 2, second char ':') -> drive root.
    if len(root) == 2 and root[1] == ":" and root[0].isalpha():
        return root + "\\"
    return os.path.abspath(root)


def deduplicate_nested(nodes: list[ScanNode]) -> list[ScanNode]:
    """Drop candidates whose bytes are already represented by an ancestor candidate.

    The walk records a directory *and* its qualifying children/grandchildren, so the
    same bytes appear 2-3x (e.g. ``Build`` + ``Build/Win64`` + ``Build/Win64/x64``).
    Summing or selecting those double-counts space. This keeps only the **shallowest**
    node in each nested chain, but separately per class (actionable vs container) so a
    deletable junk dir nested inside a protected container is still surfaced — e.g.
    ``C:\\Dev`` (container) stays, and ``C:\\Dev\\proj\\node_modules`` (actionable)
    inside it also stays, while ``node_modules\\dep`` under it is dropped.

    Pure and order-independent; returns a new list. Uses normalized-path prefix
    containment, so it is a string operation with no filesystem access.
    """
    from . import heuristics

    # Bucket each node's normalized path by class; a node is redundant if a node of the
    # SAME class is a path-ancestor of it within the candidate set.
    def norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p))

    actionable_paths: set[str] = set()
    container_paths: set[str] = set()
    for n in nodes:
        (container_paths if heuristics.is_container(n) else actionable_paths).add(norm(n.path))

    def has_ancestor_in(path: str, pool: set[str]) -> bool:
        parent = os.path.dirname(path)
        while len(parent) > 3:  # stop above drive root "c:\"
            if parent in pool:
                return True
            parent = os.path.dirname(parent)
        return False

    kept: list[ScanNode] = []
    for n in nodes:
        pool = container_paths if heuristics.is_container(n) else actionable_paths
        if not has_ancestor_in(norm(n.path), pool):
            kept.append(n)
    return kept


class ScanCancelled(Exception):
    """Raised internally when a scan is cancelled via the stop flag."""


class Scanner:
    """Stateful, cancellable scanner.

    Call :meth:`scan` to walk a root and return the candidate list (files and
    directories at or above ``min_size_bytes``, largest first, capped at ``top_n``).
    Set :attr:`cancel` to True from another thread to stop early.
    """

    def __init__(
        self,
        min_size_bytes: int,
        top_n: int,
        progress_cb: ProgressCb | None = None,
        progress_every: int = 500,
    ) -> None:
        self.min_size_bytes = min_size_bytes
        self.top_n = top_n
        self._progress_cb = progress_cb
        self._progress_every = max(1, progress_every)
        self.cancel = False
        self._progress = ScanProgress()
        self._started_at = 0.0

    def _emit(self, force: bool = False) -> None:
        if self._progress_cb is None:
            return
        if force or self._progress.entries % self._progress_every == 0:
            if self._started_at:
                self._progress.elapsed_s = time.time() - self._started_at
            self._progress_cb(self._progress)

    def scan(self, root: str) -> list[ScanNode]:
        root = normalize_root(root)
        now = time.time()
        self._started_at = now
        candidates: list[ScanNode] = []

        try:
            self._walk(root, now, candidates)
        except ScanCancelled:
            self._progress.cancelled = True

        self._progress.done = True
        self._emit(force=True)

        # The scan root is itself a big container we never want to offer for deletion.
        root_norm = os.path.normcase(root)
        candidates = [n for n in candidates if os.path.normcase(n.path) != root_norm]

        # Collapse nested duplicates so the same bytes aren't counted (and deletable) twice.
        candidates = deduplicate_nested(candidates)

        # Actionable items first (largest-first), context containers after (largest-first),
        # so the top_n cap can never hide real targets behind big-but-untouchable folders.
        candidates.sort(key=lambda n: (heuristics.is_container(n), -n.size))
        return candidates[: self.top_n]

    def _walk(self, path: str, now: float, out: list[ScanNode]) -> int:
        """Return the aggregate byte size of ``path``; append qualifying nodes to ``out``."""
        if self.cancel:
            raise ScanCancelled

        total = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if self.cancel:
                        raise ScanCancelled
                    total += self._visit(entry, now, out)
        except (PermissionError, OSError):
            self._progress.errors += 1
            return total

        # Record the directory itself as a candidate if it is large enough.
        if total >= self.min_size_bytes:
            try:
                st = os.stat(path, follow_symlinks=False)
                mtime, atime = st.st_mtime, st.st_atime
            except OSError:
                mtime = atime = 0.0
            node = ScanNode(path=path, kind=Kind.DIR, size=total, mtime=mtime, atime=atime)
            heuristics.annotate(node, now)
            out.append(node)
        return total

    def _visit(self, entry: os.DirEntry, now: float, out: list[ScanNode]) -> int:
        self._progress.entries += 1
        self._progress.current_path = entry.path

        try:
            is_symlink = entry.is_symlink()
        except OSError:
            is_symlink = False
        if is_symlink:
            self._emit()
            return 0  # never follow links; avoids cycles and double-counting

        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            self._progress.errors += 1
            self._emit()
            return 0

        if is_dir:
            self._emit()
            return self._walk(entry.path, now, out)

        # Regular file.
        try:
            st = entry.stat(follow_symlinks=False)
            size = st.st_size
        except OSError:
            self._progress.errors += 1
            self._emit()
            return 0

        self._progress.bytes_seen += size
        if size >= self.min_size_bytes:
            node = ScanNode(
                path=entry.path,
                kind=Kind.FILE,
                size=size,
                mtime=st.st_mtime,
                atime=st.st_atime,
            )
            heuristics.annotate(node, now)
            out.append(node)
        self._emit()
        return size
