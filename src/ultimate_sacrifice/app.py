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
    grid-rows: auto auto 1fr auto auto auto;
    padding: 1 2;
    width: 90;
    max-width: 100%;
    height: auto;
    max-height: 90%;
    border: heavy $error;
    background: $surface;
}
#confirm-title {
    color: $primary;
    text-style: bold;
}
#confirm-mode {
    color: $foreground;
    margin-bottom: 1;
}
#confirm-list {
    height: 1fr;
    min-height: 5;
    max-height: 15;
    border: round $secondary;
    background: $background;
}
#confirm-buttons {
    height: auto;
    margin-top: 1;
}
#confirm-buttons Button {
    margin-right: 2;
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
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.root:
        cfg.scan.root = args.root
    if args.provider:
        cfg.ai.provider = args.provider
    if args.no_cache:
        cfg.cache.enabled = False

    UltimateSacrificeApp(cfg).run()


if __name__ == "__main__":
    main()
