"""Prompt construction and response parsing shared by all providers.

Kept as free functions (no I/O) so they can be unit-tested against canned model
output with no network or GPU.
"""

from __future__ import annotations

import json

from .base import AssessRequest, Assessment, Recommendation, fallback_assessment

# Bump whenever the prompt text or output schema changes. Cached verdicts recorded
# under an older version are treated as stale and re-assessed.
PROMPT_VERSION = 2

SYSTEM_INSTRUCTIONS = (
    "You are a decisive disk-cleanup assistant. You are given one filesystem item "
    "with local metrics and must decide whether it is safe to delete to reclaim space. "
    "A local scanner has already excluded OS/program paths, the scan root, and valuable "
    "container folders, so the item you see is a real cleanup candidate.\n"
    "Rules:\n"
    "- Reply with ONLY a JSON object, no prose, no markdown fences.\n"
    "- Schema: {\"recommendation\": \"keep\"|\"review\"|\"delete\", "
    "\"confidence\": 0.0-1.0, \"reason\": \"one short sentence\"}.\n"
    "- Choose 'delete' with HIGH confidence (>= 0.9) for clearly regenerable or "
    "disposable items. These are safe to delete because they are rebuilt/re-downloaded "
    "automatically and hold no unique user data:\n"
    "    * category 'build-artifact' — dependency dirs (node_modules, vendor, packages) "
    "and build output (build/, dist/, target/, Intermediate/, obj/, .gradle, __pycache__). "
    "These regenerate from source on the next build/install.\n"
    "    * category 'temp-cache' — temp dirs, log dirs, and tool/package caches. Transient "
    "by definition.\n"
    "  Do not downgrade these to 'review' merely because they are large or you lack full "
    "context — regenerable-ness is the deciding factor, and the category already establishes it.\n"
    "- Choose 'review' for items that MIGHT hold irreplaceable data and need a human eye: "
    "large archives/installers (category 'archive/installer'), a user's Downloads, media, "
    "documents, or anything whose value you genuinely cannot judge from the metrics.\n"
    "- Choose 'keep' for items that look actively used or important: recently modified "
    "(days_since_last_use is small) source/config/projects, save data, or anything that "
    "would be painful to lose and is not trivially regenerable.\n"
    "- Weigh staleness: a build-artifact/cache untouched for months is an even safer delete; "
    "something modified in the last few days leans toward 'review'/'keep'.\n"
    "- The 'local_junk_score' (0..1) is the scanner's own disposability estimate — a high "
    "score corroborates 'delete', a low score warns you to look closer.\n"
    "- Reserve caution for the ambiguous middle. Do NOT reflexively pick 'review' for a clearly "
    "regenerable build-artifact or cache — that is exactly the case you should confidently delete."
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
