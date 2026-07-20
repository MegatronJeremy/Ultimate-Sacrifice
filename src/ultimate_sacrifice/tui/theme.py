"""Visual theme for the TUI — "Gold & Obsidian", inspired by Galneryus' album
*Ultimate Sacrifice*.

A single registered Textual ``Theme`` drives every default widget's color, so the
rest of the UI styles via theme tokens (``$primary``/``$error``/...) instead of
hardcoding colors per widget. Verdict colors also come from one place
(``verdict_style``) so the table and detail panel never drift.
"""

from __future__ import annotations

from textual.theme import Theme

# Palette (locked with the user): obsidian ground, parchment text, regal gold,
# moss for "keep", crimson for danger.
_OBSIDIAN = "#0a0a0f"
_PARCHMENT = "#e8dcc0"
_GOLD = "#d4af37"
_GOLD_DIM = "#8a6d3b"
_MOSS = "#6b8e5a"
_CRIMSON = "#b3242b"
_MAUVE_DIM = "#6c6478"

ULTIMATE_SACRIFICE_THEME = Theme(
    name="ultimate-sacrifice",
    primary=_GOLD,
    secondary=_GOLD_DIM,
    accent=_GOLD,
    foreground=_PARCHMENT,
    background=_OBSIDIAN,
    surface="#141018",
    panel="#1c1622",
    success=_MOSS,      # keep
    warning=_GOLD,      # review
    error=_CRIMSON,     # delete / danger
    dark=True,
    variables={
        "block-cursor-background": _GOLD,
        "block-cursor-foreground": _OBSIDIAN,
        "block-cursor-text-style": "bold",
        "border": _GOLD,
        "border-blurred": _GOLD_DIM,
        "footer-key-foreground": _GOLD,
        "footer-description-foreground": _PARCHMENT,
        "input-selection-background": f"{_GOLD_DIM} 40%",
        "datatable--header-cursor": _GOLD,
    },
)

# ASCII banner shown on the scan screen. Kept <= 70 columns so it fits an 80-col
# terminal; it's a plain Static, so a narrower terminal just wraps it (no crash).
BANNER = r"""
 _   _ _ _   _                 _
| | | | | |_(_)_ __ ___   __ _| |_ ___
| | | | | __| | '_ ` _ \ / _` | __/ _ \
| |_| | | |_| | | | | | | (_| | ||  __/
 \___/|_|\__|_|_| |_| |_|\__,_|\__\___|
 ____                  _  __ _
/ ___|  __ _  ___ _ __(_)/ _(_) ___ ___
\___ \ / _` |/ __| '__| | |_| |/ __/ _ \
 ___) | (_| | (__| |  | |  _| | (_|  __/
|____/ \__,_|\___|_|  |_|_| |_|\___\___|
"""

TAGLINE = "— reclaim what must be sacrificed —"


def verdict_style(recommendation: str) -> str:
    """Rich/Textual style string for an AI recommendation (single source of truth)."""
    return {
        "keep": _MOSS,
        "review": _GOLD,
        "delete": f"bold {_CRIMSON}",
        "protected": _MAUVE_DIM,
        "container": _MAUVE_DIM,
    }.get(recommendation, _PARCHMENT)
