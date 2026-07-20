"""Disk Advisor TUI screen — disk map + ranked action cards.

Shows *where the space went* and *what to do about it* before the raw table. Each
cleanup group is a selectable card; choosing one opens the results table focused on
that group's paths (pre-selected), so review/delete reuses all the existing safety
machinery (guards, confirm dialog, progress). The advisor only recommends and
pre-selects — it never deletes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from ..analysis import AdvicePlan, analyze_with_ai
from ..config import human_size
from ..scanner.model import ScanNode

if TYPE_CHECKING:
    from ..app import UltimateSacrificeApp

_BAR_WIDTH = 24
_SAFETY_STYLE = {"safe": "success", "review": "warning", "caution": "error"}


def _bar(pct: float) -> str:
    filled = int(round(pct / 100.0 * _BAR_WIDTH))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


class AdvisorScreen(Screen):
    """Disk map + ranked cleanup plan; drill from a card into the results table."""

    BINDINGS = [
        ("enter", "open_group", "Open group"),
        ("t", "show_table", "Full table"),
        ("escape", "back", "Back"),
    ]

    def __init__(self, *, nodes: list[ScanNode], root: str) -> None:
        super().__init__()
        self._scan_nodes = nodes
        self._root = root
        self._plan: AdvicePlan | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("Analyzing disk…", id="advisor-summary")
            yield Static("", id="advisor-map")
            yield Static("[b]Recommended actions[/b] — [dim]Enter to review a group, t for the full table[/dim]", id="advisor-actions-label")
            with VerticalScroll(id="advisor-cards-wrap"):
                yield OptionList(id="advisor-cards")
            yield Static("", id="advisor-narrative")
        yield Footer()

    def on_mount(self) -> None:
        self.build_plan()

    @work(exclusive=True)
    async def build_plan(self) -> None:
        import time

        app: UltimateSacrificeApp = self.app  # type: ignore[assignment]
        provider = None
        # Reuse the configured provider for the narrative, but never block the plan on it.
        if app.cfg.ai.provider:
            from ..ai import build_provider

            provider = build_provider(app.cfg.ai.provider, app.cfg.ai)
            self.query_one("#advisor-summary", Static).update("Analyzing disk… (asking AI for a strategy)")
        plan = await analyze_with_ai(self._scan_nodes, self._root, time.time(), provider=provider)
        self._plan = plan
        self._render_plan(plan)

    def _render_plan(self, plan: AdvicePlan) -> None:
        s = plan.summary
        # Summary line.
        drive = ""
        if s.drive_total_bytes:
            used = s.drive_total_bytes - s.drive_free_bytes
            drive = (
                f"  ·  drive [b]{human_size(used)}[/b] used, "
                f"[b]{human_size(s.drive_free_bytes)}[/b] free of {human_size(s.drive_total_bytes)}"
            )
        self.query_one("#advisor-summary", Static).update(
            f"[b]{human_size(s.reclaimable_bytes)}[/b] reclaimable across "
            f"[b]{s.candidate_count}[/b] items{drive}"
        )
        # Disk map bars. Use a concrete gold hex (Rich markup, not a Textual CSS token).
        map_lines = ["[b]Where the space is:[/b]"]
        for cls, b, pct in s.by_class:
            map_lines.append(f"  [#d4af37]{_bar(pct)}[/] {pct:5.1f}%  {human_size(b):>10}  {cls}")
        self.query_one("#advisor-map", Static).update("\n".join(map_lines))
        # Action cards.
        cards = self.query_one("#advisor-cards", OptionList)
        cards.clear_options()
        for i, g in enumerate(plan.groups, start=1):
            chip_style = _SAFETY_STYLE.get(g.safety.value, "warning")
            prompt = (
                f"{i:>2}. [b]{human_size(g.total_bytes):>10}[/b]  "
                f"[{chip_style}]{g.safety.value}/{g.regen_cost.value}[/]  {g.label}\n"
                f"      [dim]{g.item_count} item(s) · {g.reason}[/dim]"
            )
            cards.add_option(Option(prompt, id=str(i - 1)))
        if not plan.groups:
            self.query_one("#advisor-actions-label", Static).update(
                "[dim]Nothing actionable found above the size threshold.[/dim]"
            )
        # Narrative.
        nar = self.query_one("#advisor-narrative", Static)
        if plan.narrative:
            nar.update(f"[b #d4af37]AI strategy:[/]\n{plan.narrative}")
            nar.display = True
        else:
            nar.display = False

    @on(OptionList.OptionSelected, "#advisor-cards")
    def _card_selected(self, event: OptionList.OptionSelected) -> None:
        self._open_group_index(int(event.option.id))

    def action_open_group(self) -> None:
        cards = self.query_one("#advisor-cards", OptionList)
        if cards.highlighted is not None and self._plan and self._plan.groups:
            opt = cards.get_option_at_index(cards.highlighted)
            self._open_group_index(int(opt.id))

    def _open_group_index(self, idx: int) -> None:
        if not self._plan or idx >= len(self._plan.groups):
            return
        group = self._plan.groups[idx]
        # Import here to avoid a circular import (screens imports theme, not advisor).
        from .screens import ResultsScreen

        self.app.push_screen(
            ResultsScreen(prescanned=self._scan_nodes, root=self._root, focus_paths=set(group.paths))
        )

    def action_show_table(self) -> None:
        from .screens import ResultsScreen

        self.app.push_screen(ResultsScreen(prescanned=self._scan_nodes, root=self._root))

    def action_back(self) -> None:
        self.app.pop_screen()
