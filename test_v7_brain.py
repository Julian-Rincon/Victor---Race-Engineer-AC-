"""Tests para victor_brain: failover en cadena y circuit breaker.
Todo mockeado — nunca llama a una API real."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import victor_brain as vb


def _make_brain(**keys):
    with patch.object(vb.cfg, "GROQ_API_KEY", keys.get("groq", "")), \
         patch.object(vb.cfg, "CEREBRAS_API_KEY", keys.get("cerebras", "")), \
         patch.object(vb.cfg, "NVIDIA_API_KEY", keys.get("nvidia", "")), \
         patch.object(vb.cfg, "GEMINI_API_KEY", keys.get("gemini", "")):
        return vb.VictorBrain()


def test_first_available_backend_wins():
    brain = _make_brain(groq="k1", cerebras="k2", nvidia="k3", gemini="k4")
    with patch.object(brain, "_call_one", return_value="respuesta groq") as m:
        reply = brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert reply == "respuesta groq"
    assert brain.last_backend == "groq"
    m.assert_called_once()


def test_falls_back_when_first_backend_raises():
    brain = _make_brain(groq="k1", cerebras="k2", nvidia="k3", gemini="k4")

    def side_effect(b, *a, **kw):
        if b.name == "groq":
            raise ConnectionError("groq caído")
        return f"respuesta {b.name}"

    with patch.object(brain, "_call_one", side_effect=side_effect):
        reply = brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert reply == "respuesta cerebras"
    assert brain.last_backend == "cerebras"


def test_all_backends_fail_returns_empty_string():
    brain = _make_brain(groq="k1", cerebras="k2")
    with patch.object(brain, "_call_one", side_effect=ConnectionError("caído")):
        reply = brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert reply == ""
    assert brain.last_backend is None


def test_backend_without_key_is_skipped():
    brain = _make_brain(groq="", cerebras="k2")
    with patch.object(brain, "_call_one", return_value="respuesta cerebras") as m:
        reply = brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert reply == "respuesta cerebras"
    m.assert_called_once()
    called_backend = m.call_args[0][0]
    assert called_backend.name == "cerebras"


def test_circuit_breaker_pauses_backend_after_three_failures():
    brain = _make_brain(groq="k1", cerebras="k2")
    groq_backend = next(b for b in brain.backends if b.name == "groq")

    def side_effect(b, *a, **kw):
        if b.name == "groq":
            raise ConnectionError("groq caído")
        return "respuesta cerebras"

    with patch.object(brain, "_call_one", side_effect=side_effect):
        for _ in range(3):
            brain.complete("sys", [{"role": "user", "content": "hola"}])

    assert groq_backend.fails == 3
    assert groq_backend.paused_until > time.time()
    assert not groq_backend.available()

    # Cuarta llamada: groq ya está pausado, ni se intenta (solo cerebras se llama)
    with patch.object(brain, "_call_one", side_effect=side_effect) as m:
        brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert m.call_count == 1
    assert m.call_args[0][0].name == "cerebras"


def test_success_resets_failure_count():
    brain = _make_brain(groq="k1")
    groq_backend = brain.backends[0]
    groq_backend.fails = 2
    with patch.object(brain, "_call_one", return_value="ok"):
        brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert groq_backend.fails == 0


def test_reasoning_model_gets_extra_token_budget():
    brain = _make_brain(cerebras="k2")
    cerebras_backend = brain.backends[1]
    assert cerebras_backend.reasoning_reserve > 0

    captured = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    def fake_post(url, headers, json, timeout):
        captured["max_tokens"] = json["max_tokens"]
        return FakeResp()

    with patch.object(vb.requests, "post", side_effect=fake_post):
        brain.complete("sys", [{"role": "user", "content": "hola"}], max_tokens=100)
    assert captured["max_tokens"] == 100 + cerebras_backend.reasoning_reserve


def test_truncated_reasoning_response_counts_as_failure_not_empty_reply():
    """Respuesta sin 'content' (cortada en razonamiento) debe activar fallback,
    NO devolverse como '' silencioso que pasaría un fallo real por bueno."""
    brain = _make_brain(cerebras="k2", gemini="k4")

    class TruncatedResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"reasoning": "pensando...", "role": "assistant"}}]}

    class OkResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "respuesta gemini"}}]}

    def fake_post(url, headers, json, timeout):
        if "cerebras" in url:
            return TruncatedResp()
        return OkResp()

    with patch.object(vb.requests, "post", side_effect=fake_post):
        reply = brain.complete("sys", [{"role": "user", "content": "hola"}])
    assert reply == "respuesta gemini"
    assert brain.last_backend == "gemini"


def test_status_reports_paused_backends():
    brain = _make_brain(groq="k1")
    groq_backend = brain.backends[0]
    groq_backend.fails = 3
    groq_backend.paused_until = time.time() + 100
    status = brain.status()
    assert "pausado" in status["groq"]
