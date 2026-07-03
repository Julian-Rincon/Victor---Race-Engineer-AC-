#!/usr/bin/env python3
"""
Tests v6: mejoras post-auditoría (2026-07-03).

1. fuel_laps calculado desde el histórico de vueltas (la fuente SHM no lo trae)
2. Splits acumulados desde transiciones de sector (SHM trae sector_index +
   last_sector_ms, no arrays de splits)
3. Gating por status del juego (LIVE=2): sin alertas en menú/replay/pausa

Ejecutar CON AC parado:  python3 test_v6_improvements.py
"""
import sys
from unittest import mock

sys.modules['pyaudio'] = mock.MagicMock()
_piper_mock = mock.MagicMock()
_piper_mock.PiperVoice.load.return_value = mock.MagicMock()
sys.modules['piper'] = _piper_mock
sys.modules['piper.voice'] = _piper_mock

import engineer as eng  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra and not cond else ""))


# ─── 1. fuel_laps desde histórico ────────────────────────────────────────────
print("[1] _calc_fuel_laps")
eng._lap_history.clear()
check("sin histórico → 0.0", eng._calc_fuel_laps(30.0) == 0.0)

for lap, used in ((1, 2.0), (2, 2.2), (3, 2.1)):
    eng._lap_history.append(eng.LapRecord(lap=lap, time_ms=95000, fuel_used=used, pos=3))
got = eng._calc_fuel_laps(10.5)
check("promedio últimas 3 vueltas (10.5L / 2.1L/v = 5.0v)", abs(got - 5.0) < 0.01, f"got={got}")
check("fuel 0 → 0.0", eng._calc_fuel_laps(0.0) == 0.0)

# Outlier de vuelta en pits (consumo ~0) se ignora
eng._lap_history.append(eng.LapRecord(lap=4, time_ms=180000, fuel_used=0.01, pos=3))
got = eng._calc_fuel_laps(10.5)
check("vuelta de pits (0.01L) ignorada", abs(got - 5.0) < 0.01, f"got={got}")

# ─── 2. Splits por transiciones de sector ────────────────────────────────────
print("\n[2] _ingest_sector")
eng._reset_sector_state()

# Vuelta 5: sector 0 → 1 → 2 → (vuelta 6) 0
eng._ingest_sector(0, 0, 5)          # arranque de vuelta, nada que registrar
eng._ingest_sector(1, 30500, 5)      # completó S1 en 30500
eng._ingest_sector(2, 31200, 5)      # completó S2 en 31200
eng._ingest_sector(0, 29900, 6)      # completó S3 → vuelta cerrada
check("last_splits de la vuelta = [30500, 31200, 29900]",
      eng._last_lap_splits() == [30500, 31200, 29900], f"got={eng._last_lap_splits()}")
check("best_splits inicial = primera vuelta completa",
      eng._best_sector_splits() == [30500, 31200, 29900])

# Vuelta 6: mejora S2, empeora S1
eng._ingest_sector(1, 30800, 6)
eng._ingest_sector(2, 30900, 6)
eng._ingest_sector(0, 30100, 7)
check("last_splits vuelta 6", eng._last_lap_splits() == [30800, 30900, 30100])
check("best elementwise (ideal lap) = [30500, 30900, 29900]",
      eng._best_sector_splits() == [30500, 30900, 29900], f"got={eng._best_sector_splits()}")

# Sector repetido (mismo paquete varias veces) no duplica
eng._ingest_sector(1, 31000, 7)
eng._ingest_sector(1, 31000, 7)
eng._ingest_sector(2, 30000, 7)
eng._ingest_sector(0, 30000, 8)
check("paquetes repetidos no rompen la vuelta",
      eng._last_lap_splits() == [31000, 30000, 30000])

# Vuelta incompleta (entró a pits, se saltó sectores) no contamina best
eng._reset_sector_state()
eng._ingest_sector(0, 0, 1)
eng._ingest_sector(2, 45000, 1)      # salto 0→2 (telemetría perdida)
eng._ingest_sector(0, 31000, 2)
check("vuelta incompleta → last_splits vacío", eng._last_lap_splits() == [])

# ─── 3. Gating por status ────────────────────────────────────────────────────
print("\n[3] _is_live")
t = eng.Telemetry()
t.status = 2
check("LIVE (2) → True", eng._is_live(t) is True)
for s, name in ((0, "OFF"), (1, "REPLAY"), (3, "PAUSE")):
    t.status = s
    check(f"{name} ({s}) → False", eng._is_live(t) is False)

t_default = eng.Telemetry()
check("default del dataclass es LIVE (compat con bridge sin status)",
      eng._is_live(t_default) is True)

# ─── RESUMEN ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  {len(PASS)} OK, {len(FAIL)} FAIL" + (f" → {FAIL}" if FAIL else ""))
print("=" * 60)
sys.exit(1 if FAIL else 0)
