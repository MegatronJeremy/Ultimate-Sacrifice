"""Headless smoke test for Ultimate-Sacrifice.

Exercises the real pipeline end-to-end with no TUI: scan a root, restore/assess
with the configured AI provider, then print what would be deleted and how much
space that reclaims. Deletes nothing (assessment only) — it's a read-only probe
you can run after any change to confirm the pipeline still produces sane verdicts.

Usage:
    python scripts/smoke_test.py                       # scans a built-in temp tree
    python scripts/smoke_test.py --root C:\\Dev         # scan a real folder
    python scripts/smoke_test.py --provider ollama --min-size-mb 50
    python scripts/smoke_test.py --no-ai               # heuristics only, no provider

Exit code 0 on success, 1 if the pipeline errored.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile

# Allow running from a source checkout without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ultimate_sacrifice.ai import Assessor, build_provider, provider_identity  # noqa: E402
from ultimate_sacrifice.ai.base import Assessment  # noqa: E402
from ultimate_sacrifice.config import human_size, load_config  # noqa: E402
from ultimate_sacrifice.scanner import heuristics  # noqa: E402
from ultimate_sacrifice.scanner.model import ScanNode  # noqa: E402
from ultimate_sacrifice.scanner.walker import Scanner  # noqa: E402

_REC_ORDER = {"delete": 0, "review": 1, "keep": 2}


def _build_demo_tree() -> str:
    """A throwaway tree with a mix of junk, a container, and a loose big file.

    Created next to the current working directory (NOT under %TEMP%) so the temp/
    cache heuristics behave as they would on a real target — a demo tree inside
    %TEMP% would be flagged temp-cache wholesale and misrepresent the pipeline.
    """
    root = tempfile.mkdtemp(prefix="us_smoke_", dir=os.getcwd())

    def w(rel: str, mb: float) -> None:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"\0" * int(mb * 1024 * 1024))

    w(os.path.join("MyApp", "node_modules", "left-pad", "index.js"), 60)
    w(os.path.join("MyApp", "build", "out.o"), 40)
    w(os.path.join("MyApp", "src", "main.cpp"), 30)
    w(os.path.join("Videos", "vacation.mp4"), 90)
    w("old-installer.zip", 55)
    w(os.path.join("cache", "blob.dat"), 45)
    return root


def _fmt_row(node: ScanNode, a: Assessment | None) -> str:
    container = heuristics.is_container(node)
    if container:
        verdict = "protected(container)"
    elif a is not None:
        verdict = f"{a.recommendation} {a.confidence:.0%} [{a.source}]"
    else:
        verdict = "-"
    return f"  {human_size(node.size):>10}  {node.category:<16} {verdict:<28} {node.path}"


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.provider:
        cfg.ai.provider = args.provider
    if args.min_size_mb is not None:
        cfg.scan.min_size_mb = args.min_size_mb

    using_demo = not args.root
    root = args.root or _build_demo_tree()
    # The demo files are tens of MB; lower the threshold so they surface (unless the
    # caller pinned one). Real --root scans keep the configured default.
    if using_demo and args.min_size_mb is None:
        cfg.scan.min_size_mb = 20

    try:
        print(f"Scanning {root} (min {cfg.scan.min_size_mb} MB, top {cfg.scan.top_n})...")
        scanner = Scanner(min_size_bytes=cfg.scan.min_size_bytes, top_n=cfg.scan.top_n)
        nodes = scanner.scan(root)
        if not nodes:
            print("No candidates found above the size threshold.")
            return 0

        assessable = [n for n in nodes if not heuristics.is_container(n)]
        assessments: dict[str, Assessment] = {}

        if args.no_ai:
            print("Skipping AI (--no-ai); heuristics only.\n")
        else:
            provider = build_provider(cfg.ai.provider, cfg.ai)
            available = await provider.available()
            status = "available" if available else "UNAVAILABLE (will fall back to heuristics)"
            print(f"Provider: {provider.name} — {status}")
            print(f"Assessing {len(assessable)} items...\n")
            assessor = Assessor(provider, concurrency=cfg.ai.concurrency)
            identity = provider_identity(cfg.ai)
            results = await assessor.assess(assessable, identity=identity)
            assessments = {a.path: a for a in results}

        # Report.
        print("Candidates (largest first; containers protected):")
        for n in nodes:
            print(_fmt_row(n, assessments.get(n.path)))

        to_delete = [n for n in assessable if assessments.get(n.path) and assessments[n.path].recommendation == "delete"]
        to_review = [n for n in assessable if assessments.get(n.path) and assessments[n.path].recommendation == "review"]
        reclaim = sum(n.size for n in to_delete)
        review_bytes = sum(n.size for n in to_review)

        print("\n" + "=" * 60)
        print(f"Recommended for DELETION: {len(to_delete)} item(s) — {human_size(reclaim)} reclaimable")
        for n in to_delete:
            print(f"  - {human_size(n.size):>10}  {n.path}")
        print(f"Flagged for REVIEW: {len(to_review)} item(s) — {human_size(review_bytes)}")
        print("=" * 60)
        return 0
    finally:
        if using_demo:
            shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", help="Folder to scan (default: a built-in demo tree).")
    p.add_argument("--provider", choices=("ollama", "claude_cli", "anthropic"))
    p.add_argument("--config")
    p.add_argument("--min-size-mb", type=int)
    p.add_argument("--no-ai", action="store_true", help="Skip the AI; report heuristics only.")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except Exception as exc:  # noqa: BLE001 - smoke harness reports and fails loudly
        print(f"[error] smoke test failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
