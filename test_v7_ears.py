"""Tests para victor_ears: VAD real (Silero) y SpeechGate.

Usa Piper (voz local ya empaquetada en el proyecto) para sintetizar audio de
comando real y ruido sintético tipo motor — el mismo enfoque que
tools/teach_victor.py, pero como suite de regresión rápida y determinista.
Nunca depende de archivos fuera de este repo ni de hardware de audio real.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

import victor_config as cfg
from victor_ears import SileroVad, SpeechGate, int16_to_float32, iter_vad_frames, _VAD_FRAME

PIPER_SR = 22050
VAD_SR = 16000


def _piper_available() -> bool:
    return cfg.PIPER_MODEL_PATH.exists() and cfg.SILERO_VAD_PATH.exists()


pytestmark = pytest.mark.skipif(not _piper_available(), reason="assets de voz no disponibles en este entorno")


@pytest.fixture(scope="module")
def vad() -> SileroVad:
    return SileroVad(cfg.SILERO_VAD_PATH)


@pytest.fixture(scope="module")
def piper_command_pcm() -> np.ndarray:
    from piper.voice import PiperVoice
    voice = PiperVoice.load(str(cfg.PIPER_MODEL_PATH))
    chunks = list(voice.synthesize("victor, cuánto combustible me queda"))
    pcm = np.concatenate([np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])
    return resample_poly(pcm.astype(np.float64), VAD_SR, PIPER_SR).astype(np.int16)


def _int16_frames(pcm_int16: np.ndarray):
    """SpeechGate.push() espera frames int16 crudos (hace su propia normalización
    interna) — a diferencia de iter_vad_frames(), que ya devuelve float32 para
    alimentar SileroVad.speech_prob() directamente. No mezclar los dos."""
    n = len(pcm_int16) // _VAD_FRAME
    for i in range(n):
        yield pcm_int16[i * _VAD_FRAME:(i + 1) * _VAD_FRAME]


def _engine_noise(seconds: float, rms_target: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * VAD_SR)
    white = rng.standard_normal(n)
    brown = np.cumsum(white)
    brown -= brown.mean()
    brown /= (np.abs(brown).max() + 1e-9)
    t = np.arange(n) / VAD_SR
    hum = 0.3 * np.sin(2 * np.pi * 90 * t) + 0.2 * np.sin(2 * np.pi * 180 * t)
    sig = brown * 0.7 + hum * 0.3
    sig /= (np.abs(sig).max() + 1e-9)
    current_rms = np.sqrt(np.mean(sig ** 2))
    sig *= (rms_target / (current_rms * 32768.0 + 1e-9))
    return np.clip(sig * 32768.0, -32767, 32767).astype(np.int16)


# ─── SileroVad ────────────────────────────────────────────────────────────────

def test_silence_gives_low_probability(vad):
    vad.reset()
    frame = np.zeros(_VAD_FRAME, dtype=np.float32)
    assert vad.speech_prob(frame) < 0.2


def test_real_synthesized_speech_gives_high_probability(vad, piper_command_pcm):
    """Regresión directa del bug de contexto: sin las 64 muestras de cola del
    frame anterior, este assert fallaba (probabilidad ~0.003 incluso con voz real)."""
    vad.reset()
    max_prob = 0.0
    for frame in iter_vad_frames(piper_command_pcm):
        max_prob = max(max_prob, vad.speech_prob(frame))
    assert max_prob > 0.8, f"esperaba alta confianza de voz, dio {max_prob:.3f} — ¿volvió el bug de contexto?"


def test_wrong_frame_size_raises():
    vad_local = SileroVad(cfg.SILERO_VAD_PATH)
    with pytest.raises(ValueError):
        vad_local.speech_prob(np.zeros(256, dtype=np.float32))


def test_reset_clears_context_and_state(vad, piper_command_pcm):
    vad.reset()
    frames = list(iter_vad_frames(piper_command_pcm))
    for f in frames[:5]:
        vad.speech_prob(f)
    ctx_after_speech = vad._context.copy()
    vad.reset()
    assert np.all(vad._context == 0.0)
    assert not np.array_equal(ctx_after_speech, vad._context)  # el contexto sí cambió antes del reset


# ─── SpeechGate ───────────────────────────────────────────────────────────────

def test_gate_fires_start_on_real_command(vad, piper_command_pcm):
    vad.reset()
    gate = SpeechGate(vad, vad_threshold=0.6, confirm_frames=4, rms_prefilter=200.0)
    events = [gate.push(f) for f in _int16_frames(piper_command_pcm)]
    assert "start" in events


def test_gate_does_not_fire_on_engine_noise(vad):
    vad.reset()
    gate = SpeechGate(vad, vad_threshold=0.6, confirm_frames=4, rms_prefilter=200.0)
    noise = _engine_noise(seconds=3.0, rms_target=1800.0)
    events = [gate.push(f) for f in _int16_frames(noise)]
    assert "start" not in events, "el gate confundió ruido de motor con voz"


def test_gate_prefilter_skips_silence_without_running_vad(vad):
    vad.reset()
    gate = SpeechGate(vad, rms_prefilter=250.0)
    silent_frame = np.zeros(_VAD_FRAME, dtype=np.int16)
    result = gate.push(silent_frame)
    assert result is None
    assert gate._voice_run == 0


def test_gate_stops_after_sustained_silence(vad, piper_command_pcm):
    vad.reset()
    gate = SpeechGate(vad, vad_threshold=0.6, vad_end_threshold=0.35,
                       confirm_frames=4, silence_frames=8, rms_prefilter=200.0)
    speech_frames = list(_int16_frames(piper_command_pcm))
    silence_frames = [np.zeros(_VAD_FRAME, dtype=np.int16)] * 20

    started = False
    stopped = False
    for f in speech_frames + silence_frames:
        ev = gate.push(f)
        if ev == "start":
            started = True
        if ev == "stop":
            stopped = True
            break
    assert started, "nunca arrancó la grabación"
    assert stopped, "nunca cerró la grabación tras el silencio sostenido"
    assert not gate.is_recording


def test_gate_respects_max_frames_hard_cap(vad):
    vad.reset()
    gate = SpeechGate(vad, vad_threshold=0.01, confirm_frames=1, rms_prefilter=1.0, max_frames=10)
    # Ruido "hablado" artificial que el VAD trivialmente cruza (umbral 0.01) sin parar solo.
    loud = _engine_noise(seconds=2.0, rms_target=2000.0)
    events = [gate.push(f) for f in _int16_frames(loud)]
    assert "stop" in events, "el tope duro de frames debería forzar un stop"


# ─── Calibración persistida ────────────────────────────────────────────────────

def test_calibration_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CALIBRATION_PATH", tmp_path / "cal.json")
    cal = cfg.load_calibration()
    assert cal["vad_threshold"] == cfg.DEFAULT_CALIBRATION["vad_threshold"]
    cal["vad_threshold"] = 0.77
    cfg.save_calibration(cal)
    reloaded = cfg.load_calibration()
    assert reloaded["vad_threshold"] == 0.77
