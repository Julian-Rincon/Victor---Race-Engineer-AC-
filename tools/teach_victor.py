#!/usr/bin/env python3
"""Enseña a Victor a distinguir voz real de ruido de motor — usando Piper como
generador de datos de entrenamiento/calibración, 100% offline.

Qué hace:
  1. Sintetiza con Piper las frases de comando reales que el piloto usa
     (ver COMMANDS más abajo) — esto es "voz que SÍ debe disparar" al SpeechGate.
  2. Genera ruido sintético tipo motor/tráfico a varios niveles de energía —
     "ruido que NO debe disparar".
  3. Corre ambos corpus por SpeechGate con distintos umbrales de Silero VAD y mide:
       - sensibilidad = % de comandos que sí dispararon "start"
       - falsos positivos = % de clips de ruido que dispararon "start"
  4. Elige el umbral más bajo que mantenga falsos positivos en 0 y sensibilidad
     máxima, y lo persiste en data/victor_calibration.json.

Uso:
  python3 tools/teach_victor.py            # calibra y guarda
  python3 tools/teach_victor.py --report    # solo mide con la calibración actual
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).parent.parent))
import victor_config as cfg
from victor_ears import SileroVad, SpeechGate, _VAD_FRAME  # noqa: E402

PIPER_SR = 22050
VAD_SR = 16000

# Comandos reales que el piloto dice durante una carrera (de engineer.py _MUTE_KW etc.)
COMMANDS = [
    "cállate un momento",
    "estado completo por favor",
    "cuánto combustible me queda",
    "cuál es el gap con el de adelante",
    "cómo están los neumáticos",
    "cuándo entro a pits",
    "en qué posición voy",
    "análisis de sectores",
    "tengo daño en el coche",
    "repite lo último que dijiste",
    "activa el modo gap",
    "victor dame un resumen",
]

RNG = np.random.default_rng(42)


def _synthesize_piper(text: str) -> np.ndarray:
    from piper.voice import PiperVoice
    voice = PiperVoice.load(str(cfg.PIPER_MODEL_PATH))
    chunks = list(voice.synthesize(text))
    pcm = np.concatenate([np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])
    resampled = resample_poly(pcm.astype(np.float64), VAD_SR, PIPER_SR)
    return resampled.astype(np.int16)


def _engine_noise(seconds: float, rms_target: float) -> np.ndarray:
    """Ruido marrón (1/f^2) + un par de armónicos graves — se parece más al zumbido
    de motor real que el ruido blanco, y es justo el caso difícil para un gate por RMS."""
    n = int(seconds * VAD_SR)
    white = RNG.standard_normal(n)
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


def _run_through_gate(pcm: np.ndarray, vad: SileroVad, threshold: float) -> bool:
    """True si el gate llegó a disparar 'start' en algún punto del clip."""
    gate = SpeechGate(vad, vad_threshold=threshold, confirm_frames=4, rms_prefilter=200.0)
    n_frames = len(pcm) // _VAD_FRAME
    for i in range(n_frames):
        frame = pcm[i * _VAD_FRAME:(i + 1) * _VAD_FRAME]
        if gate.push(frame) == "start":
            return True
    return False


def evaluate(threshold: float, commands_pcm: list[np.ndarray], noise_pcm: list[np.ndarray],
             vad: SileroVad) -> tuple[float, float]:
    hits = sum(_run_through_gate(pcm, vad, threshold) for pcm in commands_pcm)
    false_pos = sum(_run_through_gate(pcm, vad, threshold) for pcm in noise_pcm)
    sensitivity = hits / len(commands_pcm)
    fp_rate = false_pos / len(noise_pcm)
    return sensitivity, fp_rate


def build_corpus() -> tuple[list[np.ndarray], list[np.ndarray]]:
    print(f"[Teach] Sintetizando {len(COMMANDS)} comandos con Piper...")
    commands_pcm = [_synthesize_piper(c) for c in COMMANDS]

    print("[Teach] Generando corpus de ruido de motor (12 clips, RMS 300-3000)...")
    noise_pcm = [_engine_noise(2.5, rms) for rms in np.linspace(300, 3000, 12)]
    return commands_pcm, noise_pcm


def calibrate() -> dict:
    vad = SileroVad(cfg.SILERO_VAD_PATH)
    commands_pcm, noise_pcm = build_corpus()

    best = None
    for threshold in np.arange(0.40, 0.91, 0.05):
        vad.reset()
        sens, fp = evaluate(round(threshold, 2), commands_pcm, noise_pcm, vad)
        print(f"  umbral={threshold:.2f}  sensibilidad={sens:.0%}  falsos_positivos={fp:.0%}")
        # Entre los umbrales con 0% falsos positivos y sensibilidad máxima, preferimos
        # el MÁS ALTO (más margen de seguridad) — el corpus de ruido sintético es más
        # limpio que el ruido real de motor/público que Victor va a enfrentar en pista.
        if fp == 0.0 and sens >= 0.95:
            if best is None or sens > best[1] or (sens == best[1] and threshold > best[0]):
                best = (round(threshold, 2), sens, fp)

    if best is None:
        # Ningún umbral logró 0 falsos positivos — toma el que minimice FP y reporta advertencia.
        results = []
        for threshold in np.arange(0.40, 0.91, 0.05):
            vad.reset()
            sens, fp = evaluate(round(threshold, 2), commands_pcm, noise_pcm, vad)
            results.append((round(threshold, 2), sens, fp))
        best = min(results, key=lambda r: (r[2], -r[1]))
        print(f"[Teach] ADVERTENCIA: ningún umbral logró 0% falsos positivos. "
              f"Mejor compromiso: {best}")

    threshold, sensitivity, fp_rate = best
    cal = cfg.load_calibration()
    cal.update({
        "vad_threshold": threshold,
        "vad_end_threshold": max(0.2, threshold - 0.25),
        "sensitivity": sensitivity,
        "false_positive_rate": fp_rate,
        "calibrated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })
    cfg.save_calibration(cal)
    print(f"\n[Teach] Calibración final: umbral={threshold} "
          f"sensibilidad={sensitivity:.0%} falsos_positivos={fp_rate:.0%}")
    print(f"[Teach] Guardado en {cfg.CALIBRATION_PATH}")
    return cal


def report() -> dict:
    vad = SileroVad(cfg.SILERO_VAD_PATH)
    commands_pcm, noise_pcm = build_corpus()
    cal = cfg.load_calibration()
    sens, fp = evaluate(cal["vad_threshold"], commands_pcm, noise_pcm, vad)
    print(f"[Teach] Calibración actual (umbral={cal['vad_threshold']}): "
          f"sensibilidad={sens:.0%} falsos_positivos={fp:.0%}")
    return cal


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        calibrate()
