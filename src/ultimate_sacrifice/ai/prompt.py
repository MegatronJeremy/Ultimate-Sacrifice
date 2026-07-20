"""Prompt construction and response parsing shared by all providers.

Kept as free functions (no I/O) so they can be unit-tested against canned model
output with no network or GPU.
"""

from __future__ import annotations

import json

from .base import AssessRequest, Assessment, Recommendation, fallback_assessment

# Bump whenever the prompt text or output schema changes. Cached verdicts recorded
# under an older version are treated as stale and re-assessed.
PROMPT_VERSION = 1

SYSTEM_INSTRUCTIONS = (
    "You are a cautious disk-cleanup assistant. You are given one filesystem item "
    "with local metrics and must decide whether it is safe to delete to reclaim space.\n"
    "Rules:\n"
    "- Reply with ONLY a JSON object, no prose, no markdown fences.\n"
    "- Schema: {\"recommendation\": \"keep\"|\"review\"|\"delete\", "
    "\"confidence\": 0.0-1.0, \"reason\": \"one short sentence\"}.\n"
    "- 'delete' = clearly regenerable or disposable (build output, dependency caches, "
    "temp/log files, stale downloads/installers not touched in months).\n"
    "- 'review' = plausibly removable but needs human judgement (large archives, "
    "user documents, anything ambiguous).\n"
    "- 'keep' = likely important or in active use (recently modified source, config, "
    "anything under a system or program-install path).\n"
    "- NEVER recommend 'delete' for OS/program directories (Windows, Program Files, "
    "System32) or source directories with recent edits, regardless of size.\n"
    "- Be conservative: when unsure, choose 'review', not 'delete'."
)


def build_prompt(req: AssessRequest) -> str:
    """Full single-shot prompt: instructions + the item's metrics as JSON."""
    item = {
        "path": req.path,
        "type": "directory" if req.is_dir else "file",
        "size_bytes": req.size_bytes,
        "size_human": _human(req.size_bytes),
        "days_since_last_use": round(req.stale_days, 1),
        "category": req.category,
        "local_junk_score": round(req.junk_score, 2),
        "signals": req.flags,
    }
    return (
        f"{SYSTEM_INSTRUCTIONS}\n\n"
        f"Item to assess:\n{json.dumps(item, indent=2)}\n\n"
        "JSON verdict:"
    )


def parse_response(text: str, req: AssessRequest) -> Assessment:
    """Parse a model's text into an Assessment, tolerating fences and stray prose.

    Falls back to the local heuristic verdict if nothing parseable is found.
    """
    obj = _extract_json(text)
    if obj is None:
        return fallback_assessment(req, "AI response was not valid JSON; used local heuristics.")

    rec = str(obj.get("recommendation", "")).strip().lower()
    if rec not in ("keep", "review", "delete"):
        return fallback_assessment(req, "AI omitted a valid recommendation; used local heuristics.")

    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    reason = str(obj.get("reason", "")).strip() or "(no reason given)"
    reason = reason.replace("\n", " ")[:300]

    return Assessment(
        path=req.path,
        recommendation=rec,  # type: ignore[arg-type]
        confidence=round(conf, 2),
        reason=reason,
        source="ai",
    )


def _extract_json(text: str) -> dict | None:
    """Find the first JSON object in ``text``. Handles ```json fences and prose."""
    if not text:
        return None
    s = text.strip()
    # Strip markdown code fences if present.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    # Fast path: whole thing is JSON.
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Slow path: scan for the first balanced {...} block.
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    return None


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
