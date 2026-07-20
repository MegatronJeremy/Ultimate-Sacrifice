"""Tests for prompt building and response parsing with canned model output."""

from __future__ import annotations

from ultimate_sacrifice.ai.base import AssessRequest, fallback_assessment
from ultimate_sacrifice.ai.prompt import build_prompt, parse_response


def req(**kw) -> AssessRequest:
    base = dict(
        path=r"C:\p\node_modules",
        is_dir=True,
        size_bytes=500 * 1024 * 1024,
        stale_days=200.0,
        category="build-artifact",
        junk_score=0.85,
        flags=["build-artifact", "stale>180d"],
    )
    base.update(kw)
    return AssessRequest(**base)


def test_prompt_includes_metrics():
    p = build_prompt(req())
    assert "node_modules" in p
    assert "build-artifact" in p
    assert "200" in p  # stale days
    assert "JSON" in p


def test_parse_clean_json():
    r = req()
    text = '{"recommendation": "delete", "confidence": 0.9, "reason": "regenerable deps"}'
    a = parse_response(text, r)
    assert a.recommendation == "delete"
    assert a.confidence == 0.9
    assert a.source == "ai"
    assert "regenerable" in a.reason


def test_parse_json_with_fences_and_prose():
    r = req()
    text = 'Sure!\n```json\n{"recommendation":"review","confidence":0.4,"reason":"large archive"}\n```\n'
    a = parse_response(text, r)
    assert a.recommendation == "review"
    assert a.confidence == 0.4


def test_parse_embedded_json_in_text():
    r = req()
    text = 'The verdict is {"recommendation": "keep", "confidence": 0.7, "reason": "recent"} for this.'
    a = parse_response(text, r)
    assert a.recommendation == "keep"


def test_parse_garbage_falls_back_to_heuristics():
    r = req(junk_score=0.9)
    a = parse_response("I cannot help with that.", r)
    assert a.source == "heuristic-fallback"
    # junk_score 0.9 -> delete in fallback.
    assert a.recommendation == "delete"


def test_parse_invalid_recommendation_falls_back():
    r = req(junk_score=0.2)
    a = parse_response('{"recommendation": "nuke", "confidence": 1.0}', r)
    assert a.source == "heuristic-fallback"
    assert a.recommendation == "keep"  # low junk score


def test_confidence_clamped():
    r = req()
    a = parse_response('{"recommendation":"delete","confidence":5,"reason":"x"}', r)
    assert a.confidence == 1.0


def test_fallback_confidence_capped():
    a = fallback_assessment(req(junk_score=1.0), "no ai")
    assert a.confidence <= 0.6
