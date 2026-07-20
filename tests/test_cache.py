"""Tests for the persistent assessment cache."""

from __future__ import annotations

import os

from ultimate_sacrifice.ai.base import Assessment, fallback_assessment, AssessRequest
from ultimate_sacrifice.ai.cache import AssessmentCache
from ultimate_sacrifice.scanner.model import Kind, ScanNode

IDENTITY = "ollama:qwen3:8b"


def make_node(path: str, size: int = 1000, mtime: float = 111.0) -> ScanNode:
    return ScanNode(path=path, kind=Kind.FILE, size=size, mtime=mtime, atime=mtime)


def ai_verdict(path: str) -> Assessment:
    return Assessment(path=path, recommendation="delete", confidence=0.9, reason="stale", source="ai")


def test_put_get_roundtrip_hit(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"))
    c.put(node, ai_verdict(node.path), IDENTITY)
    hit = c.get(node, IDENTITY)
    assert hit is not None
    assert hit.recommendation == "delete"
    assert hit.confidence == 0.9
    assert hit.source == "ai-cached"  # tagged so the UI can mark it


def test_miss_when_size_changes(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"), size=1000)
    c.put(node, ai_verdict(node.path), IDENTITY)
    changed = make_node(node.path, size=2000, mtime=node.mtime)
    assert c.get(changed, IDENTITY) is None


def test_miss_when_mtime_changes(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"), mtime=100.0)
    c.put(node, ai_verdict(node.path), IDENTITY)
    changed = make_node(node.path, size=node.size, mtime=200.0)
    assert c.get(changed, IDENTITY) is None


def test_miss_on_different_provider_identity(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"))
    c.put(node, ai_verdict(node.path), IDENTITY)
    assert c.get(node, "claude_cli:sonnet") is None


def test_miss_after_prompt_version_bump(tmp_path, monkeypatch):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"))
    c.put(node, ai_verdict(node.path), IDENTITY)
    # Simulate a prompt/schema change: the stored entry's version no longer matches.
    import ultimate_sacrifice.ai.cache as cache_mod

    monkeypatch.setattr(cache_mod, "PROMPT_VERSION", 999)
    assert c.get(node, IDENTITY) is None


def test_fallback_verdicts_not_cached(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    node = make_node(str(tmp_path / "a.bin"))
    req = AssessRequest(
        path=node.path, is_dir=False, size_bytes=node.size,
        stale_days=0, category="other", junk_score=0.9, flags=[],
    )
    c.put(node, fallback_assessment(req, "provider down"), IDENTITY)
    assert len(c) == 0
    assert c.get(node, IDENTITY) is None


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "c.json")
    # Use a real file so save()'s dead-path pruning keeps the entry.
    real = tmp_path / "a.bin"
    real.write_bytes(b"\0" * 1000)
    node = make_node(str(real))

    c = AssessmentCache(path)
    c.put(node, ai_verdict(node.path), IDENTITY)
    c.save()
    assert os.path.exists(path)

    reloaded = AssessmentCache.load(path)
    hit = reloaded.get(node, IDENTITY)
    assert hit is not None
    assert hit.recommendation == "delete"


def test_corrupt_file_loads_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not valid json")
    c = AssessmentCache.load(str(path))
    assert len(c) == 0


def test_save_prunes_missing_paths(tmp_path):
    c = AssessmentCache(str(tmp_path / "c.json"))
    ghost = make_node(str(tmp_path / "gone.bin"))  # never created on disk
    c.put(ghost, ai_verdict(ghost.path), IDENTITY)
    assert len(c) == 1
    c.save()
    assert len(c) == 0  # pruned because the path doesn't exist
