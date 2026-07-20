"""Textual application entry point."""

from __future__ import annotations

import argparse

from textual.app import App

from .ai import AssessmentCache
from .config import Config, load_config, resolved_cache_path
from .tui.screens import ScanConfigScreen
from .tui.theme import ULTIMATE_SACRIFICE_THEME

_CSS = """
Screen {
    background: $background;
}
Header {
    background: $background;
    color: $primary;
    text-style: bold;
}
Footer {
    background: $background;
}

/* ---- Scan config screen ---- */
#config-body {
    padding: 1 3;
}
#banner {
    color: $primary;
    text-style: bold;
    height: auto;
    margin-bottom: 0;
}
#tagline {
    color: $secondary;
    text-style: italic;
    margin-bottom: 1;
}
#title {
    padding: 1 0;
    text-style: bold;
    color: $foreground;
}
#config-body Label {
    margin-top: 1;
    color: $secondary;
    text-style: bold;
}
#config-body Input, #config-body Select {
    width: 80%;
    max-width: 90;
    border: round $secondary;
    background: $surface;
}
#config-body Input:focus, #config-body Select:focus {
    border: round $accent;
}
#config-toggles {
    height: auto;
    margin-top: 1;
}
#config-toggles Checkbox {
    margin-right: 4;
    background: $surface;
    border: round $secondary;
}
#config-toggles Checkbox:focus {
    border: round $accent;
}
#scan-btn {
    margin-top: 2;
}
#config-status {
    margin-top: 1;
}

/* ---- Results screen ---- */
#context-bar {
    height: auto;
    padding: 1 2;
    margin: 1 1 0 1;
    background: $panel;
    color: $foreground;
    border: round $primary;
}
#breadcrumb {
    height: auto;
    padding: 0 2;
    color: $accent;
    text-style: bold;
    display: none;
}
#results-status {
    padding: 1 2;
    height: auto;
    background: $surface;
    color: $foreground;
    border: round $primary;
}
#scan-progress {
    margin: 0 2;
}
#results-table {
    height: 1fr;
    border: round $primary;
    background: $background;
}
#results-table > .datatable--header {
    color: $primary;
    text-style: bold;
    background: $panel;
}
#results-table > .datatable--cursor {
    background: $primary;
    color: $background;
    text-style: bold;
}
#detail-panel {
    height: 7;
    padding: 1 2;
    border: heavy $primary;
    background: $surface;
    color: $foreground;
}

/* ---- Confirm-delete modal ---- */
#confirm-dialog {
    grid-size: 1;
    grid-gutter: 1;
    padding: 2 4;
    width: 76%;
    min-width: 70;
    max-width: 120;
    height: auto;
    max-height: 90%;
    border: heavy $error;
    background: $surface;
}
#confirm-title {
    color: $primary;
    text-style: bold;
    width: 100%;
    content-align: center middle;
}
#confirm-mode {
    color: $foreground;
    width: 100%;
    content-align: center middle;
}
#confirm-list-label {
    color: $secondary;
}
#confirm-list {
    height: auto;
    min-height: 6;
    max-height: 16;
    padding: 1 2;
    border: round $secondary;
    background: $background;
}
#delete-progress {
    width: 100%;
}
#confirm-prompt {
    color: $foreground;
}
#confirm-buttons {
    height: auto;
    align-horizontal: right;
}
#confirm-buttons Button {
    margin-left: 2;
}
#confirm-status {
    color: $foreground;
}

/* ---- Help overlay ---- */
#help-dialog {
    grid-size: 1;
    grid-rows: auto 1fr auto;
    padding: 1 2;
    width: 64;
    max-width: 100%;
    height: auto;
    max-height: 90%;
    border: heavy $primary;
    background: $surface;
}
#help-title {
    color: $primary;
    text-style: bold;
    margin-bottom: 1;
}
#help-list {
    height: auto;
    max-height: 20;
}
#help-footer {
    margin-top: 1;
}
"""


class UltimateSacrificeApp(App):
    CSS = _CSS
    TITLE = "Ultimate Sacrifice"
    SUB_TITLE = "AI disk scanner & cleanup"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.cache: AssessmentCache | None = None

    def on_mount(self) -> None:
        self.register_theme(ULTIMATE_SACRIFICE_THEME)
        self.theme = "ultimate-sacrifice"
        if self.cfg.cache.enabled:
            self.cache = AssessmentCache.load(resolved_cache_path(self.cfg.cache))
        self.push_screen(ScanConfigScreen())

    def on_unmount(self) -> None:
        if self.cache is not None:
            self.cache.save()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-accelerated disk scanner and cleanup TUI.")
    parser.add_argument("--config", help="Path to a config.toml (optional).")
    parser.add_argument("--root", help="Override the folder to scan.")
    parser.add_argument(
        "--provider",
        choices=("ollama", "claude_cli", "anthropic"),
        help="Override the AI provider.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the assessment cache (re-assess every item).",
    )
    parser.add_argument(
        "--advise",
        action="store_true",
        help="Headless: scan and print a disk map + prioritized cleanup plan, then exit (no TUI).",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="With --advise, skip the AI narrative (rule-based plan only).",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.root:
        cfg.scan.root = args.root
    if args.provider:
        cfg.ai.provider = args.provider
    if args.no_cache:
        cfg.cache.enabled = False

    if args.advise:
        _run_advise(cfg, use_ai=not args.no_ai)
        return

    UltimateSacrificeApp(cfg).run()


def _run_advise(cfg: Config, use_ai: bool = True) -> None:
    """Headless disk advisor: scan -> analyze -> print plan. No TUI, deletes nothing."""
    import asyncio
    import time

    from .ai import build_provider, provider_identity  # noqa: F401
    from .analysis import analyze_with_ai
    from .analysis.report import render_plan
    from .scanner.walker import Scanner

    root = cfg.scan.root
    print(f"Scanning {root} (min {cfg.scan.min_size_mb} MB)… this walks the whole tree.", flush=True)
    scanner = Scanner(min_size_bytes=cfg.scan.min_size_bytes, top_n=100000)
    nodes = scanner.scan(root)
    print(f"  {len(nodes)} candidates found. Analyzing…", flush=True)

    provider = None
    if use_ai:
        provider = build_provider(cfg.ai.provider, cfg.ai)

    plan = asyncio.run(analyze_with_ai(nodes, root, time.time(), provider=provider))
    _safe_print(render_plan(plan))


def _safe_print(text: str) -> None:
    """Print UTF-8, tolerating legacy Windows consoles (cp1252) that can't encode it."""
    import sys

    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write(text.encode(enc, errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
