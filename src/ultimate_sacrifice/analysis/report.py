"""Plain-text rendering of an AdvicePlan for the headless --advise report."""

from __future__ import annotations

from ..config import human_size
from .advisor import AdvicePlan

_BAR_WIDTH = 28


def _bar(pct: float) -> str:
    filled = int(round(pct / 100.0 * _BAR_WIDTH))
    return "#" * filled + "-" * (_BAR_WIDTH - filled)


def render_plan(plan: AdvicePlan, top: int = 12) -> str:
    """Render the disk map + ranked plan (+ narrative) as terminal text."""
    s = plan.summary
    out: list[str] = []
    out.append("=" * 64)
    out.append("  DISK ADVISOR")
    out.append("=" * 64)
    if s.drive_total_bytes:
        used = s.drive_total_bytes - s.drive_free_bytes
        out.append(
            f"Drive {s.root}: {human_size(used)} used, {human_size(s.drive_free_bytes)} free "
            f"of {human_size(s.drive_total_bytes)}"
        )
    out.append(
        f"Reclaimable found: {human_size(s.reclaimable_bytes)} across {s.candidate_count} items"
    )
    out.append("")
    out.append("Where the reclaimable space is:")
    for cls, b, pct in s.by_class:
        out.append(f"  {_bar(pct)} {pct:5.1f}%  {human_size(b):>10}  {cls}")
    out.append("")
    out.append("-" * 64)
    out.append("  RECOMMENDED ACTIONS (biggest safe wins first)")
    out.append("-" * 64)
    if not plan.groups:
        out.append("  Nothing actionable found above the size threshold.")
    for i, g in enumerate(plan.groups[:top], start=1):
        out.append(
            f"{i:>2}. {human_size(g.total_bytes):>10}  [{g.safety.value}/{g.regen_cost.value}]  {g.label}"
        )
        out.append(f"      {g.item_count} item(s) · {g.reason}")
    if len(plan.groups) > top:
        rest = sum(g.total_bytes for g in plan.groups[top:])
        out.append(f"    …and {len(plan.groups) - top} more groups ({human_size(rest)})")
    out.append("")
    if plan.narrative:
        out.append("-" * 64)
        out.append("  AI STRATEGY NOTES")
        out.append("-" * 64)
        out.append(plan.narrative)
    else:
        out.append("(AI narrative unavailable — showing rule-based plan only.)")
    out.append("=" * 64)
    return "\n".join(out)
