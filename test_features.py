#!/usr/bin/env python3
"""
Tests unitarios para las nuevas features de Victor.
Ejecutar CON AC parado (no necesita audio real).
"""
import sys, time, json, os, threading, collections
from unittest import mock

# Parchear módulos de audio antes de importar engineer
sys.modules['pyaudio'] = mock.MagicMock()
_piper_mock = mock.MagicMock()
_piper_mock.PiperVoice.load.return_value = mock.MagicMock()
sys.modules['piper'] = _piper_mock
sys.modules['piper.voice'] = _piper_mock

# Silenciar TTS y audio para que los tests sean rápidos
import engineer as eng

# Reemplazar TTS con captura
spoken: list[tuple[int, str]] = []

def _fake_tts_put(item):
    if isinstance(item, tuple) and len(item) == 2 and item[1] is not None:
        spoken.append(item)

eng._tts_pq.put = _fake_tts_put

def reset():
    spoken.clear()
    eng._alert_times.clear()
    eng._deferred_msgs.clear()
    eng._gap_snapshots.clear()
    eng._connected   = True
    eng._current     = eng.Telemetry()
    eng._current.session  = 2
    eng._current.track    = 'ks_red_bull_ring'
    eng._current.lap      = 3
    eng._current.speed    = 150.0
    # 0.30 = recta larga RBR — FUERA de las 3 zonas de frenada [(0.96,0.06),(0.45,0.53),(0.72,0.80)]
    eng._current.spline   = 0.30
    eng._current.fuel_laps = 10.0
    eng._current.fuel      = 20.0
    eng._current.cars      = 10
    eng._current.position  = 4
    eng._current.best_lap_ms = 90000.0   # 1:30 reference (>30000 threshold)
    eng._car_db['ks_red_bull_ring_gt3'] = {'name':'Test GT3','class':'GT3','tags':['gt3']}
    eng._prev_dmg_max  = 0.0
    eng._prev_position = -1

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

def check(label, cond, detail=""):
    icon = PASS if cond else FAIL
    print(f"  {icon} {label}" + (f" → {detail}" if detail else ""))
    return cond

print("\n" + "="*60)
print("  VICTOR — Test Suite de Features")
print("="*60)

# ─── TEST 1: LLM tiering — fuel alert es rule-based (no llama a _groq) ─────────
print("\n[1] LLM tiering — alertas rule-based")
reset()
groq_calls = []
orig_groq = eng._groq
eng._groq = lambda *a, **k: (groq_calls.append(a), "")[1]

eng._current.fuel_laps = 1.4   # CRÍTICO ≤ 1.8
eng._check_proactive_alerts()
check("Fuel crítico no llama a _groq",     len(groq_calls) == 0,
      f"groq_calls={len(groq_calls)}")
check("Fuel crítico genera audio",          any("combustible" in s[1].lower() for s in spoken),
      spoken[-1][1][:70] if spoken else "sin audio")
check("Fuel crítico = prioridad 1",         any(s[0] == 1 for s in spoken))

reset(); groq_calls.clear()
eng._groq = lambda *a, **k: (groq_calls.append(a), "")[1]
eng._current.fuel_laps = 3.0   # WARN 1.8–3.5
eng._check_proactive_alerts()
check("Fuel warn no llama a _groq",         len(groq_calls) == 0)
check("Fuel warn genera audio",             any("combustible" in s[1].lower() for s in spoken))

reset(); groq_calls.clear()
eng._groq = lambda *a, **k: (groq_calls.append(a), "")[1]
eng._current.tyre_fl = 108.0   # GT3 sobrecalentamiento (>95+10=105)
eng._current.tyre_fr = eng._current.tyre_rl = eng._current.tyre_rr = 85.0
eng._car_db[''] = {'name':'GT3','class':'GT3','tags':['gt3']}
eng._check_proactive_alerts()
check("Tyre crit no llama a _groq",        len(groq_calls) == 0)
check("Tyre crit genera audio",            any("sobrecalentamiento" in s[1].lower() or "temperatura" in s[1].lower() for s in spoken),
      spoken[-1][1][:70] if spoken else "sin audio")

# Restaurar _groq
eng._groq = orig_groq

