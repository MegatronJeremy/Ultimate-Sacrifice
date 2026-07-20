"""Local Ollama provider (default). Talks to the Ollama HTTP API.

Zero-config when Ollama is running locally with a chat model pulled (e.g.
``qwen3:8b``). Uses ``format: json`` to nudge structured output; the parser is
still tolerant of stray prose.
"""

from __future__ import annotations

import httpx

from .base import AssessRequest, Assessment, fallback_assessment
from .prompt import build_prompt, parse_response


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str = "qwen3:8b",
        host: str = "http://localhost:11434",
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.host}/api/tags")
                resp.raise_for_status()
                tags = resp.json().get("models", [])
                names = {m.get("name", "") for m in tags}
                # Accept exact or ':latest'-normalized match; if we can't tell, assume ok.
                return not names or self.model in names or f"{self.model}:latest" in names or any(
                    n.split(":")[0] == self.model.split(":")[0] for n in names
                )
        except (httpx.HTTPError, ValueError):
            return False

    async def assess_one(self, request: AssessRequest) -> Assessment:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_prompt(request)}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.host}/api/chat", json=payload)
                resp.raise_for_status()
                content = resp.json().get("message", {}).get("content", "")
        except httpx.HTTPError as exc:
            return fallback_assessment(request, f"Ollama request failed ({exc.__class__.__name__}).")
        return parse_response(content, request)
