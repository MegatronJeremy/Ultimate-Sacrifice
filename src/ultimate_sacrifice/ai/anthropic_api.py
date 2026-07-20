"""Anthropic Messages API provider (optional). Requires ANTHROPIC_API_KEY.

Only useful when you want pay-per-token cloud inference with an explicit key,
rather than the logged-in Claude CLI. Uses a direct httpx call so we don't pull in
the anthropic SDK as a hard dependency.
"""

from __future__ import annotations

import os

import httpx

from .base import AssessRequest, Assessment, fallback_assessment
from .prompt import build_prompt, parse_response

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout = timeout

    async def available(self) -> bool:
        return bool(self.api_key)

    async def assess_one(self, request: AssessRequest) -> Assessment:
        if not self.api_key:
            return fallback_assessment(request, "ANTHROPIC_API_KEY is not set.")

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 256,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": build_prompt(request)}],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(_API_URL, headers=headers, json=payload)
                resp.raise_for_status()
                blocks = resp.json().get("content", [])
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except httpx.HTTPError as exc:
            return fallback_assessment(request, f"Anthropic API request failed ({exc.__class__.__name__}).")
        return parse_response(text, request)
