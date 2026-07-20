"""AI provider layer for cleanup assessment."""

from __future__ import annotations

from .base import AIProvider, AssessRequest, Assessment
from .assessor import Assessor
from .cache import AssessmentCache

_PROVIDERS = ("ollama", "claude_cli", "anthropic")


def _resolve(name: str) -> str:
    name = (name or "ollama").lower()
    if name in ("claude_cli", "claude", "claude-cli"):
        return "claude_cli"
    return name


def build_provider(name: str, config) -> AIProvider:
    """Instantiate a provider by name using fields from an AIConfig-like object."""
    name = _resolve(name)
    if name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(model=config.ollama_model, host=config.ollama_host)
    if name == "claude_cli":
        from .claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(model=config.claude_model)
    if name == "anthropic":
        from .anthropic_api import AnthropicProvider

        return AnthropicProvider(model=config.anthropic_model)
    raise ValueError(f"Unknown AI provider {name!r}; expected one of {_PROVIDERS}")


def provider_identity(config) -> str:
    """Stable ``provider:model`` string used as part of the cache key.

    Verdicts differ per model, so a cached qwen verdict must never be reused for a
    claude run. Mirrors the provider->model mapping in ``build_provider``.
    """
    name = _resolve(config.provider)
    model = {
        "ollama": config.ollama_model,
        "claude_cli": config.claude_model,
        "anthropic": config.anthropic_model,
    }.get(name, "")
    return f"{name}:{model}"


__all__ = [
    "AIProvider",
    "AssessRequest",
    "Assessment",
    "Assessor",
    "AssessmentCache",
    "build_provider",
    "provider_identity",
]
