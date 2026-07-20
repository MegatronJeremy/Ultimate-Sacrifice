"""Textual screens: scan config, results table, and delete confirmation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
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
from .theme import BANNER, GOLD as _GOLD, SELECTED_BG as _SELECTED_BG, TAGLINE, verdict_style

if TYPE_CHECKING:
    from ..app import UltimateSacrificeApp

# After assessment, items the AI is at least this confident are safe to delete get
# pre-selected for you; everything else is left for a manual decision.
_AUTO_SELECT_CONFIDENCE = 0.85

# Sort modes cycled with 's'. Each is (label, key(node) -> sortable). All sort
# descending except name/age where ascending reads more naturally; we handle that
# per-mode in _apply_view. Containers are always forced last regardless of sort.
_REC_RANK = {"delete": 0, "review": 1, "keep": 2, None: 3}

_SORT_MODES = ("size", "verdict", "junk", "age")

# Filter modes cycled with 'f': predicate over (node, assessment|None).
_FILTER_MODES = ("all", "deletes", "review", "unassessed")


def _top_level_of(current: str, root: str) -> str:
    """The first path segment of ``current`` below ``root``, for a stable progress label.

    Walking deep trees, the raw current_path flickers; showing "which top-level folder
    under the root are we in" is calmer and more informative. Falls back to a truncated
    current path if it isn't under root.
    """
    if not current:
        return ""
    cn = os.path.normcase(os.path.normpath(current))
    rn = os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(root))))
    if cn.startswith(rn):
        rest = cn[len(rn):].lstrip("\\/")
        if rest:
            return rest.split("\\", 1)[0].split("/", 1)[0]
        return "…"
    return current[-48:]


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

    # Footer shows only the core task-flow keys (show=True); secondary actions are
    # hidden from the footer to keep it readable but still work and are listed in the
    # `?` Help overlay (which renders every binding, shown or not). key_display fixes
    # glyphs that would otherwise render oddly (Enter/Backspace) or crowd the label.
    BINDINGS = [
        Binding("space", "toggle_row", "Select", key_display="space"),
        Binding("a", "assess", "Assess", key_display="a"),
        Binding("d", "delete", "Delete", key_display="d"),
        Binding("enter", "drill_in", "Drill in", key_display="enter"),
        Binding("s", "cycle_sort", "Sort", key_display="s"),
        Binding("f", "cycle_filter", "Filter", key_display="f"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("escape", "back", "Back", key_display="esc"),
        # --- secondary: hidden from footer, still active, shown in ? Help ---
        Binding("A", "select_recommended", "Select all deletes", show=False),
        Binding("c", "clear_selection", "Clear selection", show=False),
        Binding("v", "invert_selection", "Invert selection", show=False),
        Binding("backspace", "drill_up", "Drill up", key_display="bksp", show=False),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("x", "cancel_scan", "Cancel scan", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[ScanNode] = []
        self.selected: set[str] = set()
        self.assessments: dict[str, Assessment] = {}
        self._row_to_path: dict[object, str] = {}
        # Display view = ordered/filtered subset of self.nodes that the table renders.
        self._display: list[ScanNode] = []
        self._sort_mode = "size"
        self._filter_mode = "all"
        self._scanner: Scanner | None = None
        self._scanning = False
        # Drill navigation: breadcrumb of (root, min_size_bytes) frames. Frame 0 is the
        # original scan; drilling into a folder pushes a frame, backspace pops one.
        self._scan_stack: list[tuple[str, int]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("", id="context-bar")
            yield Static("", id="breadcrumb")
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
        # Seed the base scan frame from config.
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        self._scan_stack = [(app.cfg.scan.root, app.cfg.scan.min_size_bytes)]
        self.run_scan()

    # ---- scanning -------------------------------------------------------

    @property
    def _current_frame(self) -> tuple[str, int]:
        """(root, min_size_bytes) of the scan currently displayed."""
        return self._scan_stack[-1]

    @work(thread=True, exclusive=True)
    def run_scan(self, preserve_all: bool = False) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        root, min_bytes = self._current_frame
        self._scanning = True

        def on_progress(p: ScanProgress) -> None:
            app.call_from_thread(self._update_progress, p)

        scanner = Scanner(
            min_size_bytes=min_bytes,
            top_n=app.cfg.scan.top_n,
            progress_cb=on_progress,
        )
        self._scanner = scanner
        nodes = scanner.scan(root)
        self._scanning = False
        app.call_from_thread(self._scan_done, nodes, scanner._progress.cancelled, preserve_all)

    def action_cancel_scan(self) -> None:
        if self._scanning and self._scanner is not None:
            self._scanner.cancel = True
            self.query_one("#results-status", Static).update("Cancelling scan…")

    def _update_progress(self, p: ScanProgress) -> None:
        rate = int(p.entries / p.elapsed_s) if p.elapsed_s > 0 else 0
        # Show the top-level dir currently being walked, not the deep current path.
        top = _top_level_of(p.current_path, self._current_frame[0])
        status = self.query_one("#results-status", Static)
        status.update(
            f"[b]Scanning…[/b] {p.entries:,} entries · {human_size(p.bytes_seen)} · "
            f"{p.elapsed_s:.0f}s · {rate:,}/s · {p.errors} skipped   "
            f"[dim]{top}[/dim]   [b]x[/b] to cancel"
        )

    def _scan_done(
        self, nodes: list[ScanNode], cancelled: bool = False, preserve_all: bool = False
    ) -> None:
        # On drill navigation we NARROW the view, so paths from other frames are absent
        # but still valid — don't drop their selections/verdicts. Only an explicit rescan
        # of the same root (r) reconciles against genuine disappearance.
        dropped = 0 if preserve_all else self._reconcile_after_scan(nodes)
        self.nodes = nodes
        self.query_one("#scan-progress", ProgressBar).update(total=1, progress=1)
        self._update_breadcrumb()
        restored = self._restore_from_cache()
        auto = self._auto_select_confident()  # tick confident deletes from cached verdicts
        self._rebuild_table()
        self._update_context()
        total = sum(n.size for n in nodes)
        cancel_note = "[yellow]Scan cancelled — partial results.[/yellow] " if cancelled else ""
        cache_note = f" [dim]{restored} restored from cache.[/dim]" if restored else ""
        auto_note = f" [b]{auto}[/b] auto-selected." if auto else ""
        drop_note = f" [dim]{dropped} prior selection(s) dropped.[/dim]" if dropped else ""
        depth = len(self._scan_stack) - 1
        _, min_bytes = self._current_frame
        drill_note = (
            f"[b]Drilled in[/b] (≥ {human_size(min_bytes)}, [b]backspace[/b] to go up). "
            if depth > 0 else ""
        )
        self.query_one("#results-status", Static).update(
            f"{cancel_note}{drill_note}[b]{len(nodes)}[/b] candidates, {human_size(total)} total."
            f"{cache_note}{auto_note}{drop_note} [b]enter[/b] a folder, [b]a[/b] assess, "
            f"[b]?[/b] keys."
        )

    def _update_breadcrumb(self) -> None:
        """Render the drill path as `root › … › current` (empty at the base frame)."""
        crumb = self.query_one("#breadcrumb", Static)
        if len(self._scan_stack) <= 1:
            crumb.update("")
            crumb.display = False
            return
        root = self._current_frame[0]
        parts = [p for p in os.path.normpath(root).replace("/", "\\").split("\\") if p]
        shown = parts if len(parts) <= 4 else ["…", *parts[-3:]]
        crumb.update(" › ".join(shown))
        crumb.display = True

    def _update_context(self) -> None:
        """Refresh the title/context bar: where we are + counts + selection totals."""
        root = self._current_frame[0]
        total = sum(n.size for n in self.nodes)
        sel_bytes = sum(n.size for n in self.nodes if n.path in self.selected)
        n_sel = len(self.selected)
        sel_part = (
            f"[$accent]{n_sel} selected · {human_size(sel_bytes)}[/]"
            if n_sel else "[dim]nothing selected[/dim]"
        )
        self.query_one("#context-bar", Static).update(
            f"[b]{root}[/b]\n"
            f"{len(self.nodes)} items · {human_size(total)} · {sel_part}"
        )

    def _reconcile_after_scan(self, nodes: list[ScanNode]) -> int:
        """Keep selections/verdicts that still apply to the fresh node set.

        A prior selection survives only if the path is still a candidate and is still
        selectable (not newly guarded/container). Verdicts for paths that vanished are
        dropped so stale rows don't linger. Returns how many selections were dropped.
        """
        by_path = {n.path: n for n in nodes}
        kept_sel: set[str] = set()
        for path in self.selected:
            node = by_path.get(path)
            if node is not None and self._selectable(node):
                kept_sel.add(path)
        dropped = len(self.selected) - len(kept_sel)
        self.selected = kept_sel
        self.assessments = {p: a for p, a in self.assessments.items() if p in by_path}
        return dropped

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

    def _apply_view(self) -> None:
        """Recompute the ordered/filtered display list from nodes + sort/filter state."""

        def passes(node: ScanNode) -> bool:
            if self._filter_mode == "all":
                return True
            a = self.assessments.get(node.path)
            if self._filter_mode == "deletes":
                return a is not None and a.recommendation == "delete"
            if self._filter_mode == "review":
                return a is not None and a.recommendation == "review"
            if self._filter_mode == "unassessed":
                # Containers are never assessed; don't count them as "unassessed" clutter.
                return a is None and not heuristics.is_container(node)
            return True

        def sort_key(node: ScanNode):
            # Containers always sink to the bottom, matching the scanner's own ranking.
            container = heuristics.is_container(node)
            if self._sort_mode == "size":
                return (container, -node.size)
            if self._sort_mode == "junk":
                return (container, -node.junk_score)
            if self._sort_mode == "age":
                age = max(node.mtime, node.atime)
                return (container, age)  # oldest first (smallest timestamp)
            if self._sort_mode == "verdict":
                a = self.assessments.get(node.path)
                rank = _REC_RANK[a.recommendation if a else None]
                return (container, rank, -node.size)
            return (container, -node.size)

        self._display = sorted((n for n in self.nodes if passes(n)), key=sort_key)

    def _rebuild_table(self) -> None:
        self._apply_view()
        table = self.query_one("#results-table", DataTable)
        table.clear()
        self._row_to_path.clear()
        for node in self._display:
            key = table.add_row(*self._row_cells(node))  # RowKey; use as-is for update_cell
            self._row_to_path[key] = node.path

    def _row_cells(self, node: ScanNode) -> tuple:
        from rich.text import Text

        container = heuristics.is_container(node)
        selected = node.path in self.selected
        # NOTE: cells must be Text objects, not raw strings — a plain "[x]" is parsed as
        # console markup and renders as nothing. Text() is shown literally.
        if container:
            sel_cell = Text("—", style=verdict_style("container"))
        elif selected:
            sel_cell = Text("✓", style=f"bold {_GOLD}")
        else:
            sel_cell = Text("·", style="dim")

        age_days = 0.0
        # derive age lazily from mtime for display
        import time

        if node.mtime > 0:
            age_days = max(0.0, (time.time() - max(node.mtime, node.atime)) / 86400.0)
        age = Text(f"{age_days:.0f}d" if age_days else "-")
        size_cell = Text(human_size(node.size))
        junk_cell = Text(f"{node.junk_score:.2f}")
        path_cell = Text(node.path)
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

        cells = (sel_cell, size_cell, age, cat_cell, junk_cell, ai_cell, path_cell)
        # Tint the whole row so a selection stands out even when the cursor is elsewhere.
        if selected:
            for c in cells:
                c.stylize(f"on {_SELECTED_BG}")
        return cells

    def _current_path(self) -> str | None:
        table = self.query_one("#results-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return self._row_to_path.get(row_key)

    # ---- sort / filter --------------------------------------------------

    def action_cycle_sort(self) -> None:
        i = _SORT_MODES.index(self._sort_mode)
        self._sort_mode = _SORT_MODES[(i + 1) % len(_SORT_MODES)]
        self._rebuild_table()
        self._update_view_status()

    def action_cycle_filter(self) -> None:
        i = _FILTER_MODES.index(self._filter_mode)
        self._filter_mode = _FILTER_MODES[(i + 1) % len(_FILTER_MODES)]
        self._rebuild_table()
        self._update_view_status()

    def _update_view_status(self) -> None:
        shown = len(self._display)
        sel_bytes = sum(n.size for n in self.nodes if n.path in self.selected)
        self.query_one("#results-status", Static).update(
            f"Sort: [b]{self._sort_mode}[/b]  ·  Filter: [b]{self._filter_mode}[/b]  ·  "
            f"showing [b]{shown}[/b]/{len(self.nodes)}  ·  selected [b]{len(self.selected)}[/b] "
            f"({human_size(sel_bytes)}). [b]?[/b] for keys."
        )
        self._update_context()

    # ---- bulk selection -------------------------------------------------

    def _selectable(self, node: ScanNode) -> bool:
        """True if a node may be selected (not a container, not a guarded path)."""
        if heuristics.is_container(node):
            return False
        return not is_guarded(node.path)[0]

    def action_select_recommended(self) -> None:
        """Select every AI 'delete' verdict (any confidence), skipping guarded/containers."""
        added = 0
        for node in self.nodes:
            if node.path in self.selected or not self._selectable(node):
                continue
            a = self.assessments.get(node.path)
            if a is not None and a.recommendation == "delete":
                self.selected.add(node.path)
                added += 1
        self._refresh_all_visible()
        self._update_view_status()

    def action_clear_selection(self) -> None:
        self.selected.clear()
        self._refresh_all_visible()
        self._update_view_status()

    def action_invert_selection(self) -> None:
        """Invert selection among selectable nodes only (containers/guarded stay off)."""
        for node in self.nodes:
            if not self._selectable(node):
                continue
            if node.path in self.selected:
                self.selected.discard(node.path)
            else:
                self.selected.add(node.path)
        self._refresh_all_visible()
        self._update_view_status()

    def _refresh_all_visible(self) -> None:
        for path in list(self._row_to_path.values()):
            self._refresh_row(path)

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
        self._update_context()

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

    @on(DataTable.RowSelected)
    def _on_row_selected(self, _event: DataTable.RowSelected) -> None:
        # DataTable emits RowSelected on Enter (and consumes the key), so drill-in is
        # wired here rather than via a screen 'enter' binding that never fires.
        self.action_drill_in()

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
        self._update_context()
        self.query_one("#results-status", Static).update(
            f"Deleted {len(removed)} item(s). {len(self.nodes)} candidates remain."
        )

    def action_rescan(self) -> None:
        # Keep manual selections and verdicts across a rescan; _scan_done reconciles
        # them against the fresh node set (dropping paths that vanished or became
        # guarded/container). Cache-backed assessments also refill for unchanged files.
        self.run_scan()

    def action_drill_in(self) -> None:
        """Re-scan the highlighted directory as a new root with an auto-scaled threshold.

        Files can't be drilled into. Directories (including protected containers like
        C:\\Dev, a Downloads folder, or media dirs) can — that's how you find where the
        space actually went and surface many-small-files that don't clear the base
        threshold (issues #3 + #4).
        """
        if self._scanning:
            return
        path = self._current_path()
        node = next((n for n in self.nodes if n.path == path), None)
        if node is None:
            return
        if not node.is_dir:
            self.query_one("#detail-panel", Static).update(
                "[yellow]Can't drill into a file — only directories.[/yellow]"
            )
            return
        threshold = heuristics.drill_threshold(node.size)
        self._scan_stack.append((node.path, threshold))
        self.query_one("#results-status", Static).update(f"Scanning {node.name}…")
        self.run_scan(preserve_all=True)

    def action_drill_up(self) -> None:
        """Pop one drill frame and re-scan the parent view (no-op at the base frame)."""
        if self._scanning:
            return
        if len(self._scan_stack) <= 1:
            self.query_one("#detail-panel", Static).update(
                "[dim]Already at the top of the scan.[/dim]"
            )
            return
        self._scan_stack.pop()
        self.query_one("#results-status", Static).update("Returning…")
        self.run_scan(preserve_all=True)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen(self.BINDINGS))

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
        if dry:
            mode = "[b]Dry run[/b] — nothing will actually be deleted"
        elif bin_mode:
            mode = "[b $success]Recycle Bin[/] — recoverable"
        else:
            mode = "[b $error]PERMANENT delete[/] — not recoverable"

        with Grid(id="confirm-dialog"):
            yield Label(
                f"Delete [b]{len(self.paths)}[/b] item(s) · "
                f"[b]{human_size(self.total_bytes)}[/b] to reclaim",
                id="confirm-title",
            )
            yield Label(mode, id="confirm-mode")
            yield Label("[dim]These items will be removed:[/dim]", id="confirm-list-label")
            with VerticalScroll(id="confirm-list"):
                for p in self.paths[:200]:
                    yield Static(f"  • {p}")
                if len(self.paths) > 200:
                    yield Static(f"  [dim]…and {len(self.paths) - 200} more[/dim]")
            # Progress row (hidden until deletion starts).
            yield ProgressBar(total=len(self.paths), id="delete-progress", show_eta=False)
            if not bin_mode and not dry:
                yield Label("Type [b]DELETE[/b] to confirm permanent removal:", id="confirm-prompt")
                yield Input(id="confirm-input", placeholder="DELETE")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="confirm-yes")
                yield Button("Cancel", id="confirm-no")
            yield Static("", id="confirm-status")

    def on_mount(self) -> None:
        # Progress bar is only meaningful during deletion — hide it up front.
        self.query_one("#delete-progress", ProgressBar).display = False

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
                self.query_one("#confirm-status", Static).update("[$error]Type DELETE to confirm.[/]")
                return
        # Enter delete mode: reveal progress, lock the buttons so nothing double-fires.
        self.query_one("#confirm-yes", Button).disabled = True
        self.query_one("#confirm-no", Button).disabled = True
        bar = self.query_one("#delete-progress", ProgressBar)
        bar.display = True
        bar.update(total=len(self.paths), progress=0)
        self.query_one("#confirm-status", Static).update(f"Deleting 0/{len(self.paths)}…")
        self.do_delete()

    @work(thread=True, exclusive=True)
    def do_delete(self) -> None:
        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        freed = 0

        def on_progress(done: int, total: int, r) -> None:
            nonlocal freed
            if r.ok:
                freed += r.freed_bytes
            app.call_from_thread(self._show_delete_progress, done, total, freed)

        results = delete_many(
            self.paths,
            use_recycle_bin=app.cfg.cleanup.use_recycle_bin,
            dry_run=app.cfg.cleanup.dry_run,
            on_progress=on_progress,
        )
        deleted = [r.path for r in results if r.ok]
        app.call_from_thread(self.dismiss, deleted)

    def _show_delete_progress(self, done: int, total: int, freed: int) -> None:
        self.query_one("#delete-progress", ProgressBar).update(progress=done)
        self.query_one("#confirm-status", Static).update(
            f"Deleting [b]{done}/{total}[/b] · [b]{human_size(freed)}[/b] freed…"
        )


# Friendlier display names for special key identifiers used in BINDINGS.
_KEY_DISPLAY = {
    "question_mark": "?",
    "space": "space",
    "escape": "esc",
}


def binding_rows(bindings) -> list[tuple[str, str]]:
    """Flatten a Textual BINDINGS list into (key, description) pairs.

    Accepts both tuple bindings ``(key, action, description)`` and ``Binding``
    objects, so it renders whatever the screen actually declares — the help can
    never drift from the real bindings (issue #7). This lists ALL bindings, including
    those hidden from the footer (``show=False``), so Help stays complete.
    """
    rows: list[tuple[str, str]] = []
    for b in bindings:
        if isinstance(b, tuple):
            key = b[0]
            desc = b[2] if len(b) > 2 else ""
            key_display = ""
        else:  # textual.binding.Binding
            key = getattr(b, "key", "")
            desc = getattr(b, "description", "")
            key_display = getattr(b, "key_display", "") or ""
        if not desc:
            continue
        shown = key_display or _KEY_DISPLAY.get(key, key)
        rows.append((shown, desc))
    return rows


class HelpScreen(ModalScreen):
    """Modal overlay listing keyboard shortcuts, rendered from a screen's BINDINGS."""

    BINDINGS = [("escape", "dismiss", "Close"), ("question_mark", "dismiss", "Close")]

    def __init__(self, source_bindings) -> None:
        super().__init__()
        self._rows = binding_rows(source_bindings)

    def compose(self) -> ComposeResult:
        with Grid(id="help-dialog"):
            yield Label("[b]Keyboard & Mouse Shortcuts[/b]", id="help-title")
            with VerticalScroll(id="help-list"):
                for key, desc in self._rows:
                    yield Static(f"  [b]{key:<8}[/b]  {desc}")
            yield Static("[dim]Press ? or Esc to close.[/dim]", id="help-footer")

    def action_dismiss(self) -> None:
        self.dismiss(None)
