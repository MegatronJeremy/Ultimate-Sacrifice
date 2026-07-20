"""Textual screens: scan config, results table, and delete confirmation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Select,
    Static,
)

from ..ai import Assessor, build_provider, provider_identity
from ..ai.base import Assessment
from ..cleanup.deleter import delete_many, is_guarded
from ..config import human_size
from ..scanner import heuristics
from ..scanner.model import ScanNode, ScanProgress
from ..scanner.walker import Scanner
from .theme import BANNER, TAGLINE, verdict_style

if TYPE_CHECKING:
    from ..app import UltimateSacrificeApp

# After assessment, items the AI is at least this confident are safe to delete get
# pre-selected for you; everything else is left for a manual decision.
_AUTO_SELECT_CONFIDENCE = 0.85


class ScanConfigScreen(Screen):
    """First screen: choose root, threshold, provider, and options."""

    def compose(self) -> ComposeResult:
        cfg = self.app.cfg  # type: ignore[attr-defined]
        yield Header()
        with VerticalScroll(id="config-body"):
            yield Static(BANNER.strip("\n"), id="banner")
            yield Static(TAGLINE, id="tagline")
            yield Static("AI disk scanner & cleanup", id="title")
            yield Label("Folder to scan:")
            yield Input(value=os.path.expanduser(cfg.scan.root), id="root-input")
            yield Label("Minimum item size (MB):")
            yield Input(value=str(cfg.scan.min_size_mb), id="minsize-input", type="integer")
            yield Label("AI provider:")
            yield Select(
                [
                    ("Ollama (local, default)", "ollama"),
                    ("Claude CLI (your account, no key)", "claude_cli"),
                    ("Anthropic API (needs key)", "anthropic"),
                ],
                value=cfg.ai.provider,
                allow_blank=False,
                id="provider-select",
            )
            with Horizontal(id="config-toggles"):
                yield Checkbox("Recycle Bin (recoverable)", value=cfg.cleanup.use_recycle_bin, id="bin-toggle")
                yield Checkbox("Dry run (delete nothing)", value=cfg.cleanup.dry_run, id="dry-toggle")
            yield Button("Scan", variant="primary", id="scan-btn")
            yield Static("", id="config-status")
        yield Footer()

    @on(Button.Pressed, "#scan-btn")
    def start_scan(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        root = self.query_one("#root-input", Input).value.strip()
        if not root or not os.path.isdir(os.path.expanduser(root)):
            self.query_one("#config-status", Static).update("[red]Path is not a folder.[/red]")
            return
        try:
            min_mb = int(self.query_one("#minsize-input", Input).value or "100")
        except ValueError:
            min_mb = 100

        app.cfg.scan.root = root
        app.cfg.scan.min_size_mb = max(1, min_mb)
        app.cfg.ai.provider = str(self.query_one("#provider-select", Select).value)
        app.cfg.cleanup.use_recycle_bin = self.query_one("#bin-toggle", Checkbox).value
        app.cfg.cleanup.dry_run = self.query_one("#dry-toggle", Checkbox).value

        app.push_screen(ResultsScreen())


class ResultsScreen(Screen):
    """Second screen: scan results table + AI assessment + selection."""

    BINDINGS = [
        ("space", "toggle_row", "Select"),
        ("a", "assess", "Assess with AI"),
        ("d", "delete", "Delete selected"),
        ("r", "rescan", "Rescan"),
        ("escape", "back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[ScanNode] = []
        self.selected: set[str] = set()
        self.assessments: dict[str, Assessment] = {}
        self._row_to_path: dict[object, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("Scanning…", id="results-status")
            yield ProgressBar(total=None, id="scan-progress", show_eta=False)
            table = DataTable(id="results-table", cursor_type="row", zebra_stripes=True)
            yield table
            yield Static("", id="detail-panel")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.add_column("Sel", key="sel", width=4)
        table.add_column("Size", key="size", width=10)
        table.add_column("Age", key="age", width=8)
        table.add_column("Category", key="cat", width=16)
        table.add_column("Junk", key="junk", width=6)
        table.add_column("AI", key="ai", width=11)  # trailing * = restored from cache
        table.add_column("Path", key="path")
        self.run_scan()

    # ---- scanning -------------------------------------------------------

    @work(thread=True, exclusive=True)
    def run_scan(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        cfg = app.cfg

        def on_progress(p: ScanProgress) -> None:
            app.call_from_thread(self._update_progress, p)

        scanner = Scanner(
            min_size_bytes=cfg.scan.min_size_bytes,
            top_n=cfg.scan.top_n,
            progress_cb=on_progress,
        )
        self._scanner = scanner
        nodes = scanner.scan(cfg.scan.root)
        app.call_from_thread(self._scan_done, nodes)

    def _update_progress(self, p: ScanProgress) -> None:
        status = self.query_one("#results-status", Static)
        status.update(
            f"Scanning… {p.entries:,} entries, {human_size(p.bytes_seen)} seen, "
            f"{p.errors} skipped — {p.current_path[:70]}"
        )

    def _scan_done(self, nodes: list[ScanNode]) -> None:
        self.nodes = nodes
        self.query_one("#scan-progress", ProgressBar).update(total=1, progress=1)
        restored = self._restore_from_cache()
        auto = self._auto_select_confident()  # tick confident deletes from cached verdicts
        self._rebuild_table()
        total = sum(n.size for n in nodes)
        cache_note = f" [dim]{restored} restored from cache.[/dim]" if restored else ""
        auto_note = f" [b]{auto}[/b] auto-selected." if auto else ""
        self.query_one("#results-status", Static).update(
            f"[b]{len(nodes)}[/b] candidates, {human_size(total)} total.{cache_note}{auto_note} "
            f"Press [b]a[/b] to assess with AI, [b]space[/b] to select, [b]d[/b] to delete."
        )

    def _restore_from_cache(self) -> int:
        """Pre-fill assessments for unchanged files from the persistent cache."""
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        cache = getattr(app, "cache", None)
        if cache is None:
            return 0
        identity = provider_identity(app.cfg.ai)
        restored = 0
        for node in self.nodes:
            if heuristics.is_container(node):
                continue  # containers are never AI-assessed, so nothing to restore
            hit = cache.get(node, identity)
            if hit is not None:
                self.assessments[node.path] = hit
                restored += 1
        return restored

    def _auto_select_confident(self) -> int:
        """Pre-select high-confidence 'delete' verdicts; leave everything else.

        Never auto-selects a guarded path (e.g. a stray `delete` verdict on a
        `.vhdx`/`pagefile.sys`) — the same block that applies to manual selection.
        Returns how many rows this newly ticked.
        """
        newly = 0
        for node in self.nodes:
            if node.path in self.selected:
                continue
            a = self.assessments.get(node.path)
            if a is None or a.recommendation != "delete":
                continue
            if a.confidence < _AUTO_SELECT_CONFIDENCE:
                continue
            guarded, _ = is_guarded(node.path)
            if guarded:
                continue
            self.selected.add(node.path)
            newly += 1
        return newly

    def _rebuild_table(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear()
        self._row_to_path.clear()
        for node in self.nodes:
            key = table.add_row(*self._row_cells(node))
            self._row_to_path[key.value if hasattr(key, "value") else key] = node.path

    def _row_cells(self, node: ScanNode) -> tuple:
        from rich.text import Text

        container = heuristics.is_container(node)
        sel = "—" if container else ("[x]" if node.path in self.selected else "[ ]")
        age_days = 0.0
        # derive age lazily from mtime for display
        import time

        if node.mtime > 0:
            age_days = max(0.0, (time.time() - max(node.mtime, node.atime)) / 86400.0)
        age = f"{age_days:.0f}d" if age_days else "-"
        if container:
            cat_cell = Text("container", style=verdict_style("container"))
            ai_cell = Text("protected", style=verdict_style("protected"))
        else:
            cat_cell = Text(node.category)
            a = self.assessments.get(node.path)
            if a:
                mark = "*" if a.source == "ai-cached" else ""
                ai_cell = Text(
                    f"{a.recommendation} {a.confidence:.0%}{mark}",
                    style=verdict_style(a.recommendation),
                )
            else:
                ai_cell = Text("-")
        return (
            sel,
            human_size(node.size),
            age,
            cat_cell,
            f"{node.junk_score:.2f}",
            ai_cell,
            node.path,
        )

    def _current_path(self) -> str | None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return self._row_to_path.get(row_key.value if hasattr(row_key, "value") else row_key)

    # ---- interactions ---------------------------------------------------

    def action_toggle_row(self) -> None:
        path = self._current_path()
        if not path:
            return
        if path in self.selected:
            self.selected.discard(path)
        else:
            node = next((n for n in self.nodes if n.path == path), None)
            if node is not None and heuristics.is_container(node):
                self.query_one("#detail-panel", Static).update(
                    "[yellow]Protected container — not a cleanup target.[/yellow]"
                )
                return
            guarded, reason = is_guarded(path)
            if guarded:
                self.query_one("#detail-panel", Static).update(f"[red]Protected: {reason}[/red]")
                return
            self.selected.add(path)
        self._refresh_row(path)

    def _refresh_row(self, path: str) -> None:
        node = next((n for n in self.nodes if n.path == path), None)
        if node is None:
            return
        table = self.query_one("#results-table", DataTable)
        for key, p in self._row_to_path.items():
            if p == path:
                cells = self._row_cells(node)
                for col, val in zip(("sel", "size", "age", "cat", "junk", "ai", "path"), cells):
                    table.update_cell(key, col, val)
                break

    @on(DataTable.RowHighlighted)
    def show_detail(self, _event: DataTable.RowHighlighted) -> None:
        path = self._current_path()
        if not path:
            return
        node = next((n for n in self.nodes if n.path == path), None)
        a = self.assessments.get(path)
        lines = [f"[b]{path}[/b]"]
        if node:
            lines.append(f"{node.category} · {human_size(node.size)} · signals: {', '.join(node.flags) or 'none'}")
        if a:
            lines.append(f"[{verdict_style(a.recommendation)}]AI: {a.recommendation} "
                         f"({a.confidence:.0%}, {a.source})[/] — {a.reason}")
        self.query_one("#detail-panel", Static).update("\n".join(lines))

    def action_assess(self) -> None:
        if not self.nodes:
            return
        self.query_one("#results-status", Static).update("Assessing with AI…")
        self.assess_worker()

    @work(exclusive=True)
    async def assess_worker(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        # Only assess items that don't already have a verdict (cache pre-fill covers the rest);
        # containers are known-keep, so never spend AI time/tokens on them.
        cached = len(self.assessments)
        pending = [
            n for n in self.nodes
            if n.path not in self.assessments and not heuristics.is_container(n)
        ]
        if not pending:
            self.query_one("#results-status", Static).update(
                f"All {cached} candidates already assessed (from cache). Nothing new to do."
            )
            return

        provider = build_provider(app.cfg.ai.provider, app.cfg.ai)
        if not await provider.available():
            self.query_one("#results-status", Static).update(
                f"[yellow]Provider '{provider.name}' unavailable — using local heuristics only.[/yellow]"
            )
        assessor = Assessor(provider, concurrency=app.cfg.ai.concurrency)
        identity = provider_identity(app.cfg.ai)

        done = 0
        total = len(pending)

        def on_result(a: Assessment) -> None:
            nonlocal done
            done += 1
            self.assessments[a.path] = a
            self._refresh_row(a.path)
            self.query_one("#results-status", Static).update(f"Assessing… {done}/{total}")

        await assessor.assess(
            pending,
            on_result=lambda a: app.call_later(on_result, a),
            cache=getattr(app, "cache", None),
            identity=identity,
        )
        if getattr(app, "cache", None) is not None:
            app.cache.save()

        auto = self._auto_select_confident()
        for path in self.selected:
            self._refresh_row(path)  # show the auto-ticked checkboxes
        n_del = sum(1 for a in self.assessments.values() if a.recommendation == "delete")
        sel_bytes = sum(n.size for n in self.nodes if n.path in self.selected)
        cache_note = f" ({cached} reused from cache)" if cached else ""
        self.query_one("#results-status", Static).update(
            f"Assessment complete. {n_del} flagged for deletion{cache_note}. "
            f"[b]{auto} auto-selected[/b] ({human_size(sel_bytes)}) — review, adjust with "
            f"[b]space[/b], then [b]d[/b] to delete."
        )

    def action_delete(self) -> None:
        if not self.selected:
            self.query_one("#results-status", Static).update("[yellow]Nothing selected. Press space to select rows.[/yellow]")
            return
        paths = list(self.selected)
        total = sum(n.size for n in self.nodes if n.path in self.selected)
        self.app.push_screen(ConfirmDeleteScreen(paths, total), self._after_delete)

    def _after_delete(self, deleted: list[str] | None) -> None:
        if not deleted:
            return
        removed = set(deleted)
        self.nodes = [n for n in self.nodes if n.path not in removed]
        self.selected -= removed
        self._rebuild_table()
        self.query_one("#results-status", Static).update(
            f"Deleted {len(removed)} item(s). {len(self.nodes)} candidates remain."
        )

    def action_rescan(self) -> None:
        self.selected.clear()
        self.assessments.clear()
        self.run_scan()

    def action_back(self) -> None:
        self.app.pop_screen()


class ConfirmDeleteScreen(ModalScreen):
    """Modal: confirm deletion of the selected items."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, paths: list[str], total_bytes: int) -> None:
        super().__init__()
        self.paths = paths
        self.total_bytes = total_bytes

    def compose(self) -> ComposeResult:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        bin_mode = app.cfg.cleanup.use_recycle_bin
        dry = app.cfg.cleanup.dry_run
        mode = "DRY RUN — nothing will be deleted" if dry else (
            "Recycle Bin (recoverable)" if bin_mode else "[red]PERMANENT delete[/red]"
        )
        with Grid(id="confirm-dialog"):
            yield Label(f"[b]Delete {len(self.paths)} item(s) — {human_size(self.total_bytes)}?[/b]", id="confirm-title")
            yield Label(f"Method: {mode}", id="confirm-mode")
            with VerticalScroll(id="confirm-list"):
                for p in self.paths[:50]:
                    yield Static(f"• {p}")
                if len(self.paths) > 50:
                    yield Static(f"…and {len(self.paths) - 50} more")
            if not bin_mode and not dry:
                yield Label("Type DELETE to confirm permanent removal:")
                yield Input(id="confirm-input", placeholder="DELETE")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="confirm-yes")
                yield Button("Cancel", id="confirm-no")
            yield Static("", id="confirm-status")

    @on(Button.Pressed, "#confirm-no")
    def cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-yes")
    def confirm(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        if not app.cfg.cleanup.use_recycle_bin and not app.cfg.cleanup.dry_run:
            typed = self.query_one("#confirm-input", Input).value.strip()
            if typed != "DELETE":
                self.query_one("#confirm-status", Static).update("[red]Type DELETE to confirm.[/red]")
                return
        self.query_one("#confirm-status", Static).update("Deleting…")
        self.do_delete()

    @work(thread=True, exclusive=True)
    def do_delete(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        results = delete_many(
            self.paths,
            use_recycle_bin=app.cfg.cleanup.use_recycle_bin,
            dry_run=app.cfg.cleanup.dry_run,
        )
        deleted = [r.path for r in results if r.ok]
        app.call_from_thread(self.dismiss, deleted)
