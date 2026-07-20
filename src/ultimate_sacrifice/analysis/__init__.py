"""Disk Advisor — turn a flat scan into a disk map and a prioritized cleanup plan."""

from __future__ import annotations

from .advisor import (
    AdvicePlan,
    CleanupGroup,
    DiskSummary,
    analyze,
    disk_summary,
    group_candidates,
    rank_groups,
)
from .advisor_prompt import build_advice_prompt, narrate


async def analyze_with_ai(nodes, root, now, provider=None, with_drive_usage=True) -> AdvicePlan:
    """Rule-based plan + optional AI narrative (hybrid). Degrades to rules-only."""
    plan = analyze(nodes, root, now, with_drive_usage=with_drive_usage)
    text = await narrate(plan, provider)
    if text:
        plan.narrative = text
        plan.narrative_source = "ai"
    return plan


__all__ = [
    "AdvicePlan",
    "CleanupGroup",
    "DiskSummary",
    "analyze",
    "analyze_with_ai",
    "build_advice_prompt",
    "disk_summary",
    "group_candidates",
    "narrate",
    "rank_groups",
]
