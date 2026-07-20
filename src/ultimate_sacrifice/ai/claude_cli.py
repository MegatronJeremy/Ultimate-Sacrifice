"""Claude CLI provider — uses the user's already-authenticated `claude` CLI.

No API key required: it shells out to `claude -p <prompt> --output-format json
--model <model>`, which reuses the login the CLI already has. The CLI returns an
envelope like ``{"result": "<assistant text>", ...}``; we pull ``result`` and run
it through the shared parser.
"""

from __future__ import annotations

import asyncio
import json
import shutil

from .base import AssessRequest, Assessment, fallback_assessment
from .prompt import build_prompt, parse_response


class ClaudeCliProvider:
    name = "claude_cli"

    def __init__(self, model: str = "sonnet", timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout
        self._exe = shutil.which("claude")

    async def available(self) -> bool:
        return self._exe is not None

    async def assess_one(self, request: AssessRequest) -> Assessment:
        if self._exe is None:
            return fallback_assessment(request, "`claude` CLI not found on PATH.")

        args = [
            self._exe,
            "-p",
            build_prompt(request),
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return fallback_assessment(request, "Claude CLI timed out.")
        except OSError as exc:
            return fallback_assessment(request, f"Claude CLI failed to start ({exc}).")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()[:200]
            return fallback_assessment(request, f"Claude CLI error: {err or 'nonzero exit'}.")

        raw = stdout.decode("utf-8", "replace")
        content = _unwrap_cli_json(raw)
        return parse_response(content, request)

    async def complete_text(self, prompt: str) -> str:
        """Free-form text completion (used for the advisor narrative). '' on failure."""
        if self._exe is None:
            return ""
        args = [self._exe, "-p", prompt, "--output-format", "json", "--model", self.model]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except (asyncio.TimeoutError, OSError):
            return ""
        if proc.returncode != 0:
            return ""
        return _unwrap_cli_json(stdout.decode("utf-8", "replace"))


def _unwrap_cli_json(raw: str) -> str:
    """Extract the assistant text from the CLI's JSON envelope, tolerating plain text."""
    try:
        env = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(env, dict):
        # Newer CLIs: {"result": "..."}; be lenient about alternate keys.
        for key in ("result", "response", "text", "content"):
            val = env.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return raw
