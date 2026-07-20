"""Safe deletion with hard path guards, Recycle-Bin default, and dry-run.

Deletion is the one irreversible action this app takes, so it is centralized here
behind explicit guards. ``is_guarded`` refuses OS/program/critical paths regardless
of any AI recommendation; the UI must never bypass it.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from os.path import abspath, normcase, normpath

from send2trash import send2trash

# Top-level directory names we never delete, on ANY drive (e.g. both C:\Windows and
# D:\Windows). Matched drive-relative so multi-drive systems are covered, not just C:.
_GUARDED_TOP_DIRS = (
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
)

# OS-managed swap / hibernation files — deleting these breaks or destabilizes Windows.
# Matched by basename regardless of which drive/location they live on.
_GUARDED_FILENAMES = frozenset(
    ("pagefile.sys", "hiberfil.sys", "swapfile.sys")
)

# Virtual-disk / VM images — a single file that IS an entire filesystem (WSL, Hyper-V,
# VirtualBox, VMware). Recycling one silently wipes everything inside it.
_GUARDED_EXTENSIONS = frozenset(
    (".vhdx", ".vhd", ".vmdk", ".vdi", ".avhd", ".avhdx")
)


@dataclass(slots=True)
class DeleteResult:
    path: str
    ok: bool
    freed_bytes: int
    method: str  # "recycle-bin" | "permanent" | "dry-run" | "skipped"
    error: str = ""


def _norm(path: str) -> str:
    return normcase(normpath(abspath(os.path.expanduser(path))))


def is_drive_root(path: str) -> bool:
    n = _norm(path)
    # e.g. "c:\" — a drive letter, colon, single separator.
    return len(n) <= 3 and n[1:2] == ":"


def _top_dir_relative(norm_path: str) -> str | None:
    """First path segment below a drive root, lowercased. None if not a drive path.

    ``C:\\Windows\\System32`` -> ``windows``; ``D:\\Program Files\\x`` -> ``program files``.
    Lets us guard system dirs on any drive, not just C:. Expects an already-normalized
    (``_norm``) path.
    """
    if len(norm_path) < 3 or norm_path[1:3] != ":\\":
        return None  # not a "<drive>:\..." path (e.g. a UNC share) — no drive-relative top
    rest = norm_path[3:]
    if not rest:
        return None
    return rest.split("\\", 1)[0]


def is_guarded(path: str) -> tuple[bool, str]:
    """Return (guarded, reason). Guarded paths must never be deleted."""
    n = _norm(path)
    if is_drive_root(n):
        return True, "refusing to delete a drive root"
    base = os.path.basename(n)
    if base in _GUARDED_FILENAMES:
        return True, f"refusing to delete an OS swap/hibernation file ({base})"
    if os.path.splitext(base)[1] in _GUARDED_EXTENSIONS:
        return True, "refusing to delete a virtual-disk image (contains an entire filesystem)"
    top = _top_dir_relative(n)
    if top is not None and top in _GUARDED_TOP_DIRS:
        return True, f"refusing to delete a protected system path ({top})"
    # Refuse to delete the app's own install/source directory.
    try:
        app_root = _norm(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        if n == app_root or n.startswith(app_root + "\\") or app_root.startswith(n + "\\"):
            return True, "refusing to delete the running application's own directory"
    except OSError:
        pass
    # Refuse whole directories that aren't recognized junk — a big "container" folder
    # (C:\Dev, a user's Videos, the scan root) is valuable because of what it holds.
    # Recognized-disposable dirs (node_modules, build/, Temp, caches) fall through as allowed.
    from ..scanner.heuristics import is_build_artifact, is_temp_path

    if os.path.isdir(path) and not is_build_artifact(path) and not is_temp_path(path):
        return True, "refusing to delete a directory not recognized as junk (looks like a container)"
    return False, ""


def _size_on_disk(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def delete_path(path: str, *, use_recycle_bin: bool = True, dry_run: bool = False) -> DeleteResult:
    """Delete one path. Guards first, then dry-run, then Recycle Bin / permanent."""
    guarded, reason = is_guarded(path)
    if guarded:
        return DeleteResult(path, ok=False, freed_bytes=0, method="skipped", error=reason)

    if not os.path.exists(path):
        return DeleteResult(path, ok=False, freed_bytes=0, method="skipped", error="path no longer exists")

    freed = _size_on_disk(path)

    if dry_run:
        return DeleteResult(path, ok=True, freed_bytes=freed, method="dry-run")

    try:
        if use_recycle_bin:
            send2trash(path)
            method = "recycle-bin"
        else:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            method = "permanent"
    except Exception as exc:  # noqa: BLE001 - send2trash raises library-specific errors
        return DeleteResult(path, ok=False, freed_bytes=0, method="error", error=str(exc)[:200])

    return DeleteResult(path, ok=True, freed_bytes=freed, method=method)


def delete_many(
    paths: list[str],
    *,
    use_recycle_bin: bool = True,
    dry_run: bool = False,
    on_progress: "Callable[[int, int, DeleteResult], None] | None" = None,
) -> list[DeleteResult]:
    """Delete each path in turn, returning per-item results.

    ``on_progress(done, total, result)`` is called after each item (if given) so a UI
    can show live deletion progress. It runs on the calling thread — callers that need
    to marshal back to a UI thread should do so inside the callback.
    """
    total = len(paths)
    results: list[DeleteResult] = []
    for i, p in enumerate(paths, start=1):
        r = delete_path(p, use_recycle_bin=use_recycle_bin, dry_run=dry_run)
        results.append(r)
        if on_progress is not None:
            on_progress(i, total, r)
    return results
