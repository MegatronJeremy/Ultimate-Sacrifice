"""AI provider interface and shared request/response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Recommendation = Literal["keep", "review", "delete"]


@dataclass(slots=True)
class AssessRequest:
    """One item to assess, with the local metrics the model should reason over."""

    path: str
    is_dir: bool
    size_bytes: int
    stale_days: float
    category: str
    junk_score: float
    flags: list[str]


@dataclass(slots=True)
class Assessment:
    """The model's (or fallback) verdict for one item."""

    path: str
    recommendation: Recommendation
    confidence: float
    reason: str
    source: str = "ai"  # "ai" or "heuristic-fallback"


@runtime_checkable
class AIProvider(Protocol):
    """A pluggable backend that turns AssessRequests into Assessments."""

    name: str

    async def available(self) -> bool:
        """Cheap reachability/auth check; used to fail loud at startup."""
        ...

    async def assess_one(self, request: AssessRequest) -> Assessment:
        """Assess a single item. The assessor handles batching/concurrency."""
        ...


def fallback_assessment(request: AssessRequest, reason: str) -> Assessment:
    """Deterministic verdict derived purely from local heuristics.

    Used when a provider is unavailable or returns unparseable output, so the app
    degrades gracefully instead of blanking the row. Conservative: only the
    clearest junk gets a 'delete', everything else is 'review'.
    """
    score = request.junk_score
    if score >= 0.8:
        rec: Recommendation = "delete"
    elif score >= 0.45:
        rec = "review"
    else:
        rec = "keep"
    return Assessment(
        path=request.path,
        recommendation=rec,
        confidence=round(min(0.6, score), 2),  # never over-confident without the AI
        reason=reason,
        source="heuristic-fallback",
    )
