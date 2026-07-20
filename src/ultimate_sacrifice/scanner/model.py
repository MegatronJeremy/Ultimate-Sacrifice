"""Data model for scan results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    FILE = "file"
    DIR = "dir"


@dataclass(slots=True)
class ScanNode:
    """A file or directory discovered during a scan.

    ``size`` is the aggregate size in bytes (for a directory, the total of its
    contents). ``mtime`` / ``atime`` are POSIX timestamps (seconds). ``atime`` may
    be unreliable on volumes with last-access updates disabled, so treat it as a
    hint, not ground truth.
    """

    path: str
    kind: Kind
    size: int = 0
    mtime: float = 0.0
    atime: float = 0.0
    # Populated by heuristics.annotate(); kept on the node so the UI and AI share it.
    category: str = "other"
    junk_score: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        from os.path import basename, normpath

        return basename(normpath(self.path)) or self.path

    @property
    def is_dir(self) -> bool:
        return self.kind is Kind.DIR


@dataclass(slots=True)
class ScanProgress:
    """Snapshot of scan progress, emitted to the UI while walking."""

    entries: int = 0
    bytes_seen: int = 0
    current_path: str = ""
    errors: int = 0
    done: bool = False
