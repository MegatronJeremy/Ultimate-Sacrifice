"""Configuration loading. Reads an optional TOML file; all fields have defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass(slots=True)
class ScanConfig:
    root: str = "~"
    min_size_mb: int = 100
    top_n: int = 200

    @property
    def min_size_bytes(self) -> int:
        return self.min_size_mb * 1024 * 1024


@dataclass(slots=True)
class AIConfig:
    provider: str = "ollama"
    ollama_model: str = "qwen3:8b"
    ollama_host: str = "http://localhost:11434"
    claude_model: str = "sonnet"
    anthropic_model: str = "claude-sonnet-5"
    concurrency: int = 4


@dataclass(slots=True)
class CleanupConfig:
    use_recycle_bin: bool = True
    dry_run: bool = False


@dataclass(slots=True)
class CacheConfig:
    # Reuse AI verdicts for files whose (path, size, mtime) is unchanged.
    enabled: bool = True
    # Empty -> a per-user cache dir chosen at runtime (see resolved_cache_path).
    path: str = ""


@dataclass(slots=True)
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)


def _default_config_path() -> str:
    # Prefer a config.toml next to the project root (cwd) if present.
    return os.path.join(os.getcwd(), "config.toml")


def load_config(path: str | None = None) -> Config:
    """Load config from TOML, falling back to built-in defaults for anything absent."""
    path = path or _default_config_path()
    if not path or not os.path.isfile(path):
        return Config()

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    scan_d = data.get("scan", {})
    ai_d = data.get("ai", {})
    clean_d = data.get("cleanup", {})
    cache_d = data.get("cache", {})

    scan = ScanConfig(
        root=scan_d.get("root", ScanConfig.root),
        min_size_mb=int(scan_d.get("min_size_mb", ScanConfig.min_size_mb)),
        top_n=int(scan_d.get("top_n", ScanConfig.top_n)),
    )
    ai = AIConfig(
        provider=ai_d.get("provider", AIConfig.provider),
        ollama_model=ai_d.get("ollama_model", AIConfig.ollama_model),
        ollama_host=ai_d.get("ollama_host", AIConfig.ollama_host),
        claude_model=ai_d.get("claude_model", AIConfig.claude_model),
        anthropic_model=ai_d.get("anthropic_model", AIConfig.anthropic_model),
        concurrency=int(ai_d.get("concurrency", AIConfig.concurrency)),
    )
    cleanup = CleanupConfig(
        use_recycle_bin=bool(clean_d.get("use_recycle_bin", CleanupConfig.use_recycle_bin)),
        dry_run=bool(clean_d.get("dry_run", CleanupConfig.dry_run)),
    )
    cache = CacheConfig(
        enabled=bool(cache_d.get("enabled", CacheConfig.enabled)),
        path=str(cache_d.get("path", CacheConfig.path)),
    )
    return Config(scan=scan, ai=ai, cleanup=cleanup, cache=cache)


def resolved_cache_path(cfg: CacheConfig) -> str:
    """Absolute path to the assessment cache file.

    Uses the configured path if set, else a per-user cache dir so the file is never
    written inside a scanned tree and is shared across scans.
    """
    if cfg.path:
        return os.path.abspath(os.path.expanduser(cfg.path))
    from platformdirs import user_cache_dir

    return os.path.join(user_cache_dir("ultimate-sacrifice", appauthor=False), "assessments.json")


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
