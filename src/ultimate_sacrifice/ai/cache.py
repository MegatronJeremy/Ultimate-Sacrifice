"""Persistent assessment cache — reuse AI verdicts for unchanged files.

The AI assessment is the expensive part of a run (seconds/item locally, or tokens
in the cloud). This cache stores each *real AI* verdict keyed by the file's
identity ``(path, size, mtime)`` plus the provider/model and prompt version, so a
subsequent run only re-assesses files that actually changed. It mirrors an engine
asset DB: identity -> cooked result, re-cook only what changed.

I/O is confined to ``load``/``save``; the match logic is pure and testable.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from .base import Assessment
from .prompt import PROMPT_VERSION

if TYPE_CHECKING:
    from ..scanner.model import ScanNode


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _key_mtime(mtime: float) -> float:
    # Round so trivial float jitter between stat calls doesn't cause false misses.
    return round(float(mtime), 3)


class AssessmentCache:
    """JSON-backed map from file identity to a stored AI verdict.

    Entries are only ever created from genuine AI results (``source == "ai"``);
    heuristic fallbacks are never cached, so a run that degraded because the
    provider was down will retry the real AI next time.
    """

    def __init__(self, path: str, entries: dict[str, dict] | None = None) -> None:
        self.path = path
        self._entries: dict[str, dict] = entries or {}
        self._dirty = False

    # ---- persistence ----------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "AssessmentCache":
        """Load from ``path``; a missing or corrupt file yields an empty cache."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
            if not isinstance(entries, dict):
                entries = {}
        except (OSError, ValueError):
            entries = {}
        return cls(path, entries)

    def save(self) -> None:
        """Prune dead paths and atomically write the cache to disk."""
        self._prune_missing()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass
        tmp = f"{self.path}.tmp"
        payload = {"version": PROMPT_VERSION, "entries": self._entries}
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=0)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            # Fail-soft: a cache we cannot persist is a missed optimization, not an error.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _prune_missing(self) -> None:
        for key in [k for k in self._entries if not os.path.exists(k)]:
            del self._entries[key]
            self._dirty = True

    # ---- lookup / store -------------------------------------------------

    def get(self, node: "ScanNode", identity: str) -> Assessment | None:
        """Return the cached verdict for ``node`` iff identity + version all match."""
        entry = self._entries.get(_norm(node.path))
        if entry is None:
            return None
        if (
            entry.get("size") != node.size
            or entry.get("mtime") != _key_mtime(node.mtime)
            or entry.get("provider") != identity
            or entry.get("prompt_version") != PROMPT_VERSION
        ):
            return None
        return Assessment(
            path=node.path,
            recommendation=entry["recommendation"],
            confidence=entry["confidence"],
            reason=entry["reason"],
            source="ai-cached",
        )

    def put(self, node: "ScanNode", assessment: Assessment, identity: str) -> None:
        """Store a verdict. No-op unless it is a genuine AI result."""
        if assessment.source != "ai":
            return
        self._entries[_norm(node.path)] = {
            "size": node.size,
            "mtime": _key_mtime(node.mtime),
            "provider": identity,
            "prompt_version": PROMPT_VERSION,
            "recommendation": assessment.recommendation,
            "confidence": assessment.confidence,
            "reason": assessment.reason,
        }
        self._dirty = True

    def __len__(self) -> int:
        return len(self._entries)
