#!/usr/bin/env python3
"""
Tests del spotter v6 — overlap métrico estilo CrewChief.

Antes: "al lado" = gap temporal en spline < 1.2 s (≈50 m a ritmo de carrera →
falsos "coche a la derecha" constantes) y sin debounce (jitter a 10 Hz).
Ahora: overlap real por geometría (largo de coche), zona de 20 m, confirmación
de aparición y retardo de "despejado", y aviso de 3-wide.

Ejecutar CON AC parado:  python3 test_spotter_v6.py
"""
import sys
import time as _time_mod
from unittest import mock

sys.modules['pyaudio'] = mock.MagicMock()
_piper_mock = mock.MagicMock()
_piper_mock.PiperVoice.load.return_value = mock.MagicMock()
sys.modules['piper'] = _piper_mock
sys.modules['piper.voice'] = _piper_mock

import engineer as eng  # noqa: E402

PASS, FAIL = [], []
spoken: list[str] = []
eng._tts_pq.put = lambda item: spoken.append(item[1]) if item[1] else None

_now = [1000.0]
_real_time = _time_mod.time
_time_mod.time = lambda: _now[0]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra and not cond else ""))


def t_with_car(long_m: float, lat_m: float, lap: int = 5) -> eng.Telemetry:
    """Nuestro coche en (0,0) mirando +z a 150 km/h; rival a (lat, long) metros."""
    t = eng.Telemetry()
    t.status = 2
    t.session = 2
    t.track_len = 4318.0
    t.speed = 150.0
    t.lap = lap
    t.spline = 0.500000
    t.wx, t.wz = 0.0, 0.0
    t.vx, t.vz = 0.0, 41.7
    t.cars_data = [{
        "idx": 1, "race_pos": 2, "lap": lap,
        "spline": t.spline + long_m / t.track_len,
        "speed": 150.0,
        "wx": lat_m, "wz": long_m,
        "driver_name": "Ayrton Senna", "in_pit": False,
    }]
    return t


def t_clear() -> eng.Telemetry:
    t = t_with_car(0, 0)
    t.cars_data = []
    return t


def advance(secs: float):
    _now[0] += secs


def feed(t, times: int = 1, dt: float = 0.1):
    for _ in range(times):
        advance(dt)
        eng._run_spotter(t)


def reset():
    spoken.clear()
    eng._reset_spotter()
    spoken.clear()


# ── 1. Coche 10 m DETRÁS (misma línea) no es "al lado" ───────────────────────
print("[1] Coche 10 m detrás → silencio")
reset()
feed(t_with_car(long_m=-10.0, lat_m=0.5), times=8)
check("sin anuncio con rival a 10 m detrás", not spoken, f"spoken={spoken}")

# ── 2. Coche 25 m delante con offset lateral → fuera de zona ─────────────────
print("\n[2] Coche a 25 m → fuera de zona spotter")
reset()
feed(t_with_car(long_m=25.0, lat_m=2.0), times=8)
check("sin anuncio con rival a 25 m", not spoken, f"spoken={spoken}")

# ── 3. Overlap real a la derecha → anuncia tras confirmación ─────────────────
print("\n[3] Overlap real derecha")
reset()
feed(t_with_car(long_m=1.0, lat_m=2.2), times=1)
check("primer frame NO anuncia (debounce)", not spoken, f"spoken={spoken}")
feed(t_with_car(long_m=1.0, lat_m=2.2), times=3)
check("tras confirmación anuncia derecha",
      any("derecha" in s.lower() for s in spoken), f"spoken={spoken}")

# ── 4. "Despejado" con retardo, no al primer frame limpio ────────────────────
print("\n[4] Clear con retardo")
spoken.clear()
feed(t_clear(), times=2)          # 0.2 s limpio — aún no
check("0.2 s limpio: aún sin 'despejado'", not spoken, f"spoken={spoken}")
feed(t_clear(), times=9)          # ~1.1 s limpio total
check("tras retardo anuncia despejado",
      any("despejado" in s.lower() for s in spoken), f"spoken={spoken}")

# ── 5. Jitter de 1 frame no dispara clear+re-anuncio ─────────────────────────
print("\n[5] Jitter de 1 frame")
reset()
feed(t_with_car(1.0, 2.2), times=4)   # anunciado
spoken.clear()
feed(t_clear(), times=1)               # 1 frame sin señal
feed(t_with_car(1.0, 2.2), times=3)    # vuelve
check("sin mensajes espurios por jitter", not spoken, f"spoken={spoken}")

# ── 6. Tres coches en paralelo → aviso de ambos lados ────────────────────────
print("\n[6] Three-wide")
reset()
t3 = t_with_car(1.0, 2.2)
t3.cars_data.append({
    "idx": 2, "race_pos": 4, "lap": 5,
    "spline": t3.spline, "speed": 150.0,
    "wx": -2.2, "wz": -0.5,
    "driver_name": "Alain Prost", "in_pit": False,
})
feed(t3, times=4)
check("anuncia ambos lados (three-wide)",
      any("ambos" in s.lower() for s in spoken), f"spoken={spoken}")

# ── RESUMEN ──────────────────────────────────────────────────────────────────
_time_mod.time = _real_time
print("\n" + "=" * 60)
print(f"  {len(PASS)} OK, {len(FAIL)} FAIL" + (f" → {FAIL}" if FAIL else ""))
print("=" * 60)
sys.exit(1 if FAIL else 0)