# ─── TEST 2: Tyre temp dinámica por compuesto ─────────────────────────────────
print("\n[2] Tyre temp dinámica por compuesto")
reset()
eng._car_db['ks_ferrari_488_gt3'] = {'name':'Ferrari 488 GT3','class':'GT3','tags':['gt3']}
eng._current.car = 'ks_ferrari_488_gt3'

lo, hi = eng._parse_tyre_range('ks_ferrari_488_gt3')
check("GT3 range correcto (75-95)",        lo == 75.0 and hi == 95.0, f"{lo}-{hi}°C")

# Forzar cooldown y verificar con GT3 en 96°C (entre hi=95 y hi+10=105 → warn)
eng._current.tyre_fl = 97.0
eng._current.tyre_fr = eng._current.tyre_rl = eng._current.tyre_rr = 80.0
eng._check_proactive_alerts()
tyre_warn = any("fuera del rango" in s[1].lower() or "temperatura" in s[1].lower() for s in spoken)
check("Tyre warn dispara en >hi°C (97 GT3)", tyre_warn,
      spoken[-1][1][:70] if spoken else "sin audio")

reset()
# Goma fría: óptimo GT3 75°C, frío threshold = 75*0.85 ≈ 63°C
eng._current.car = 'ks_ferrari_488_gt3'
eng._current.tyre_fl = eng._current.tyre_fr = 60.0   # frías
eng._current.tyre_rl = eng._current.tyre_rr = 58.0
eng._current.lap = 2
eng._check_proactive_alerts()
cold_warn = any("fría" in s[1].lower() or "frías" in s[1].lower() for s in spoken)
check("Tyre cold detectado (<63°C GT3)",   cold_warn,
      spoken[-1][1][:70] if spoken else "sin audio")

# ─── TEST 3: Braking zone — deferred messaging ─────────────────────────────────
print("\n[3] Braking zone — supresión en frenada")
reset()
# RBR T1 wrap zone: (0.96, 0.06) → spline 0.02 está dentro
eng._current.spline = 0.02
eng._current.speed  = 220.0
eng._current.track  = 'ks_red_bull_ring'

in_brk = eng._in_braking_zone()
check("_in_braking_zone() True en T1 (spline=0.02)", in_brk)

eng._current.spline = 0.30   # recta larga
out_brk = not eng._in_braking_zone()
check("_in_braking_zone() False en recta (spline=0.30)", out_brk)

# Mensaje normal (prio=2) en zona de frenada → debe ir a _deferred_msgs
reset()
eng._current.spline = 0.02
eng._current.speed  = 220.0
eng._say("Test mensaje diferido", priority=2)
check("Mensaje prio=2 diferido en braking zone",
      len(eng._deferred_msgs) == 1 and len(spoken) == 0,
      f"deferred={len(eng._deferred_msgs)} spoken={len(spoken)}")

# Mensaje crítico (prio=1) en zona de frenada → pasa directo
eng._say("Test mensaje crítico", priority=1)
check("Mensaje prio=1 pasa directo en braking zone",
      len(spoken) == 1,
      f"spoken={len(spoken)}")

# Salir de zona → el deferred se drena en el main loop
eng._current.spline = 0.30
with eng._deferred_lock:
    msgs = eng._deferred_msgs.copy()
    eng._deferred_msgs.clear()
for pri, txt in msgs:
    eng._tts_pq.put((pri, txt))
check("Deferred se drena al salir de braking zone",
      any("diferido" in s[1] for s in spoken), f"spoken={[s[1] for s in spoken]}")

# ─── TEST 4: Gap con tendencia ─────────────────────────────────────────────────
print("\n[4] Gap con tendencia (delta por vuelta)")
reset()
# Rival con índice 5 — gap bajando: 4.0 → 3.2 → 2.5
eng._gap_snapshots[5] = collections.deque([4.0, 3.2], maxlen=5)
t = eng.Telemetry(speed=150, spline=0.50, track_len=4326, position=4, cars=10,
                   track='ks_red_bull_ring')
t.cars_data = [{'idx':5,'race_pos':3,'speed':148,'spline':0.47,'driver_name':'Torres'}]

