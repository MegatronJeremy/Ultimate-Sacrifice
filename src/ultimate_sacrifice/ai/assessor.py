"""Batch assessment: fan out ScanNodes over a provider with bounded concurrency."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from ..scanner import heuristics
from ..scanner.model import ScanNode
from .base import AIProvider, AssessRequest, Assessment
from .cache import AssessmentCache

ResultCb = Callable[[Assessment], None]


def node_to_request(node: ScanNode, now: float | None = None) -> AssessRequest:
    now = time.time() if now is None else now
    return AssessRequest(
        path=node.path,
        is_dir=node.is_dir,
        size_bytes=node.size,
        stale_days=heuristics.staleness_days(node, now),
        category=node.category,
        junk_score=node.junk_score,
        flags=list(node.flags),
    )


class Assessor:
    """Runs a provider over many nodes, capped at ``concurrency`` in flight."""

    def __init__(self, provider: AIProvider, concurrency: int = 4) -> None:
        self.provider = provider
        self.concurrency = max(1, concurrency)

    async def assess(
        self,
        nodes: list[ScanNode],
        on_result: ResultCb | None = None,
        cache: AssessmentCache | None = None,
        identity: str = "",
    ) -> list[Assessment]:
        """Assess all nodes; invoke ``on_result`` as each completes (for live UI).

        When a ``cache`` is given, nodes whose stored verdict is still valid skip the
        provider entirely (their cached ``Assessment`` is emitted immediately); only
        the remaining nodes hit the AI, and each fresh result is written back.
        """
        sem = asyncio.Semaphore(self.concurrency)
        now = time.time()

        results: list[Assessment] = []
        misses: list[ScanNode] = []

        if cache is not None:
            for node in nodes:
                hit = cache.get(node, identity)
                if hit is not None:
                    results.append(hit)
                    if on_result is not None:
                        on_result(hit)
                else:
                    misses.append(node)
        else:
            misses = list(nodes)

        async def run(node: ScanNode) -> Assessment:
            req = node_to_request(node, now)
            async with sem:
                result = await self.provider.assess_one(req)
            if cache is not None:
                cache.put(node, result, identity)
            if on_result is not None:
                on_result(result)
            return result

        fresh = await asyncio.gather(*(run(n) for n in misses))
        results.extend(fresh)
        return results
