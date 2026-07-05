"""Victor v7 — cerebro multi-backend.

Cadena cloud-only con failover automático: Groq -> Cerebras -> NVIDIA NIM -> Gemini.
Mismo patrón que el TieredBrain de NEXUS (circuit breaker: 3 fallos consecutivos
pausan el backend 5 minutos). Deliberadamente SIN Ollama: Victor solo corre durante
sesiones de juego y el modelo local consume ~5GB de VRAM que ya casi se agota con
el juego + stack gráfico (ver incidente de freeze del 2026-07-03).

STT: Groq whisper-large-v3 -> Deepgram Nova-3 como respaldo.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

import victor_config as cfg

_BREAKER_FAILS   = 3
_BREAKER_PAUSE_S = 300.0


@dataclass
class _Backend:
    name: str
    api_key: str
    base_url: str
    model: str
    fails: int = 0
    paused_until: float = 0.0
    # Modelos de razonamiento (p.ej. gpt-oss-120b en Cerebras) gastan tokens en un
    # campo "reasoning" antes de escribir "content" — con poco margen se cortan
    # (finish_reason=length) y el mensaje llega SIN content. Este colchón evita
    # que el fallback falle solo por falta de espacio para razonar.
    reasoning_reserve: int = 0

    def available(self) -> bool:
        return bool(self.api_key) and time.time() >= self.paused_until

    def record_success(self) -> None:
        self.fails = 0
        self.paused_until = 0.0

    def record_failure(self) -> None:
        self.fails += 1
        if self.fails >= _BREAKER_FAILS:
            self.paused_until = time.time() + _BREAKER_PAUSE_S


class VictorBrain:
    """Chat completion con failover en cadena sobre backends OpenAI-compatibles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.backends: list[_Backend] = [
            _Backend("groq",     cfg.GROQ_API_KEY,     "https://api.groq.com/openai/v1",         cfg.GROQ_MODEL),
            _Backend("cerebras", cfg.CEREBRAS_API_KEY, "https://api.cerebras.ai/v1",              cfg.CEREBRAS_MODEL,
                     reasoning_reserve=250),
            _Backend("nvidia",   cfg.NVIDIA_API_KEY,   "https://integrate.api.nvidia.com/v1",     cfg.NVIDIA_MODEL),
            _Backend("gemini",   cfg.GEMINI_API_KEY,
                     "https://generativelanguage.googleapis.com/v1beta/openai", cfg.GEMINI_MODEL),
        ]
        self.last_backend: Optional[str] = None

    def _call_one(self, b: _Backend, system: str, messages: list, max_tokens: int, timeout: float) -> str:
        r = requests.post(
            f"{b.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {b.api_key}", "Content-Type": "application/json"},
            json={
                "model": b.model,
                "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": max_tokens + b.reasoning_reserve,
                "temperature": 0.3,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        if not content:
            # Truncado antes de escribir content (típico de modelos de razonamiento
            # con poco margen) — contarlo como fallo real, no como respuesta vacía válida.
            raise ValueError("respuesta sin content (probable corte en razonamiento)")
        return content

    def complete(self, system: str, messages: list, max_tokens: int = 110, timeout: float = 10.0) -> str:
        """Intenta cada backend disponible en orden. Devuelve "" si todos fallan."""
        with self._lock:
            candidates = [b for b in self.backends if b.available()]
        for b in candidates:
            try:
                reply = self._call_one(b, system, messages, max_tokens, timeout)
                b.record_success()
                self.last_backend = b.name
                return reply
            except Exception as e:
                print(f"[Brain:{b.name}] {e}")
                b.record_failure()
        self.last_backend = None
        return ""

    def status(self) -> dict:
        return {
            b.name: "activo" if b.available() else f"pausado ({int(b.paused_until - time.time())}s)"
            for b in self.backends
        }


# ─── STT ──────────────────────────────────────────────────────────────────────

def transcribe(wav_path: str) -> str:
    """Groq whisper-large-v3 con rechazo anti-alucinación; Deepgram Nova-3 de respaldo."""
    text = _transcribe_groq(wav_path)
    if text or not cfg.DEEPGRAM_API_KEY:
        return text
    return _transcribe_deepgram(wav_path)


def _transcribe_groq(wav_path: str) -> str:
    if not cfg.GROQ_API_KEY:
        return ""
    try:
        with open(wav_path, "rb") as f:
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg.GROQ_API_KEY}"},
                files={"file": ("rec.wav", f, "audio/wav")},
                data={"model": "whisper-large-v3", "language": "es", "response_format": "verbose_json"},
                timeout=15,
            )
        r.raise_for_status()
        data = r.json()
        text = data.get("text", "").strip()
        segments = data.get("segments", [])
        if segments:
            no_speech = max(s.get("no_speech_prob", 0) for s in segments)
            avg_logprob = sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
            if no_speech > 0.35 or avg_logprob < -0.5:
                print(f"[STT groq] Rechazado (no_speech={no_speech:.2f} logprob={avg_logprob:.2f}): {text!r}")
                return ""
        return text
    except Exception as e:
        print(f"[STT groq] {e}")
        return ""


def _transcribe_deepgram(wav_path: str) -> str:
    try:
        with open(wav_path, "rb") as f:
            audio = f.read()
        r = requests.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "language": "es", "smart_format": "true"},
            headers={"Authorization": f"Token {cfg.DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
            data=audio,
            timeout=15,
        )
        r.raise_for_status()
        alts = r.json()["results"]["channels"][0]["alternatives"]
        return alts[0]["transcript"].strip() if alts else ""
    except Exception as e:
        print(f"[STT deepgram] {e}")
        return ""