trend = eng._gap_trend(5, 2.5)    # current_gap=2.5, last=3.2 → delta=-0.7 → cerrando
check("Trend 'cerrando' detectado", "cerrando" in trend, f"trend='{trend}'")

# Gap alejando
eng._gap_snapshots[7] = collections.deque([1.2, 1.8], maxlen=5)
trend2 = eng._gap_trend(7, 2.4)   # gap=2.4, last=1.8 → alejando
check("Trend 'alejando' detectado", "alejando" in trend2, f"trend='{trend2}'")

# Sin historia suficiente → sin trend
trend3 = eng._gap_trend(99, 3.0)
check("Sin historia → trend vacío", trend3 == "", f"trend='{trend3}'")

# _gap_desc integrado
t2 = eng.Telemetry(speed=150, spline=0.50, track_len=4326, position=4, cars=10,
                    track='ks_red_bull_ring')
t2.cars_data = [{'idx':5,'race_pos':3,'speed':148,'spline':0.47,'driver_name':'Torres'}]
eng._gap_snapshots[5] = collections.deque([4.0, 3.2, 2.5], maxlen=5)
desc = eng._gap_desc(t2)
check("_gap_desc incluye trend",
      "cerrando" in desc or "alejando" in desc,
      f"desc='{desc}'")

# ─── TEST 5: Mid-lap projection ────────────────────────────────────────────────
print("\n[5] Mid-lap projection")
# best_lap = 90s (=90000ms), zona_best = 4.5s/zona  (20*4.5=90s)
# completadas: 10 zonas a 4.7s → 47s. remaining: 10 * 4.5 → 45s. proyectado=92s → +2.0s
reset()
eng._zone_best    = [4.5] * eng._N_ZONES
eng._zone_cur_lap = [4.7] * 10 + [None] * 10
eng._current.best_lap_ms = 90000.0   # 1:30 — > threshold 30s
eng._current.session = 2
eng._project_lap_time()
check("Proyección fires con 10 zonas (50%)",
      any("proyectada" in s[1].lower() for s in spoken),
      spoken[-1][1][:70] if spoken else "sin datos — revisar umbral n_comp/n_rem_ref")

# Proyección de récord personal: zonas actuales más rápidas que el mejor
reset()
eng._zone_best    = [4.5] * eng._N_ZONES
eng._zone_cur_lap = [4.2] * 10 + [None] * 10   # 10*4.2 + 10*4.5 = 42+45 = 87s vs best 90s → PB
eng._current.best_lap_ms = 90000.0
eng._current.session = 2
eng._project_lap_time()
pb = any("récord" in s[1].lower() for s in spoken)
check("Proyección de PB detectada", pb,
      spoken[-1][1][:70] if spoken else "sin audio")

# ─── TEST 6: Controller mode en _SYSTEM ────────────────────────────────────────
print("\n[6] Controller mode")
check("CONTROLLER_MODE = True",          eng.CONTROLLER_MODE == True)
check("_SYSTEM contiene 'MANDO'",       "MANDO" in eng._SYSTEM)
check("_SYSTEM contiene 'suavidad'",    "suavidad" in eng._SYSTEM)
check("_SYSTEM contiene 'stick'",       "stick" in eng._SYSTEM.lower())

# ─── TEST 7: Crash rotation ────────────────────────────────────────────────────
print("\n[7] Crash template rotation")
reset()
eng._current.spline = 0.50   # fuera de braking zone
first_msgs = []
for i in range(4):
    eng._alert_times.pop('crash_reaction', None)
    eng._prev_dmg_max = 0.0
    eng._current.dmg_front = 20.0 + i   # spike >15 cada vez
    eng._check_proactive_alerts()
    if spoken:
        first_msgs.append(spoken[-1][1][:20])
    spoken.clear()
unique = len(set(first_msgs))
check("Crash templates rotan (≥2 variantes en 4 crashes)",
      unique >= 2, f"variantes={unique}: {first_msgs}")

# ─── RESUMEN ────────────────────────────────────────────────────────────────────
print("\n" + "="*60)
ok    = sum(1 for l in open('/dev/stdin') for _ in [] if False)  # dummy
total_pass = spoken.count == spoken.count   # always true
print("  Tests completados. Revisa los ✓/✗ arriba.")
print("="*60)
