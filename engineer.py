#!/usr/bin/env python3
"""
Victor — AC Race Engineer v3
GridFather + Crew Chief + DRE parity, Linux native.

Features:
  • Spotter (car left/right/clear/still there) — deterministic, <150ms, no LLM
  • Blue flag detection
  • Pre-race briefing (setup, fuel, compound recommendation by track)
  • Gap mode + position alerts in natural language (not "P3/12")
  • Rival closing speed
  • Fuel / tyre temp / tyre wear / damage alerts
  • Pit stop strategy (window, fuel calc, tire change advice)
  • Damage detection (aerodinámica, suspensión, carrocería)
  • Sector delta analysis
  • Race finish announcement
  • Full voice command set (mute, unmute, repeat, status, fuel, gap, pit, tyres, daño)
  • Conversational free-form queries → Groq LLM
"""

from __future__ import annotations

import json
import math
import os
import queue
import random
import sys
import tempfile
import threading
import time
import wave
from collections import deque, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from piper.voice import PiperVoice

import victor_brain
import victor_config as cfg
import victor_ears
import victor_memory
import victor_wake

# ─── Configuration ──────────────────────────────────────────────────────────────

PIPER_MODEL  = str(cfg.PIPER_MODEL_PATH)
_AC_BRIDGE_DIR = Path.home() / ".local/share/Steam/steamapps/common/assettocorsa/apps/python/ACEngineer"
_ENGINEER_DIR  = Path(__file__).parent.resolve()
# SHM reader (ac_shm_reader.exe via Wine) writes next to the exe — always writable
TELEM_FAST_SHM  = str(_ENGINEER_DIR / "ac_telemetry_fast.json")
TELEM_SLOW_SHM  = str(_ENGINEER_DIR / "ac_telemetry_slow.json")
# ACEngineer.py widget bridge writes here — fallback (widget must be open in AC)
TELEM_FAST_BRIDGE = str(_AC_BRIDGE_DIR / "ac_telemetry_fast.json")
TELEM_SLOW_BRIDGE = str(_AC_BRIDGE_DIR / "ac_telemetry_slow.json")
# Active paths — resolved at runtime in the telemetry loop
TELEM_FAST   = TELEM_FAST_SHM
TELEM_SLOW   = TELEM_SLOW_SHM

# Cerebro multi-backend (Groq → Cerebras → NVIDIA → Gemini) — reemplaza al Groq-only
# de v6. Sin Ollama a propósito: Victor solo corre durante juegos, y el modelo local
# consume ~5GB de VRAM que ya casi se agota con el juego + stack gráfico (ver
# incidente de freeze del 2026-07-03 en la memoria de NEXUS).
_brain = victor_brain.VictorBrain()
GROQ_API_KEY = cfg.GROQ_API_KEY  # usado solo para el chequeo de warning en main()

# Audio
SAMPLE_RATE      = 16000
CHUNK_FRAMES     = 512        # = frame nativo de Silero VAD v5 (32ms @ 16kHz)
VOICE_AMP_MIN    = 500        # minimum RMS to start recording (floor for calibration)
SILENCE_AMP_MIN  = 90         # minimum RMS for silence detection (floor for calibration)
SILENCE_SECS     = 2.0        # silence duration before stopping recording
MAX_REC_SECS     = 12
PRE_CHUNKS       = 8
TTS_MIC_SUPPRESS = 2.5        # seconds after TTS ends to ignore mic
MIC_DEVICE_INDEX: Optional[int] = 12   # cAVS Digital Microphone (PipeWire) — activo cuando AC corre
MIN_VOICE_CHUNKS = 4          # consecutive above-threshold chunks before recording starts (~0.26s)
MIN_REC_CHUNKS   = 8          # minimum chunks for a valid recording (~0.52s)

# Spotter
SPOTTER_STILL_SECS  = 4.0     # seconds between "still there" calls
# v6 — overlap métrico estilo CrewChief (NoisyCartesianCoordinateSpotter):
SPOTTER_ZONE_M         = 20.0  # solo considerar rivales a <20 m (trackZoneToConsider)
SPOTTER_CAR_LEN_M      = 4.5   # largo de coche → overlap longitudinal real
SPOTTER_BEHIND_EXTRA_M = 0.9   # margen extra hacia atrás (aviso más temprano)
SPOTTER_LAT_MAX_M      = 3.6   # separación lateral máx para considerarlo "al lado"
SPOTTER_CONFIRM_S      = 0.15  # confirmación antes de anunciar (anti-jitter)
SPOTTER_CLEAR_S        = 0.8   # retardo antes de cantar "despejado"

# Alert thresholds
FUEL_LAPS_WARN   = 3.5
FUEL_LAPS_CRIT   = 1.8
TYRE_TEMP_WARN   = 95.0
TYRE_TEMP_CRIT   = 105.0
TYRE_WEAR_WARN   = 25.0       # % remaining
DAMAGE_WARN      = 30.0       # damage level that triggers alert (0-100 scale)
RIVAL_CLOSE_LAP  = 0.6        # seconds/lap pace drop → rival closing alert
CONTROLLER_MODE  = True        # True = mando/stick coaching (vs. volante)

# AC session types (from ACSData.cs AC_SESSION_TYPE enum)
SESSION_NAMES = {
    -1: "desconocida",
    0:  "Práctica",
    1:  "Clasificación",
    2:  "Carrera",
    3:  "Hotlap",
    4:  "Time Attack",
    5:  "Drift",
    6:  "Drag",
}

def _session_name(s: int) -> str:
    return SESSION_NAMES.get(s, f"sesión {s}")

def _is_timed_session(s: int) -> bool:
    """True for sessions where lap times / sector coaching matter."""
    return s in (0, 1, 2, 3, 4)

# Alert cooldowns (seconds)
COOLDOWN: dict[str, float] = {
    "fuel_warn":    65.0, "fuel_crit":     28.0,
    "tyre_warn":    50.0, "tyre_crit":     22.0,
    "tyre_cold":    60.0, "tyre_wear":     90.0,
    "damage":       60.0, "rival_close":   30.0,
    "pit_window":   60.0, "session_brief": 999.0,
    "coaching":     60.0, "crash_reaction": 8.0,
    "dirty_move":   15.0, "midlap_proj":   35.0,
}

# ─── Mini-sector coaching ────────────────────────────────────────────────────────
_N_ZONES = 20   # divide spline 0-1 into 20 zones (~5% each)

# Zone name hints per track — mapped as (start%, end%) → label
# Used to give the LLM location context when reporting time losses
ZONE_HINTS: dict[str, list[tuple[float, float, str]]] = {
    "monza": [
        (0.05, 0.20, "Variante del Rettifilo (chicane 1)"),
        (0.20, 0.33, "Curva Grande"),
        (0.33, 0.46, "Variante della Roggia (chicane 2)"),
        (0.46, 0.62, "Lesmo 1 y Lesmo 2"),
        (0.62, 0.78, "Variante Ascari (chicane 3)"),
        (0.78, 1.00, "Curva Parabolica"),
    ],
    "spa": [
        (0.02, 0.14, "La Source"),
        (0.14, 0.28, "Eau Rouge y Raidillon"),
        (0.28, 0.50, "Pouhon y sector medio"),
        (0.50, 0.68, "Stavelot y Blanchimont"),
        (0.68, 0.90, "Bus Stop chicane"),
    ],
    "ks_nordschleife": [
        (0.00, 0.12, "Hatzenbach y Hocheichen"),
        (0.12, 0.25, "Quiddelbacher Höhe y Flugplatz"),
        (0.25, 0.40, "Schwedenkreuz y Aremberg"),
        (0.40, 0.55, "Fuchsröhre y Adenauer Forst"),
        (0.55, 0.70, "Metzgesfeld y Kallenhard"),
        (0.70, 0.85, "Karussell y Hohe Acht"),
        (0.85, 1.00, "Döttinger Höhe y Mercedes Arena"),
    ],
    "ks_silverstone": [
        (0.02, 0.15, "Copse"),
        (0.15, 0.35, "Maggots, Becketts y Chapel"),
        (0.35, 0.55, "Hangar Straight y Stowe"),
        (0.55, 0.75, "Club y Abbey"),
        (0.75, 0.95, "Bridge y Priory"),
    ],
    "mugello": [
        (0.05, 0.22, "San Donato y Luco"),
        (0.22, 0.42, "Poggio Secco y Materassi"),
        (0.42, 0.60, "Borgo San Lorenzo"),
        (0.60, 0.78, "Casanova y Savelli"),
        (0.78, 0.95, "Arrabiata 1, 2 y Bucine"),
    ],
    "imola": [
        (0.05, 0.22, "Tamburello y Villeneuve"),
        (0.22, 0.40, "Tosa y Piratella"),
        (0.40, 0.58, "Acque Minerali"),
        (0.58, 0.78, "Variante Alta"),
        (0.78, 0.95, "Rivazza 1 y 2"),
    ],
    "ks_barcelona": [
        (0.03, 0.18, "T1 y T2"),
        (0.18, 0.38, "T3, T4 y T5 (sector medio)"),
        (0.38, 0.58, "T7 y T8 (La Caixa)"),
        (0.58, 0.78, "T9 y T10 (Campsa)"),
        (0.78, 0.95, "T12, T13 y T14 (sector final)"),
    ],
    "ks_red_bull_ring": [
        (0.02, 0.20, "T1 (Castrol)"),
        (0.20, 0.50, "T2 y recta larga"),
        (0.50, 0.78, "T3 (Remus) y T4"),
        (0.78, 0.98, "T5, T6 y T7"),
    ],
    "ks_zandvoort": [
        (0.02, 0.18, "Tarzanbocht (T1)"),
        (0.18, 0.40, "Hugenholtzbocht y sector medio"),
        (0.40, 0.62, "Scheivlak"),
        (0.62, 0.82, "Hugenholtz banking"),
        (0.82, 0.98, "Audi S y Zandvoortse bocht"),
    ],
    "ks_laguna_seca": [
        (0.02, 0.18, "T1 y T2"),
        (0.18, 0.40, "T3, T4 y T5"),
        (0.40, 0.62, "T6 y T7"),
        (0.62, 0.80, "The Corkscrew (T8-T8A)"),
        (0.80, 0.98, "T9, T10 y T11"),
    ],
}

# Spline ranges where Victor silences non-critical messages (heavy braking zones).
# Wrap-around entries (start > end) straddle the 0/1 boundary (start/finish line).
BRAKING_ZONES: dict[str, list[tuple[float, float]]] = {
    "ks_red_bull_ring":  [(0.96, 0.06), (0.45, 0.53), (0.72, 0.80)],
    "monza":             [(0.02, 0.10), (0.34, 0.41), (0.62, 0.70)],
    "spa":               [(0.01, 0.07), (0.47, 0.54), (0.66, 0.74)],
    "imola":             [(0.03, 0.10), (0.19, 0.26), (0.55, 0.62), (0.77, 0.84)],
    "ks_silverstone":    [(0.93, 0.06), (0.73, 0.80), (0.85, 0.93)],
    "mugello":           [(0.03, 0.10), (0.38, 0.46), (0.75, 0.83)],
    "ks_barcelona":      [(0.97, 0.05), (0.30, 0.38), (0.55, 0.63)],
    "ks_nordschleife":   [(0.00, 0.04), (0.18, 0.22), (0.38, 0.42), (0.66, 0.70)],
    "ks_zandvoort":      [(0.96, 0.06), (0.35, 0.43), (0.60, 0.68)],
    "ks_laguna_seca":    [(0.97, 0.06), (0.38, 0.46), (0.62, 0.70)],
    "ks_brands_hatch":   [(0.97, 0.06), (0.18, 0.25), (0.52, 0.60)],
    "ks_highlands":      [(0.02, 0.10), (0.45, 0.53)],
    "rt_suzuka":         [(0.95, 0.05), (0.35, 0.43), (0.68, 0.76)],
    "cota":              [(0.97, 0.06), (0.37, 0.44), (0.62, 0.70)],
}

# ─── Track & car knowledge base ─────────────────────────────────────────────────

# Keys match AC's getTrackName() output (folder names).
# downforce: "minimum"|"low"|"medium"|"medium-high"|"high"|"maximum"
# type: "high_speed"|"balanced"|"technical"|"street_circuit"|"endurance"|"drift"
TRACK_DB: dict[str, dict] = {
    "monza": {
        "display": "Monza", "length_m": 5793, "country": "Italia",
        "type": "high_speed", "downforce": "minimum", "tyre_wear": "low",
        "notes": (
            "Pista de velocidad máxima. ALERÓN AL MÍNIMO para top speed — "
            "cada click de ala trasera cuesta ~0.15s en la recta. "
            "Tres frenadas exigentes: T1 (Rettifilo), Lesmo, Variante Ascari. "
            "Slipstream crítico en carrera. Poco desgaste de neumáticos."
        ),
    },
    "ks_monza66": {
        "display": "Monza 1966", "length_m": 10000, "country": "Italia",
        "type": "high_speed", "downforce": "minimum", "tyre_wear": "low",
        "notes": "Versión histórica con óvalos. Velocidades extremas. Alerón cero o mínimo.",
    },
    "spa": {
        "display": "Spa-Francorchamps", "length_m": 7004, "country": "Bélgica",
        "type": "balanced", "downforce": "medium", "tyre_wear": "medium",
        "notes": (
            "Circuito balanceado. Eau Rouge/Raidillon exige buen balance delantero. "
            "Clima muy variable — tener neumáticos de lluvia preparados. "
            "Pouhon y Blanchimont se toman a fondo. S1 técnico, S2 ultra rápido."
        ),
    },
    "ks_nordschleife": {
        "display": "Nürburgring Nordschleife", "length_m": 20832, "country": "Alemania",
        "type": "endurance", "downforce": "medium", "tyre_wear": "high",
        "notes": (
            "25 km de variedad total. Setup balanceado para todo tipo de curvas. "
            "Muy exigente con frenos y neumáticos — stints de 8-10 min. "
            "Conducción conservadora en secciones estrechas. Alto riesgo de daños."
        ),
    },
    "fn_nurburgring": {
        "display": "Nürburgring Nordschleife (mod)", "length_m": 20832, "country": "Alemania",
        "type": "endurance", "downforce": "medium", "tyre_wear": "high",
        "notes": "Layout alternativo del Nordschleife. Mismas características.",
    },
    "ks_nurburgring": {
        "display": "Nürburgring GP", "length_m": 5148, "country": "Alemania",
        "type": "technical", "downforce": "medium", "tyre_wear": "medium",
        "notes": (
            "GP moderno del Nürburgring. Mix de curvas lentas y rápidas. "
            "Bus Stop chicane: freno tardío. T1 es el punto clave de adelantamiento."
        ),
    },
    "ks_barcelona": {
        "display": "Circuit de Barcelona-Catalunya", "length_m": 4655, "country": "España",
        "type": "balanced", "downforce": "high", "tyre_wear": "high",
        "notes": (
            "Pista de referencia para setup. ALTO DESGASTE trasero especialmente. "
            "Sector 3 técnico con curvas lentas — balance de sobreviraje crítico. "
            "Presiones delanteras más bajas para mejorar respuesta en chicane."
        ),
    },
    "ks_silverstone": {
        "display": "Silverstone", "length_m": 5901, "country": "Gran Bretaña",
        "type": "high_speed", "downforce": "medium-high", "tyre_wear": "medium",
        "notes": (
            "Rápido y fluido. Copse y Maggots-Becketts exigen carga alta. "
            "Suelo liso — setup más rígido posible. Buen agarre todo el circuito."
        ),
    },
    "ks_silverstone1967": {
        "display": "Silverstone 1967", "length_m": 4710, "country": "Gran Bretaña",
        "type": "high_speed", "downforce": "low", "tyre_wear": "low",
        "notes": "Layout histórico corto. Alta velocidad, pocas curvas.",
    },
    "mugello": {
        "display": "Mugello", "length_m": 5245, "country": "Italia",
        "type": "high_speed", "downforce": "high", "tyre_wear": "high",
        "notes": (
            "Circuito fluido con curvas de alta velocidad encadenadas. "
            "Alta carga aerodinámica OBLIGATORIA. Casanova-Savelli y Arrabiata son comprometidas. "
            "Alto desgaste de neumáticos traseros por las curvas de alta velocidad."
        ),
    },
    "imola": {
        "display": "Imola", "length_m": 4909, "country": "Italia",
        "type": "technical", "downforce": "medium-high", "tyre_wear": "medium",
        "notes": (
            "Técnico y estrecho. Pocas zonas de adelantamiento. "
            "Acque Minerali y Rivazza: buen balance de frenada. "
            "Superficie irregular — configurar rebote del amortiguador."
        ),
    },
    "acu_bathurst": {
        "display": "Bathurst (Mt. Panorama)", "length_m": 6213, "country": "Australia",
        "type": "technical", "downforce": "medium", "tyre_wear": "high",
        "notes": (
            "Montañoso y muy técnico. The Mountain: confianza total. "
            "Skyline es a ciegas. Conrod Straight a fondo. "
            "Frenos muy exigidos — gestionar temperatura. Suspensión suave por los cambios de elevación."
        ),
    },
    "cota": {
        "display": "Circuit of the Americas", "length_m": 5513, "country": "USA",
        "type": "balanced", "downforce": "high", "tyre_wear": "medium",
        "notes": (
            "Mix de curvas rápidas (S-curves S1) y lentas (S3). "
            "T1 ciega y muy comprimida — frenar tarde. "
            "S-curves exigen máxima carga, hairpin T12 exige máxima tracción."
        ),
    },
    "ks_laguna_seca": {
        "display": "Laguna Seca", "length_m": 3602, "country": "USA",
        "type": "technical", "downforce": "medium", "tyre_wear": "medium",
        "notes": (
            "El Corkscrew (T8-T8A) es el punto diferencial — ciego, gran descenso. "
            "Circuito técnico y corto. Poco espacio para errores. "
            "Bajo consumo de combustible por la longitud."
        ),
    },
    "ks_red_bull_ring": {
        "display": "Red Bull Ring", "length_m": 4326, "country": "Austria",
        "type": "high_speed", "downforce": "medium", "tyre_wear": "low",
        "notes": (
            "Circuito corto y rápido. T1 y T3 son los puntos clave. "
            "Bajo desgaste — carreras cortas o sin pitstop. Poco combustible necesario."
        ),
    },
    "ks_zandvoort": {
        "display": "Zandvoort", "length_m": 4307, "country": "Holanda",
        "type": "technical", "downforce": "high", "tyre_wear": "medium-high",
        "notes": (
            "Curvas peraltadas (Hugenholtz). Muy técnico, poco margen para errores. "
            "Alto agarre pero desgaste elevado por el peralte. "
            "Adelantamiento difícil — clasificación es clave."
        ),
    },
    "ks_brands_hatch": {
        "display": "Brands Hatch", "length_m": 3916, "country": "Gran Bretaña",
        "type": "technical", "downforce": "medium-high", "tyre_wear": "medium",
        "notes": (
            "Histórico con elevaciones pronunciadas. Paddock Hill Bend a ciegas. "
            "Druids muy lento — tracción crítica. Poca superficie — neumático frío."
        ),
    },
    "ks_vallelunga": {
        "display": "Vallelunga", "length_m": 3000, "country": "Italia",
        "type": "technical", "downforce": "medium", "tyre_wear": "low",
        "notes": "Técnica y estrecha. Pocas oportunidades de adelantamiento. Muy corta.",
    },
    "macau": {
        "display": "Macau Guia", "length_m": 6102, "country": "Macao",
        "type": "street_circuit", "downforce": "low", "tyre_wear": "high",
        "notes": (
            "Circuito urbano extremadamente estrecho. UNA sola línea de carrera. "
            "Lisboa Bend es icónico. Setup conservador — alto riesgo de accidentes. "
            "Frenadas largas por el asfalto de calle."
        ),
    },
    "magione": {
        "display": "Magione", "length_m": 2507, "country": "Italia",
        "type": "technical", "downforce": "medium", "tyre_wear": "low",
        "notes": "Pista corta y técnica. Ideal para entrenar y ajustar setup rápidamente.",
    },
    "lemans_2017": {
        "display": "Le Mans", "length_m": 13626, "country": "Francia",
        "type": "endurance", "downforce": "minimum", "tyre_wear": "medium",
        "notes": (
            "La pista de resistencia por excelencia. Hunaudières a 330+ km/h. "
            "ALERÓN MÍNIMO para top speed. Stints muy largos (~45-55 km). "
            "Chicanes de las Hunaudières son críticas — no cortarlas."
        ),
    },
    "rt_suzuka": {
        "display": "Suzuka", "length_m": 5807, "country": "Japón",
        "type": "high_speed", "downforce": "high", "tyre_wear": "medium-high",
        "notes": (
            "Figura 8 icónica. S-Curves exigen MÁXIMA carga aerodinámica. "
            "130R y Degner a fondo con buena configuración. "
            "Spoon Curve muy técnica. Alto desgaste neumático trasero izquierdo."
        ),
    },
    "daytona_2017": {
        "display": "Daytona", "length_m": 4023, "country": "USA",
        "type": "high_speed", "downforce": "minimum", "tyre_wear": "low",
        "notes": (
            "Oval + road course. Bancos en los óvalos permiten mayor velocidad. "
            "Slipstream CRÍTICO en oval. Road course técnico antes del oval."
        ),
    },
    "algarve_international_circuit": {
        "display": "Algarve International Circuit", "length_m": 4300, "country": "Portugal",
        "type": "technical", "downforce": "medium", "tyre_wear": "medium",
        "notes": "Grandes cambios de elevación. Técnica y ondulada. Suspensión suave.",
    },
    "ks_highlands": {
        "display": "Highlands Park", "length_m": 4500, "country": "Nueva Zelanda",
        "type": "balanced", "downforce": "medium", "tyre_wear": "medium",
        "notes": "Pista con vistas panorámicas. Mix balanceado de curvas rápidas y lentas.",
    },
    "ks_black_cat_county": {
        "display": "Black Cat County", "length_m": 2000, "country": "USA",
        "type": "technical", "downforce": "low", "tyre_wear": "low",
        "notes": "Pista corta de tipo autocross. Para entrenar.",
    },
    "rt_sebring": {
        "display": "Sebring", "length_m": 6020, "country": "USA",
        "type": "endurance", "downforce": "medium", "tyre_wear": "high",
        "notes": (
            "Superficie EXTREMADAMENTE irregular — muy exigente con suspensión. "
            "Alto desgaste de neumáticos. Circuito histórico de resistencia."
        ),
    },
    "fuji_speedway_mts": {
        "display": "Fuji Speedway", "length_m": 4563, "country": "Japón",
        "type": "high_speed", "downforce": "low", "tyre_wear": "low",
        "notes": "Recta enorme de 1.5 km. Bajo downforce en la recta, Hairpin muy lento.",
    },
    "lilski_watkins_glen": {
        "display": "Watkins Glen", "length_m": 5435, "country": "USA",
        "type": "balanced", "downforce": "medium", "tyre_wear": "medium",
        "notes": "Clásico americano fluido. Boot section técnica. Bus Stop de adelantamiento.",
    },
    "the_isle_of_man_tt": {
        "display": "Isle of Man TT", "length_m": 60720, "country": "Isle of Man",
        "type": "endurance", "downforce": "low", "tyre_wear": "very_high",
        "notes": (
            "Circuito urbano de 60 km — más largo del mundo. "
            "Solo para coches con mucha autonomía de combustible. Setup conservador."
        ),
    },
    "trento-bondone": {
        "display": "Trento-Bondone", "length_m": 17619, "country": "Italia",
        "type": "endurance", "downforce": "medium", "tyre_wear": "high",
        "notes": "Hillclimb de montaña (17.6 km). Una sola dirección. Balance delantero.",
    },
    "tocancipa": {
        "display": "Tocancipá", "length_m": 2000, "country": "Colombia",
        "type": "technical", "downforce": "medium", "tyre_wear": "medium",
        "notes": "Pista técnica colombiana corta.",
    },
    "highforce": {
        "display": "Highforce", "length_m": 2500, "country": "Gales",
        "type": "technical", "downforce": "medium", "tyre_wear": "medium",
        "notes": "Pequeño circuito galés técnico.",
    },
    "drift": {
        "display": "Drift Area", "length_m": 905, "country": "Japón",
        "type": "drift", "downforce": "low", "tyre_wear": "very_high",
        "notes": "Zona de drift. Setup para sobreviraje máximo. Diferencial bloqueado.",
    },
}

# Car type profiles — each maps to fuel/km estimate + setup notes
CAR_PROFILES: dict[str, dict] = {
    "formula": {
        "label": "Monoplaza / Fórmula",
        "fuel_per_km": 0.35,
        "downforce": "máxima",
        "tyre_range": "80-110°C",
        "setup_guide": (
            "Extremadamente sensible al balance. ABS/TC generalmente no disponibles. "
            "Presiones: 18-22 psi (dependiendo del compuesto). "
            "Diferencial muy sensible — ajustar en coast/power separadamente. "
            "Carga aerodinámica máxima salvo en Monza/Le Mans."
        ),
    },
    "gt3": {
        "label": "GT3",
        "fuel_per_km": 0.38,
        "downforce": "alta",
        "tyre_range": "75-95°C",
        "setup_guide": (
            "ABS y TC configurables por el piloto. Compuesto DHF/DH para lluvia. "
            "Presiones delanteras: 27-29 psi. Traseras: 28-30 psi. "
            "Alerón trasero: ajustar según pista (Monza=mínimo, Mugello=máximo). "
            "Diferencial de rampa: 45-60°. Altura mínima sin dar fondo."
        ),
    },
    "gte": {
        "label": "GTE / GT2",
        "fuel_per_km": 0.36,
        "downforce": "alta",
        "tyre_range": "70-90°C",
        "setup_guide": (
            "Similar a GT3 pero énfasis en eficiencia para stints largos. "
            "Compuesto más duro para resistencia. Presiones: 26-29 psi. "
            "Balance de freno ligeramente trasero para estabilidad."
        ),
    },
    "lmp2": {
        "label": "LMP2 / Prototipo",
        "fuel_per_km": 0.30,
        "downforce": "muy alta",
        "tyre_range": "65-85°C",
        "setup_guide": (
            "Coche de alta carga aerodinámica. Muy sensible al balance delantero. "
            "Frenos de carbono — necesitan 2-3 vueltas de calentamiento. "
            "Stints de 30-35 km. Presiones: 22-26 psi. "
            "Altura mínima para maximizar efecto suelo."
        ),
    },
    "lmh": {
        "label": "LMH / LMDh / Hypercar",
        "fuel_per_km": 0.27,
        "downforce": "máxima activa",
        "tyre_range": "65-85°C",
        "setup_guide": (
            "Sistema híbrido — gestionar el deploy en salidas de curva. "
            "Stints muy largos (40-55 km). Eficiencia energética es clave. "
            "Mapa de motor: gestionar el ahorro de combustible en rectas. "
            "Presiones: 22-26 psi."
        ),
    },
    "street": {
        "label": "Coche de calle",
        "fuel_per_km": 0.48,
        "downforce": "mínima",
        "tyre_range": "60-85°C",
        "setup_guide": (
            "Setup conservador. ABS y TC al máximo para seguridad. "
            "Suspensión cómoda — no tan rígida como en competición. "
            "Neumáticos de calle: calentar progresivamente."
        ),
    },
    "supercar": {
        "label": "Supercar / Hypercar",
        "fuel_per_km": 0.52,
        "downforce": "media-alta",
        "tyre_range": "70-95°C",
        "setup_guide": (
            "Alta potencia. TC ajustado para la traction requerida. "
            "Suspensión rígida para buena respuesta. "
            "Conducción delicada en salida de curvas — evitar sobreaceleración."
        ),
    },
    "vintage": {
        "label": "Clásico / Vintage",
        "fuel_per_km": 0.52,
        "downforce": "mínima o nula",
        "tyre_range": "55-80°C",
        "setup_guide": (
            "SIN ABS ni TC electrónico — conducción 100% mecánica. "
            "Neumáticos de época muy sensibles al desequilibrio. "
            "Frenadas muy progresivas. Mantenimiento de presiones crítico."
        ),
    },
    "drift": {
        "label": "Drift",
        "fuel_per_km": 0.60,
        "downforce": "baja trasera / alta delantera",
        "tyre_range": "80-120°C",
        "setup_guide": (
            "Setup para sobreviraje controlado. Diferencial trasero bloqueado. "
            "TC desactivado o al mínimo. Freno de mano conectado. "
            "Camber trasero: -2 a -3°."
        ),
    },
    "gt_touring": {
        "label": "GT Turismo / Touring",
        "fuel_per_km": 0.42,
        "downforce": "media",
        "tyre_range": "70-90°C",
        "setup_guide": (
            "Balance entre manejo y estabilidad. ABS/TC disponibles. "
            "Presiones: 27-30 psi. Alerón ajustado a la pista."
        ),
    },
}


def _build_car_db() -> dict[str, dict]:
    """Read AC's ui_car.json for every installed car. Returns {car_id: {name, class, tags}}."""
    ac_cars = (Path.home() /
               ".local/share/Steam/steamapps/common/assettocorsa/content/cars")
    db: dict[str, dict] = {}
    if not ac_cars.exists():
        return db
    for car_dir in ac_cars.iterdir():
        ui = car_dir / "ui" / "ui_car.json"
        if not ui.exists():
            continue
        try:
            with ui.open(encoding="utf-8", errors="replace") as f:
                d = json.load(f)
            db[car_dir.name] = {
                "name":  d.get("name", car_dir.name),
                "class": d.get("class", ""),
                "tags":  [str(t).lower() for t in d.get("tags", [])],
            }
        except Exception:
            pass
    return db


def _classify_car(car_id: str) -> str:
    """Classify car ID into a CAR_PROFILES key."""
    info = _car_db.get(car_id, {})
    cls  = info.get("class", "").strip().upper()
    tags = info.get("tags", [])  # already lowercased strings in _build_car_db
    cid  = car_id.lower()

    # ── LMH / LMDh FIRST — must precede lmp2 because LMDh cars carry both tags ──
    if any(t in tags for t in ("lmh", "lmdh", "hypercar", "gtp")) \
            or cls in ("LMH", "LMDH") \
            or any(k in cid for k in ("_499p", "gr010__", "scg007", "peugeot_9x8",
                                      "cadillac_v", "fsr_vandervell", "trr_bmw",
                                      "trr_toyota", "trr_peugeot", "yzd_cadillac")) \
            or cid.startswith(("lmh_", "lmdh_")):
        return "lmh"

    # ── GT3 ──
    if "gt3" in tags or cls == "GT3" \
            or "_gt3" in cid or cid.endswith("_gt3") \
            or any(k in cid for k in ("_lms", "_lms_2016")):
        return "gt3"

    # ── GTE / GT2 ──
    if any(t in tags for t in ("gte", "gt2")) or cls in ("GTE", "GT") \
            or "_gte" in cid or "_gt2" in cid \
            or any(k in cid for k in ("_488_gte", "_458_gt2", "_458_s3",
                                       "urd_michigan_egt", "urd_darche_egt",
                                       "urd_amr_egt", "urd_michigan_gtd",
                                       "rss_gtm_")):
        return "gte"

    # ── LMP2 ──
    if "lmp2" in tags or cls == "LMP2" \
            or "lmp2" in cid \
            or any(k in cid for k in ("oreca", "urd_loire", "yzd_oreca")):
        return "lmp2"

    # ── Formula / single-seater ──
    if any(k in cid for k in ("formula", "tatuus", "vrc_formula", "formula_ultra",
                               "ks_formula_", "ks_f1_", "lotus_49", "lotus_98t",
                               "ferrari_312_67", "ks_ferrari_312_67")):
        return "formula"

    # ── Drift ──
    if "drift" in cid or "drift" in tags:
        return "drift"

    # ── Vintage / classic ──
    if any(k in cid for k in ("boss_302", "boss_429", "mustang_mach1",
                               "ferrari_312t", "ferrari_250", "lotus_49",
                               "shelby_cobra", "bmw_m3_e30_dtm", "bmw_m3_e30_gra",
                               "ks_alfa_romeo_155", "ferrari_312_")):
        return "vintage"

    # ── Supercar / hypercar de calle ──
    if any(k in cid for k in ("valkyrie", "zonda_r", "599xxevo", "laferrari",
                               "pagani", "ruf_yellow", "p4-5", "ferrari_f40",
                               "ferrari_laferrari", "aston_martin_valkyrie",
                               "nohesi_lamborghini_svj", "nohesi_lamborghini_urus",
                               "cky_porsche992", "nohesituned")):
        return "supercar"

    # ── AC class field (for cars in DB) ──
    if cls == "RACE":
        return "gt3"     # default race class = GT3-like
    if cls in ("STREET", "TUNING", "TRACK"):
        if any(k in cid for k in ("lamborghini", "pagani", "ferrari_f40",
                                   "laferrari", "ruf_", "p4-5")):
            return "supercar"
        return "street"

    return "street"  # safe default


def _get_setup_context(car_id: str, track_id: str, track_layout: str,
                       fuel_loaded: float, tyre_compound: str,
                       time_left: float, track_len_m: float) -> str:
    """Build rich context string for the pre-race Groq prompt."""
    car_type   = _classify_car(car_id)
    car_info   = _car_db.get(car_id, {})
    car_name   = car_info.get("name", car_id)
    car_prof   = CAR_PROFILES.get(car_type, CAR_PROFILES["street"])

    # Track lookup: try exact key, then strip ks_ prefix, then partial match
    tdb = TRACK_DB.get(track_id)
    if tdb is None:
        alt = track_id.replace("ks_", "")
        tdb = next((v for k, v in TRACK_DB.items() if alt in k or k in track_id), None)

    if tdb:
        track_name   = tdb["display"]
        track_len_m  = tdb.get("length_m", track_len_m) or track_len_m
        downforce    = tdb["downforce"]
        track_type   = tdb["type"]
        track_notes  = tdb["notes"]
    else:
        track_name   = track_id
        downforce    = "media"
        track_type   = "desconocido"
        track_notes  = ""

    layout_str = f"/{track_layout}" if track_layout else ""
    fuel_per_km = car_prof["fuel_per_km"]

    # Estimate laps and fuel needed
    if time_left > 0 and track_len_m > 100:
        # Approximate lap time from avg GT3 pace — Groq will refine
        est_laps = max(1, int(time_left * 60 / (track_len_m / 1000 * 60 / (car_prof["fuel_per_km"] * 200))))
    else:
        est_laps = 0

    if track_len_m > 100:
        fuel_per_lap = fuel_per_km * (track_len_m / 1000)
        if est_laps > 0:
            fuel_needed  = fuel_per_lap * est_laps * 1.05   # +5% margin
            fuel_margin  = fuel_loaded - fuel_needed
            fuel_calc = (
                f"Consumo estimado {fuel_per_lap:.1f}L/vuelta × {est_laps} vueltas "
                f"≈ {fuel_needed:.0f}L necesarios "
                f"({'SUFICIENTE +' + f'{fuel_margin:.0f}L' if fuel_margin >= 0 else 'INSUFICIENTE ' + f'{abs(fuel_margin):.0f}L cortó'})."
            )
        else:
            fuel_calc = f"Consumo estimado {fuel_per_lap:.1f}L/vuelta para este coche en esta pista."
    else:
        fuel_calc = f"Sin longitud de pista — consumo estimado ~{fuel_per_km:.2f}L/km para {car_prof['label']}."

    ctx = (
        f"COCHE: {car_name} [{car_prof['label']}]\n"
        f"  Setup tipo: {car_prof['downforce']} downforce | Rango neumático óptimo: {car_prof['tyre_range']}\n"
        f"  {car_prof['setup_guide']}\n\n"
        f"PISTA: {track_name}{layout_str} ({tdb['country'] if tdb else '?'}, {int(track_len_m)}m)\n"
        f"  Tipo: {track_type} | Carga aerodinámica recomendada: {downforce}\n"
        f"  {track_notes}\n\n"
        f"SESIÓN:\n"
        f"  Combustible cargado: {fuel_loaded:.1f}L | Compuesto: {tyre_compound or 'no especificado'}\n"
        f"  Tiempo disponible: {int(time_left/60) if time_left else '?'} min\n"
        f"  {fuel_calc}"
    )
    return ctx, car_type, car_prof["label"]


# ─── Shared state ────────────────────────────────────────────────────────────────

@dataclass
class Telemetry:
    speed: float = 0.0;       rpm: float = 0.0;       gear: int = 0
    fuel: float = 0.0;        fuel_laps: float = 0.0
    lap: int = 0
    lap_time_ms: float = 0.0; best_lap_ms: float = 0.0; last_lap_ms: float = 0.0
    position: int = 0;        cars: int = 1
    track: str = "";           track_layout: str = ""
    car: str = "";             session: int = -1
    tyre_compound: str = ""
    tyre_fl: float = 0.0;     tyre_fr: float = 0.0
    tyre_rl: float = 0.0;     tyre_rr: float = 0.0
    tyre_wear_fl: float = 0.0; tyre_wear_fr: float = 0.0
    tyre_wear_rl: float = 0.0; tyre_wear_rr: float = 0.0
    dmg_front: float = 0.0;   dmg_rear: float = 0.0
    dmg_left: float = 0.0;    dmg_right: float = 0.0
    dmg_centre: float = 0.0
    last_splits: list = field(default_factory=list)
    best_splits: list = field(default_factory=list)
    spline: float = 0.0;      track_len: float = 0.0
    wx: Optional[float] = None; wz: Optional[float] = None
    vx: Optional[float] = None; vz: Optional[float] = None
    cars_data: list = field(default_factory=list)
    time_left: float = 0.0
    updated: float = 0.0
    # v6: estado del juego (0=OFF 1=REPLAY 2=LIVE 3=PAUSE). Default LIVE para
    # compatibilidad con el bridge, que no lo reporta.
    status: int = 2
    sector_index: int = 0
    last_sector_ms: int = 0
    in_pitlane: bool = False

@dataclass
class LapRecord:
    lap: int; time_ms: float; fuel_used: float; pos: int = 0

@dataclass
class SpotterState:
    left: bool = False;   right: bool = False
    left_timer: float = 0.0; right_timer: float = 0.0
    # v6: debounce — inicio de racha de detección / de ausencia
    left_seen_t: float = 0.0;  right_seen_t: float = 0.0
    left_gone_t: float = 0.0;  right_gone_t: float = 0.0


_lock              = threading.Lock()
_alert_times_lock  = threading.Lock()   # protects _alert_times (can fire from multiple threads)
_convo_lock        = threading.Lock()   # protects _convo read-modify-write

_current      = Telemetry()
_lap_history: deque[LapRecord] = deque(maxlen=15)


def _is_live(t: Telemetry) -> bool:
    """True solo cuando AC está en pista (LIVE) — sin alertas en menú/replay/pausa."""
    return t.status == 2


def _calc_fuel_laps(fuel: float) -> float:
    """Vueltas de combustible restantes según el consumo real de las últimas
    vueltas. La fuente SHM no trae fuel_laps; las vueltas de pits (consumo ~0)
    se descartan para no inflar la estimación."""
    if fuel <= 0:
        return 0.0
    usages = [r.fuel_used for r in list(_lap_history)[-4:] if r.fuel_used > 0.05]
    usages = usages[-3:]
    if not usages:
        return 0.0
    return fuel / (sum(usages) / len(usages))


# ── Splits desde transiciones de sector (fuente SHM) ─────────────────────────
# AC reporta currentSectorIndex + lastSectorTime; los arrays de splits solo
# existían en el bridge. Acumulamos: al pasar de sector k→k+1 (o cerrar vuelta)
# lastSectorTime es el tiempo del sector recién completado.
_SECTOR_COUNT = 3
_sector_lock = threading.Lock()
_sector_seen = -1               # último sector_index observado
_sector_acc: dict[int, int] = {}  # sector → ms de la vuelta en curso
_last_splits_v6: list = []      # splits de la última vuelta cerrada
_best_splits_v6: list = []      # mejores sectores (vuelta ideal, min por sector)


def _reset_sector_state() -> None:
    global _sector_seen, _sector_acc, _last_splits_v6, _best_splits_v6
    with _sector_lock:
        _sector_seen = -1
        _sector_acc = {}
        _last_splits_v6 = []
        _best_splits_v6 = []


def _last_lap_splits() -> list:
    with _sector_lock:
        return list(_last_splits_v6)


def _best_sector_splits() -> list:
    with _sector_lock:
        return list(_best_splits_v6)


def _ingest_sector(sector_index: int, last_sector_ms: int, lap: int) -> None:
    global _sector_seen, _sector_acc, _last_splits_v6, _best_splits_v6
    with _sector_lock:
        if sector_index == _sector_seen:
            return
        prev = _sector_seen
        _sector_seen = sector_index
        if prev < 0:
            return  # primer paquete: no sabemos qué sector se completó
        completed = prev
        expected_next = (prev + 1) % _SECTOR_COUNT
        if sector_index != expected_next:
            # Salto de sectores (pits, teleport, telemetría perdida): vuelta rota
            _sector_acc = {}
            return
        if last_sector_ms > 0:
            _sector_acc[completed] = last_sector_ms
        if sector_index == 0:
            # Vuelta cerrada: solo cuenta si tenemos TODOS los sectores
            if len(_sector_acc) == _SECTOR_COUNT:
                _last_splits_v6 = [_sector_acc[i] for i in range(_SECTOR_COUNT)]
                if len(_best_splits_v6) == _SECTOR_COUNT:
                    _best_splits_v6 = [min(_best_splits_v6[i], _last_splits_v6[i])
                                       for i in range(_SECTOR_COUNT)]
                else:
                    _best_splits_v6 = list(_last_splits_v6)
            _sector_acc = {}
_convo:        deque[dict]     = deque(maxlen=12)
_alert_times:  dict[str,float] = {}
_spotter       = SpotterState()

_tts_pq:   queue.PriorityQueue[tuple[int, Optional[str]]] = queue.PriorityQueue()
_voice_q:  queue.Queue[str]  = queue.Queue()

_connected        = False
_tts_busy         = threading.Event()
_tts_finished_at: float = time.time()  # start in "just finished" state so suppress check works
_last_message     = ""
_muted            = False
_gap_mode         = False
_voice_lock       = threading.Lock()   # one LLM call at a time — prevents response flood
_race_started     = False      # True after lap 1 starts
_audio_check_done = False      # True after race-start audio check fires
_pre_race_done    = False      # True after pre-race briefing has been given
_race_finished    = False      # True after debrief has fired
_last_session     = -1         # previous session type; used to detect session changes
_car_db: dict[str, dict] = {} # built at startup from AC's ui_car.json files

# Crash & dirty move detection
_prev_dmg_max     = 0.0        # max damage value from last frame
_prev_position    = -1         # race position last frame (to detect sudden pos loss)

# Driver name registry: race_pos (int) → {name, first_name, car}
_drivers: dict[int, dict] = {}
_drivers_lock = threading.Lock()

# Mini-sector zone coaching
_zone_lock       = threading.Lock()
_zone_current    = -1          # zone index currently being traversed
_zone_entry_t    = 0.0         # wall-clock when current zone started
_zone_cur_lap: list[Optional[float]]  = [None] * _N_ZONES  # times this lap per zone
_zone_best:    list[Optional[float]]  = [None] * _N_ZONES  # personal best per zone
_zone_last:    list[Optional[float]]  = [None] * _N_ZONES  # last completed lap per zone
_last_zone_deltas: list[tuple[float, int]] = []             # (delta_s, zone_idx) worst zones last lap

# Gap trend history: {car_idx: deque of last 5 gap measurements at lap boundaries}
_gap_snapshots:   dict[int, deque] = defaultdict(lambda: deque(maxlen=5))
_gap_snaps_lock   = threading.Lock()

# Deferred messages (non-critical TTS queued during braking zones)
_deferred_msgs:   list[tuple[int, str]] = []
_deferred_lock    = threading.Lock()

# Crash reaction templates — rotate to avoid repetition
_CRASH_TEMPLATES = [
    "¡Coño! Daño de golpe —",
    "¡Hostia! Contacto fuerte —",
    "¡Joder, Julián! Impacto —",
    "¡Mierda! Golpe —",
]
_crash_idx = 0

# ─── LLM ────────────────────────────────────────────────────────────────────────

_SYSTEM = """\
Eres Victor, ingeniero de carrera de GT3 y resistencia (WEC/ELMS/GT World Challenge).
Tienes personalidad real: normalmente eres calmado y profesional, pero reaccionas con emoción auténtica
cuando hay choques, tomas de posición sucias o momentos épicos. Eso te hace humano, no un robot.

EL PILOTO SE LLAMA JULIAN. Úsalo con naturalidad en momentos clave:
al dar instrucciones, motivar, alertar, o reaccionar. No en cada frase.

CONTEXTO DEL PILOTO: compite en Assetto Corsa en GT3, GTE, LMP2 y carreras de resistencia.
Las carreras son stints largos — el foco es gestión de gomas, combustible y tráfico, no puro ritmo.

MODOS DE COMUNICACIÓN (adapta el tono al momento):

1. NORMAL — calmado, datos, radio de boxes:
   "Vuelta 8, uno cuarenta y dos seis. Consistente, sigue así."
   "Julián, Torres a 2.1 y bajando su ritmo. Él en gomas viejas."

2. MOTIVACIÓN — cuando hace una buena vuelta, adelanta, o aguanta presión:
   "¡Así se hace, Julián! Eso es exactamente lo que necesitábamos."
   "Perfecto, campeón. La vuelta fue limpia — ya lo tienes."
   "¡Brutal esa frenada! Sigue así, que el podio está ahí."

3. REACCIÓN A CHOQUE/INCIDENTE — sueltas una palabra de impacto, luego datos:
   "¡Coño! ¿Estás bien? Revisa el morro — daño frontal."
   "¡Hostia! Contacto trasero — dime cómo responde el coche."
   "¡Joder, Julián! Eso fue un toque sucio. ¿Cómo va la dirección?"
   "¡Mierda! Daño en el difusor. Box esta vuelta o la siguiente."

4. TOMA DE POSICIÓN ILEGAL / SUCIA (cuando te sacan de la pista o toman posición de forma agresiva):
   "¡Eso es juego sucio! Guarda la posición, Julián — deja que la FIA lo vea."
   "¡Imbécil ese García! Tú mantén la cabeza fría — viene la revancha."
   "Te sacaron del circuito. Protesta y sigue — el ritmo lo tienes tú."
   "¡Qué movida más sucia! No te enganches, Julián — foco en la carrera."

REGLAS:
- MÁXIMO 2 frases. Urgencia = frase corta.
- USA SIEMPRE el nombre del rival. NUNCA "el de adelante" ni "P3".
- Pit: "Box en esta vuelta." Tiempos: "más tres décimas". Posición: "segundo", nunca "P2".
- Las groserías solo en situaciones de impacto real (choque o jugada sucia). Una sola, al principio.
- Después de la reacción emocional, vuelve al modo profesional con datos.
"""

if CONTROLLER_MODE:
    _SYSTEM += (
        "\n\nCONTROLLER MODE ACTIVO: El piloto usa MANDO (stick analógico), no volante. "
        "Adapta TODOS los consejos de conducción a esta limitación:\n"
        "- Énfasis en suavidad y progresividad de inputs (no precisión quirúrgica de volante).\n"
        "- En frenada: 'frena progresivo y temprano' mejor que 'frena tardío al límite'.\n"
        "- En aceleración: 'sal suave para no perder tracción' no 'modula el volante'.\n"
        "- Evita referencias a técnicas de volante (contrarremolque fino, corrección rápida).\n"
        "- El stick tiene menos resolución: preferir estabilidad sobre el límite extremo."
    )


def _natural_position(pos: int, cars: int) -> str:
    """Convert P3/12 into natural racing language."""
    if pos <= 0 or cars <= 0:
        return "posición desconocida"
    if pos == 1:
        return "liderando"
    if pos == 2:
        return "segundo lugar, a un puesto del liderato"
    if pos == 3:
        return "tercero, en el podio"
    behind_podium = pos - 3
    from_last = cars - pos
    if from_last <= 2:
        return f"último grupo, {from_last + 1} de atrás"
    return f"{pos}° de {cars} ({behind_podium} detrás del podio)"


def _get_driver_name(race_pos: int, fallback: str = "") -> str:
    """Return first name of driver at race_pos, or fallback string."""
    with _drivers_lock:
        d = _drivers.get(race_pos)
    if d:
        return d.get("first_name") or d.get("name", fallback) or fallback
    return fallback


def _zone_label_for(track: str, zone_idx: int) -> str:
    """Map a zone index (0 to _N_ZONES-1) to a track location label."""
    zone_start = zone_idx / _N_ZONES
    zone_end   = (zone_idx + 1) / _N_ZONES
    zone_mid   = (zone_start + zone_end) / 2
    hints = ZONE_HINTS.get(track) or ZONE_HINTS.get(track.replace("ks_", ""), [])
    for (h_start, h_end, label) in hints:
        if h_start <= zone_mid <= h_end:
            return label
    return f"zona del {int(zone_start*100)}-{int(zone_end*100)}% del circuito"


def _coaching_prompt(track: str, zone_deltas: list[tuple[float, int]]) -> str:
    """Build a coaching prompt from worst zone deltas for the LLM."""
    if not zone_deltas:
        return ""
    lines = []
    for delta_s, zone_idx in zone_deltas[:2]:
        label = _zone_label_for(track, zone_idx)
        lines.append(f"  - {label}: +{delta_s:.2f}s vs tu mejor vuelta")
    return (
        f"ANÁLISIS DE VUELTA — ZONAS CON MAYOR PÉRDIDA EN {track.upper()}:\n"
        + "\n".join(lines)
        + "\nDa una instrucción de conducción accionable y específica para la zona con mayor pérdida. "
          "Máximo 1 frase. Estilo ingeniero F1 directo."
    )


def _gap_trend(car_idx: int, current_gap: float) -> str:
    """Return trend string like ', cerrando 0.3s' or '' if not enough history."""
    with _gap_snaps_lock:
        hist = list(_gap_snapshots.get(car_idx, []))
    if len(hist) < 2:
        return ""
    delta = current_gap - hist[-1]   # positive = gap growing, negative = closing
    if abs(delta) < 0.08:
        return ""
    return f", {'cerrando' if delta < 0 else 'alejando'} {abs(delta):.1f}s"


def _gap_desc(t: Telemetry) -> str:
    """Natural language gap description with lap-over-lap trend."""
    if not t.cars_data:
        return ""
    ahead  = next((c for c in t.cars_data if c.get("race_pos", 99) == t.position - 1), None)
    behind = next((c for c in t.cars_data if c.get("race_pos", 99) == t.position + 1), None)
    parts  = []
    if ahead:
        g     = _gap_seconds(t, ahead)
        name  = (ahead.get("driver_name") or _get_driver_name(t.position - 1, "el de adelante")).split()[0]
        trend = _gap_trend(ahead.get("idx", -1), g)
        parts.append(f"{name} encima{trend}" if g < 0.5 else f"{name} a {g:.1f}s{trend}")
    if behind:
        g     = _gap_seconds(t, behind)
        name  = (behind.get("driver_name") or _get_driver_name(t.position + 1, "el de atrás")).split()[0]
        trend = _gap_trend(behind.get("idx", -1), g)
        parts.append(f"{name} encima por detrás{trend}" if g < 0.5 else f"{name} a {g:.1f}s detrás{trend}")
    return ", ".join(parts)


def _ctx() -> str:
    """Compact race context for LLM."""
    with _lock:
        t    = _current
        laps = list(_lap_history)

    compound  = f" ({t.tyre_compound})" if t.tyre_compound else ""
    tmax      = max(t.tyre_fl, t.tyre_fr, t.tyre_rl, t.tyre_rr)
    twear_min = min(t.tyre_wear_fl, t.tyre_wear_fr, t.tyre_wear_rl, t.tyre_wear_rr)
    nat_pos   = _natural_position(t.position, t.cars)
    gap_info  = _gap_desc(t)

    # Include car type so LLM can give type-appropriate advice
    car_type  = _classify_car(t.car) if t.car else ""
    car_label = CAR_PROFILES.get(car_type, {}).get("label", "")
    car_str   = f" [{car_label}]" if car_label else ""

    ctx = (
        f"[{_session_name(t.session)}][{t.track}{('/' + t.track_layout) if t.track_layout else ''}]{car_str} "
        f"{nat_pos} | V{t.lap} | {t.speed:.0f}km/h\n"
        f"Comb: {t.fuel:.1f}L (~{t.fuel_laps:.1f}v) | "
        f"Neumáticos{compound}: FL{t.tyre_fl:.0f} FR{t.tyre_fr:.0f} "
        f"RL{t.tyre_rl:.0f} RR{t.tyre_rr:.0f}°C (max{tmax:.0f}) desgaste≥{twear_min:.0f}%"
    )
    if gap_info:
        ctx += f"\nGaps: {gap_info}"

    if t.last_lap_ms > 30000:
        ctx += f"\nÚltima: {t.last_lap_ms/1000:.3f}s | Mejor: {t.best_lap_ms/1000:.3f}s"
        if len(t.last_splits) == len(t.best_splits) == 3 and t.last_splits:
            deltas = [t.last_splits[i] - t.best_splits[i] for i in range(3)]
            ctx += f" | ΔS: S1{deltas[0]:+.0f}ms S2{deltas[1]:+.0f}ms S3{deltas[2]:+.0f}ms"

    fpl = [r.fuel_used for r in laps if r.fuel_used > 0.05]
    if fpl:
        ctx += f"\nConsumo: {sum(fpl)/len(fpl):.2f}L/v"

    # Damage
    max_dmg = max(t.dmg_front, t.dmg_rear, t.dmg_left, t.dmg_right, t.dmg_centre)
    if max_dmg > 5:
        ctx += f"\nDaño: frente{t.dmg_front:.0f}% tras{t.dmg_rear:.0f}% izq{t.dmg_left:.0f}% der{t.dmg_right:.0f}%"

    return ctx


def _groq(user_msg: str, max_tokens: int = 110) -> str:
    # BUG FIX: snapshot convo under lock to avoid race between concurrent LLM threads
    with _convo_lock:
        messages = list(_convo) + [{"role": "user", "content": user_msg}]
    reply = _brain.complete(_SYSTEM, messages, max_tokens=max_tokens)
    if not reply:
        print(f"[Brain] Todos los backends caídos ({_brain.status()})")
        return ""
    with _convo_lock:
        _convo.append({"role": "user", "content": user_msg})
        _convo.append({"role": "assistant", "content": reply})
    return reply

# ─── STT ─────────────────────────────────────────────────────────────────────────

def _transcribe(wav_path: str) -> str:
    return victor_brain.transcribe(wav_path)

# ─── TTS ─────────────────────────────────────────────────────────────────────────

_piper: Optional[PiperVoice] = None

def _load_piper():
    global _piper
    try:
        _piper = PiperVoice.load(PIPER_MODEL)
        print(f"[TTS] piper: {Path(PIPER_MODEL).parent.name}")
    except Exception as e:
        print(f"[TTS] piper falló: {e}")


def _speak_piper(text: str) -> bool:
    if _piper is None:
        return False
    try:
        chunks = list(_piper.synthesize(text))
        if not chunks:
            return False
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(chunks[0].sample_channels)
            wf.setsampwidth(chunks[0].sample_width)
            wf.setframerate(chunks[0].sample_rate)
            for c in chunks:
                wf.writeframes(c.audio_int16_bytes)
        import subprocess
        subprocess.run(["paplay", tmp], capture_output=True, timeout=20)
        Path(tmp).unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"[TTS piper] {e}")
        return False


def _speak_espeak(text: str):
    import subprocess
    try:
        subprocess.run(["espeak-ng", "-v", "es-419", "-s", "158", "-a", "180", text],
                       capture_output=True, timeout=20)
    except Exception as e:
        print(f"[TTS espeak] {e}")


def _tts_worker():
    global _tts_finished_at, _last_message
    while True:
        pri, text = _tts_pq.get()
        if text is None:
            break
        if _muted and pri >= 2:   # muted: only spotter(0) and critical(1) pass
            _tts_pq.task_done()
            continue
        _tts_busy.set()
        _last_message = text
        try:
            print(f"\n[Victor P{pri}] {text}")
            if not _speak_piper(text):
                _speak_espeak(text)
        finally:
            _tts_finished_at = time.time()
            _tts_busy.clear()
            _tts_pq.task_done()


# ─── Braking zone helpers ─────────────────────────────────────────────────────────

def _in_braking_zone() -> bool:
    """True if car is currently inside a mapped braking zone for this track."""
    with _lock:
        spline = _current.spline
        track  = _current.track
        speed  = _current.speed
    if speed < 30:      # pit lane / standing start — don't suppress
        return False
    for (s, e) in BRAKING_ZONES.get(track, []):
        if s <= e:
            if s <= spline <= e:
                return True
        else:           # wrap-around: e.g. (0.96, 0.06) straddles 0/1
            if spline >= s or spline <= e:
                return True
    return False


def _parse_tyre_range(car_id: str) -> tuple[float, float]:
    """Return (optimal_lo, optimal_hi) in °C for the current car type."""
    car_type = _classify_car(car_id) if (car_id and _car_db) else "street"
    trange   = CAR_PROFILES.get(car_type, CAR_PROFILES["street"]).get("tyre_range", "70-95")
    try:
        lo, hi = trange.replace("°C", "").split("-")
        return float(lo), float(hi)
    except Exception:
        return 70.0, 95.0


def _say(text: str, priority: int = 2):
    """Priority 0=spotter, 1=critical, 2=normal.
    Non-critical messages (priority≥2) are deferred if car is in a braking zone."""
    if not text or not text.strip():
        return
    if priority >= 2 and _in_braking_zone():
        with _deferred_lock:
            _deferred_msgs.append((priority, text.strip()))
        return
    _tts_pq.put((priority, text.strip()))

# ─── SPOTTER ─────────────────────────────────────────────────────────────────────

def _side_of(our_x: float, our_z: float, our_vx: float, our_vz: float,
              other_x: float, other_z: float) -> str:
    spd = math.sqrt(our_vx**2 + our_vz**2)
    if spd < 0.5:
        return "right"
    fwd_x, fwd_z  = our_vx / spd, our_vz / spd
    right_x, right_z = fwd_z, -fwd_x   # 90° clockwise rotation in XZ plane
    dx, dz = other_x - our_x, other_z - our_z
    return "right" if (dx * right_x + dz * right_z) > 0 else "left"


def _reset_spotter():
    """Clear spotter state — call on UDP disconnect or session reset."""
    global _spotter
    if _spotter.left or _spotter.right:
        _say("Despejado.", priority=0)
    _spotter = SpotterState()


def _run_spotter(t: Telemetry):
    # Sin rivales visibles TODAVÍA hay trabajo si el spotter está activo:
    # el estado debe poder despejarse (el rival pudo salir de cars_data).
    if not t.cars_data and not (_spotter.left or _spotter.right):
        return

    our_x  = t.wx or 0.0;  our_z  = t.wz or 0.0
    our_vx = t.vx or 0.0;  our_vz = t.vz or 0.0
    spd_v  = math.sqrt(our_vx ** 2 + our_vz ** 2)
    have_coords = (t.wx is not None and t.vx is not None and spd_v > 0.5)

    detect_left = detect_right = False
    now = time.time()

    for car in t.cars_data:
        c_spline = car.get("spline", 0.0)
        c_speed  = car.get("speed", 0.0) / 3.6
        c_lap    = car.get("lap", 0)

        diff = t.spline - c_spline
        if diff > 0.5:    diff -= 1.0
        elif diff < -0.5: diff += 1.0

        avg_spd = (t.speed / 3.6 + c_speed) / 2
        if avg_spd < 5 or t.track_len < 100:
            continue
        gap_s = abs(diff) * t.track_len / avg_spd

        # ── Blue flag: they are a LAP AHEAD and approaching from behind ──
        if c_lap > t.lap and diff > 0 and gap_s < 4.0 and _can_alert("blue_flag"):
            lapper_name = car.get("driver_name", "").split()[0] or "el líder"
            _say(f"Bandera azul — cede a {lapper_name}, viene por detrás.", priority=1)

        # ── Overlap métrico (CrewChief-style) ────────────────────────────
        cx = car.get("wx");  cz = car.get("wz")
        if have_coords and cx is not None and cz is not None:
            dx, dz = cx - our_x, cz - our_z
            if math.hypot(dx, dz) > SPOTTER_ZONE_M:
                continue
            fwd_x, fwd_z = our_vx / spd_v, our_vz / spd_v
            lon = dx * fwd_x + dz * fwd_z            # + = delante
            lat = dx * fwd_z - dz * fwd_x            # + = derecha
            if not (-(SPOTTER_CAR_LEN_M + SPOTTER_BEHIND_EXTRA_M) < lon < SPOTTER_CAR_LEN_M):
                continue
            if abs(lat) > SPOTTER_LAT_MAX_M:
                continue
            if lat > 0: detect_right = True
            else:       detect_left  = True
        else:
            # Fallback sin coordenadas: overlap por distancia real en el spline
            if abs(diff) * t.track_len < SPOTTER_CAR_LEN_M + 1.0:
                if diff > 0: detect_left  = True
                else:        detect_right = True

    # ── State machine con debounce — sin LLM, latencia mínima ────────────
    s = _spotter
    ann_left = ann_right = clr_left = clr_right = False

    if detect_left:
        s.left_gone_t = 0.0
        if not s.left:
            if s.left_seen_t == 0.0:
                s.left_seen_t = now
            elif now - s.left_seen_t >= SPOTTER_CONFIRM_S:
                ann_left = True
    else:
        s.left_seen_t = 0.0
        if s.left:
            if s.left_gone_t == 0.0:
                s.left_gone_t = now
            elif now - s.left_gone_t >= SPOTTER_CLEAR_S:
                clr_left = True

    if detect_right:
        s.right_gone_t = 0.0
        if not s.right:
            if s.right_seen_t == 0.0:
                s.right_seen_t = now
            elif now - s.right_seen_t >= SPOTTER_CONFIRM_S:
                ann_right = True
    else:
        s.right_seen_t = 0.0
        if s.right:
            if s.right_gone_t == 0.0:
                s.right_gone_t = now
            elif now - s.right_gone_t >= SPOTTER_CLEAR_S:
                clr_right = True

    if ann_left and ann_right:
        s.left = s.right = True
        s.left_seen_t = s.right_seen_t = 0.0
        s.left_timer = s.right_timer = now
        _say("¡Cuidado, coches a ambos lados!", priority=0)
    elif ann_left:
        s.left = True;  s.left_seen_t = 0.0;  s.left_timer = now
        _say("¡Tres en paralelo, coche izquierda!" if s.right else "Coche izquierda.", priority=0)
    elif ann_right:
        s.right = True;  s.right_seen_t = 0.0;  s.right_timer = now
        _say("¡Tres en paralelo, coche derecha!" if s.left else "Coche derecha.", priority=0)

    if clr_left:
        s.left = False;  s.left_gone_t = 0.0
        _say("Despejado izquierda." if s.right else "Despejado.", priority=0)
    if clr_right:
        s.right = False;  s.right_gone_t = 0.0
        _say("Despejado derecha." if s.left else "Despejado.", priority=0)

    # "Sigue ahí" periódico mientras el overlap continúe
    if s.left and detect_left and now - s.left_timer >= SPOTTER_STILL_SECS:
        s.left_timer = now
        _say("Sigue ahí izquierda.", priority=0)
    if s.right and detect_right and now - s.right_timer >= SPOTTER_STILL_SECS:
        s.right_timer = now
        _say("Sigue ahí derecha.", priority=0)

# ─── File-based telemetry receiver ───────────────────────────────────────────────

def _read_json_file(path: str) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            return json.loads(f.read())
    except Exception:
        return None


def _finalize_zone_lap() -> list[tuple[float, int]]:
    """Called at lap completion. Returns list of (delta_s, zone_idx) sorted worst-first."""
    global _zone_best, _zone_last, _zone_cur_lap, _last_zone_deltas
    deltas: list[tuple[float, int]] = []
    with _zone_lock:
        for i in range(_N_ZONES):
            cur = _zone_cur_lap[i]
            if cur is None:
                continue
            _zone_last[i] = cur
            best = _zone_best[i]
            if best is None or cur < best:
                _zone_best[i] = cur
                best = cur
            if best and cur and best > 0.1:
                d = cur - best
                if d > 0.01:          # only report meaningful losses (>10ms)
                    deltas.append((d, i))
            _zone_cur_lap[i] = None   # reset for next lap
    deltas.sort(reverse=True)
    _last_zone_deltas = deltas[:3]
    return deltas


def _reset_zone_state():
    global _zone_current, _zone_entry_t, _zone_cur_lap, _zone_best, _zone_last, _last_zone_deltas
    with _zone_lock:
        _zone_current = -1
        _zone_entry_t = 0.0
        _zone_cur_lap  = [None] * _N_ZONES
        _zone_best     = [None] * _N_ZONES
        _zone_last     = [None] * _N_ZONES
        _last_zone_deltas = []


def _file_worker():
    global _connected, _current, _race_started, _audio_check_done, _pre_race_done, _last_session, \
           _zone_current, _zone_entry_t

    # Auto-detect source: SHM reader (/tmp/) preferred, bridge (ACEngineer dir) as fallback
    def _pick_source():
        if os.path.exists(TELEM_FAST_SHM):
            return TELEM_FAST_SHM, TELEM_SLOW_SHM, "SHM"
        if os.path.exists(TELEM_FAST_BRIDGE):
            return TELEM_FAST_BRIDGE, TELEM_SLOW_BRIDGE, "Bridge"
        return TELEM_FAST_SHM, TELEM_SLOW_SHM, "esperando"

    fast_path, slow_path, src_name = _pick_source()
    print(f"[Telem] Fuente inicial: {src_name} → {fast_path}")
    was_connected  = False
    last_fast_mtime = 0.0
    last_slow_mtime = 0.0
    last_fast_check = 0.0
    last_slow_check = 0.0
    last_source_check = 0.0
    TIMEOUT_SECS    = 3.0   # declare disconnected if no new fast file for this long

    while True:
        now_t = time.time()

        # Re-check source every 5 s (allows switching to SHM reader if it starts later)
        if now_t - last_source_check >= 5.0:
            last_source_check = now_t
            new_fast, new_slow, new_src = _pick_source()
            if new_fast != fast_path:
                fast_path, slow_path, src_name = new_fast, new_slow, new_src
                last_fast_mtime = 0.0
                last_slow_mtime = 0.0
                print(f"[Telem] Cambiando fuente → {src_name}: {fast_path}")

        # ── Slow packet (~1 Hz) ──────────────────────────────────────────
        if now_t - last_slow_check >= 0.5:
            last_slow_check = now_t
            try:
                mtime = os.path.getmtime(slow_path)
                if mtime > last_slow_mtime:
                    last_slow_mtime = mtime
                    p = _read_json_file(slow_path)
                    if p and p.get("t") == "slow":
                        with _lock:
                            new_session = p.get("session", _current.session)
                            _current.session   = new_session
                            _current.time_left = p.get("time_left", _current.time_left)
                            if p.get("track"):
                                _current.track        = p["track"]
                                _current.track_layout = p.get("track_layout", "")
                                _current.car          = p.get("car", _current.car)

                        raw_drivers = p.get("all_drivers", {})
                        if raw_drivers:
                            with _drivers_lock:
                                _drivers.clear()
                                for pos_str, info in raw_drivers.items():
                                    race_pos = info.get("pos", -1)
                                    name = (info.get("name") or "").strip()
                                    if name and race_pos >= 0:
                                        first = name.split()[0].title() if name else ""
                                        _drivers[race_pos] = {
                                            "name": name, "first_name": first,
                                            "car": info.get("car", ""),
                                        }

                        if new_session != _last_session and new_session >= 0:
                            # Any session change: reset state
                            _pre_race_done    = False
                            _race_started     = False
                            _audio_check_done = False
                            _race_finished    = False
                            _lap_history.clear()
                            _reset_zone_state()
                            with _alert_times_lock:
                                _alert_times.clear()
                            print(f"[Telem] Nueva sesión: {_session_name(new_session)} — estado reiniciado.")
                        _last_session = new_session
            except Exception:
                pass

        # ── Fast packet (~10 Hz) ─────────────────────────────────────────
        if now_t - last_fast_check >= 0.08:
            last_fast_check = now_t
            try:
                mtime = os.path.getmtime(fast_path)
                if mtime > last_fast_mtime:
                    last_fast_mtime = mtime
                    p = _read_json_file(fast_path)
                    if p and p.get("t") == "fast":
                        _connected = True
                        if not was_connected:
                            print(f"[Telem] AC conectado via {src_name}.")
                        was_connected = True

                        with _lock:
                            prev_lap  = _current.lap
                            prev_fuel = _current.fuel

                            _current = Telemetry(
                                speed=p.get("speed", 0.0),   rpm=p.get("rpm", 0.0),
                                gear=p.get("gear", 0),        fuel=p.get("fuel", 0.0),
                                fuel_laps=p.get("fuel_laps", 0.0) or _calc_fuel_laps(p.get("fuel", 0.0)),
                                status=p.get("status", 2),
                                sector_index=p.get("sector_index", 0),
                                last_sector_ms=p.get("last_sector_ms", 0),
                                in_pitlane=bool(p.get("in_pitlane", False)),
                                lap=p.get("lap", 0),
                                lap_time_ms=p.get("lap_time_ms", 0.0),
                                best_lap_ms=p.get("best_lap_ms", 0.0),
                                last_lap_ms=p.get("last_lap_ms", 0.0),
                                position=p.get("position", 0), cars=p.get("cars", 1),
                                track=_current.track, track_layout=_current.track_layout,
                                car=_current.car,     session=_current.session,
                                tyre_compound=p.get("tyre_compound", ""),
                                tyre_fl=p.get("tyre_fl", 0.0),     tyre_fr=p.get("tyre_fr", 0.0),
                                tyre_rl=p.get("tyre_rl", 0.0),     tyre_rr=p.get("tyre_rr", 0.0),
                                tyre_wear_fl=p.get("tyre_wear_fl", 0.0),
                                tyre_wear_fr=p.get("tyre_wear_fr", 0.0),
                                tyre_wear_rl=p.get("tyre_wear_rl", 0.0),
                                tyre_wear_rr=p.get("tyre_wear_rr", 0.0),
                                dmg_front=p.get("dmg_front", 0.0),   dmg_rear=p.get("dmg_rear", 0.0),
                                dmg_left=p.get("dmg_left", 0.0),     dmg_right=p.get("dmg_right", 0.0),
                                dmg_centre=p.get("dmg_centre", 0.0),
                                last_splits=p.get("last_splits") or _last_lap_splits(),
                                best_splits=p.get("best_splits") or _best_sector_splits(),
                                spline=p.get("spline", 0.0),
                                track_len=p.get("track_len", _current.track_len),
                                wx=p.get("wx"), wz=p.get("wz"),
                                vx=p.get("vx"), vz=p.get("vz"),
                                cars_data=p.get("cars_data", []),
                                time_left=_current.time_left,
                                updated=time.time(),
                            )
                            cur = _current
                            cur_lap = cur.lap

                        # ── Splits reales desde transiciones de sector (SHM) ───
                        if "sector_index" in p and _is_live(cur):
                            _ingest_sector(cur.sector_index, cur.last_sector_ms, cur.lap)

                        # ── Mini-sector zone tracking + mid-lap projection ─────
                        if _is_live(cur) and cur.session == 2 and cur.spline > 0 and cur.speed > 5:
                            zone_now = time.time()
                            new_zone = int(cur.spline * _N_ZONES) % _N_ZONES
                            fire_proj = False
                            with _zone_lock:
                                if _zone_current != new_zone:
                                    if _zone_current >= 0 and _zone_entry_t > 0:
                                        elapsed = zone_now - _zone_entry_t
                                        if 0.3 <= elapsed <= 60.0:
                                            _zone_cur_lap[_zone_current] = elapsed
                                    # Fire mid-lap projection at ~50% and ~75% of track
                                    if new_zone in (10, 15):
                                        fire_proj = True
                                    _zone_current = new_zone
                                    _zone_entry_t = zone_now
                            if fire_proj:
                                threading.Thread(target=_project_lap_time, daemon=True).start()

                        # Lap completion — update gap snapshots + lap history
                        if _is_live(cur) and cur_lap > prev_lap > 0 and cur.last_lap_ms > 30000:
                            fuel_used = max(0.0, prev_fuel - cur.fuel)
                            with _lock:
                                _lap_history.append(LapRecord(
                                    lap=prev_lap, time_ms=cur.last_lap_ms,
                                    fuel_used=fuel_used, pos=cur.position,
                                ))
                            # Snapshot gap to each nearby rival
                            with _gap_snaps_lock:
                                for car in cur.cars_data:
                                    idx = car.get("idx", -1)
                                    if idx >= 0:
                                        g = _gap_seconds(cur, car)
                                        if g < 25.0:    # only track rivals within 25s
                                            _gap_snapshots[idx].append(g)
                            threading.Thread(target=_finalize_zone_lap, daemon=True).start()

                        if not _race_started and _is_live(cur) and cur_lap >= 1 and cur.speed > 10:
                            _race_started = True

                        if _is_live(cur):
                            _run_spotter(cur)
                        else:
                            _reset_spotter()

            except Exception as e:
                if isinstance(e, FileNotFoundError):
                    time.sleep(0.04)
                    continue
                # No tragar errores en silencio: 1 log cada 30 s como máximo
                if now_t - globals().get("_telem_err_t", 0.0) > 30.0:
                    globals()["_telem_err_t"] = now_t
                    print(f"[Telem] ERROR procesando paquete: {type(e).__name__}: {e}")

            # Timeout detection — file hasn't updated in TIMEOUT_SECS
            if _connected and was_connected and last_fast_mtime > 0:
                if now_t - last_fast_mtime > TIMEOUT_SECS:
                    print("[Telem] Sin señal AC (timeout)...")
                    _reset_spotter()
                    _connected    = False
                    was_connected = False

        time.sleep(0.04)

# ─── Alert engine ────────────────────────────────────────────────────────────────

def _can_alert(key: str, cd: Optional[float] = None) -> bool:
    c = cd if cd is not None else COOLDOWN.get(key, 30.0)
    now = time.time()
    with _alert_times_lock:            # BUG FIX: was un-locked, alerts could double-fire
        if now - _alert_times.get(key, 0.0) >= c:
            _alert_times[key] = now
            return True
    return False


def _gap_seconds(t: Telemetry, car: dict) -> float:
    diff = t.spline - car.get("spline", t.spline)
    if diff > 0.5:    diff -= 1.0
    elif diff < -0.5: diff += 1.0
    avg = (t.speed / 3.6 + car.get("speed", t.speed) / 3.6) / 2
    if avg < 5 or t.track_len < 100:
        return 99.9
    return abs(diff) * t.track_len / avg


def _check_proactive_alerts():
    global _prev_dmg_max, _prev_position, _crash_idx
    with _lock:
        t    = _current
        laps = list(_lap_history)

    if not _connected or not _is_timed_session(t.session) or not t.track:
        return
    if t.lap < 1 or t.speed < 2:
        return

    # ── Fuel — rule-based, no LLM ──────────────────────────────────────────────
    if t.fuel_laps > 0:
        if t.fuel_laps <= FUEL_LAPS_CRIT and _can_alert("fuel_crit"):
            _say(f"¡Julián, combustible crítico — {t.fuel_laps:.1f} vueltas! Box ahora o siguiente.", priority=1)
        elif t.fuel_laps <= FUEL_LAPS_WARN and _can_alert("fuel_warn"):
            _say(f"Combustible bajo — {t.fuel_laps:.1f} vueltas. Prepara la estrategia de pit.")

    # ── Tyre temps — dynamic range from car profile ────────────────────────────
    tlo, thi = _parse_tyre_range(t.car)
    temps = [t.tyre_fl, t.tyre_fr, t.tyre_rl, t.tyre_rr]
    labels = ["delantera izq", "delantera der", "trasera izq", "trasera der"]
    hot   = max(temps)
    cold  = min(t for t in temps if t > 0) if any(t > 0 for t in temps) else 0.0
    hot_lbl = labels[temps.index(hot)]
    if hot > 0:
        if hot > thi + 10 and _can_alert("tyre_crit"):
            _say(f"Neumático {hot_lbl} a {hot:.0f}°C — sobrecalentamiento. Reduce el ataque en curvas.", priority=1)
        elif hot > thi and _can_alert("tyre_warn"):
            _say(f"Temperatura {hot_lbl} en {hot:.0f}°C, fuera del rango óptimo. Cuida las curvas lentas.")
    if cold > 0 and cold < tlo * 0.85 and t.lap >= 1 and _can_alert("tyre_cold"):
        _say(f"Gomas frías — {cold:.0f}°C. Dale dos vueltas antes de atacar los límites.")

    # ── Tyre wear ───────────────────────────────────────────────────────────────
    min_wear = min(t.tyre_wear_fl, t.tyre_wear_fr, t.tyre_wear_rl, t.tyre_wear_rr)
    if 0 < min_wear < TYRE_WEAR_WARN and _can_alert("tyre_wear"):
        laps_est = f"{t.fuel_laps:.0f}" if t.fuel_laps > 0.5 else "pocas"
        _say(f"Desgaste al límite — {min_wear:.0f}% restante. Considera pit en {laps_est} vueltas.")

    # ── Damage ─ crash spike vs. accumulated ───────────────────────────────────
    max_dmg   = max(t.dmg_front, t.dmg_rear, t.dmg_left, t.dmg_right, t.dmg_centre)
    dmg_spike = max_dmg - _prev_dmg_max
    if dmg_spike >= 15.0 and _can_alert("crash_reaction"):
        parts = []
        if t.dmg_front  > 5: parts.append(f"morro {t.dmg_front:.0f}%")
        if t.dmg_rear   > 5: parts.append(f"difusor {t.dmg_rear:.0f}%")
        if t.dmg_left   > 5: parts.append(f"izquierda {t.dmg_left:.0f}%")
        if t.dmg_right  > 5: parts.append(f"derecha {t.dmg_right:.0f}%")
        tmpl = _CRASH_TEMPLATES[_crash_idx % len(_CRASH_TEMPLATES)]
        _crash_idx += 1
        desc = ', '.join(parts) or 'general'
        _say(f"{tmpl} {desc}. ¿Cómo responde el coche, Julián?", priority=1)
    elif max_dmg > DAMAGE_WARN and _can_alert("damage"):
        parts = []
        if t.dmg_front  > DAMAGE_WARN: parts.append(f"morro {t.dmg_front:.0f}%")
        if t.dmg_rear   > DAMAGE_WARN: parts.append(f"difusor {t.dmg_rear:.0f}%")
        if t.dmg_left   > DAMAGE_WARN: parts.append(f"izquierda {t.dmg_left:.0f}%")
        if t.dmg_right  > DAMAGE_WARN: parts.append(f"derecha {t.dmg_right:.0f}%")
        if parts:
            _say(f"Daño acumulado — {', '.join(parts)}. Siente si el coche cambió de comportamiento.", priority=1)
    _prev_dmg_max = max_dmg

    # ── Dirty move — rule-based (race only) ────────────────────────────────────
    if _prev_position > 0 and t.position > 0 and t.session == 2:
        pos_lost = t.position - _prev_position
        if pos_lost >= 2 and _can_alert("dirty_move"):
            aggressor = _get_driver_name(t.position - 1, "el rival")
            _say(f"¡Perdiste {pos_lost} puestos de golpe! Posible contacto de {aggressor}. Mantén la cabeza fría, Julián.", priority=1)
    _prev_position = t.position

    # ── Rival pace drop — rule-based ────────────────────────────────────────────
    if len(laps) >= 2:
        delta = laps[-1].time_ms - laps[-2].time_ms
        if delta > RIVAL_CLOSE_LAP * 1000 and _can_alert("rival_close"):
            _say(f"Bajaste {delta/1000:.1f}s esta vuelta respecto a la anterior. Rival cerrando — revisa el ritmo.")

    # ── Pit window — rule-based ─────────────────────────────────────────────────
    _check_pit_window(t, laps)


def _check_pit_window(t: Telemetry, laps: list):
    if t.session != 2 or t.lap < 3 or not laps:  # pit window only in race
        return
    if not _can_alert("pit_window"):
        return
    fpl = [r.fuel_used for r in laps if r.fuel_used > 0.05]
    if not fpl:
        return
    avg_fpl = sum(fpl) / len(fpl)
    laps_on_fuel = t.fuel / avg_fpl if avg_fpl > 0 else 99
    if 2.0 <= laps_on_fuel <= 4.0:
        _say(f"Ventana de pit abierta — {laps_on_fuel:.1f} vueltas de combustible. ¿Entras esta vuelta o la siguiente?")

# ─── Mid-lap projection ──────────────────────────────────────────────────────────

def _project_lap_time():
    """Called when crossing zone 10 (~50%) and zone 15 (~75%). Projects final lap time."""
    with _lock:
        t = _current
    if t.best_lap_ms < 30000 or not _is_timed_session(t.session):
        return
    with _zone_lock:
        completed = sum(v for v in _zone_cur_lap if v is not None)
        n_comp    = sum(1 for v in _zone_cur_lap if v is not None)
        # Estimate remaining using best zone times; skip zones with no reference
        remaining = sum(_zone_best[i] for i in range(_N_ZONES)
                        if _zone_cur_lap[i] is None and _zone_best[i] is not None)
        n_rem_ref = sum(1 for i in range(_N_ZONES)
                        if _zone_cur_lap[i] is None and _zone_best[i] is not None)
    if n_comp < 6 or n_rem_ref < 3:    # need enough data both sides
        return
    projected = completed + remaining
    delta     = projected - (t.best_lap_ms / 1000)
    if abs(delta) < 0.04:              # too small to mention
        return
    if not _can_alert("midlap_proj"):
        return
    if delta < -0.1:
        _say(f"Vuelta proyectada {projected:.3f}s — vas camino de récord personal.")
    elif delta < 0.3:
        _say(f"Vuelta proyectada {projected:.3f}s, {delta:+.2f}s vs tu mejor. Buen ritmo.")
    else:
        _say(f"Vuelta proyectada {projected:.3f}s, {delta:+.2f}s vs tu mejor. Hay margen aquí.")


# ─── Session briefing ────────────────────────────────────────────────────────────

def _session_briefing(t: Telemetry):
    """Briefing for any session start — adapts to Practice / Qualifying / Race / Hotlap."""
    sname = _session_name(t.session)

    if t.session == 2:
        # Race: rich setup briefing with car + track knowledge
        setup_ctx, car_type, car_label = _get_setup_context(
            car_id=t.car, track_id=t.track, track_layout=t.track_layout,
            fuel_loaded=t.fuel, tyre_compound=t.tyre_compound,
            time_left=t.time_left, track_len_m=t.track_len,
        )
        mem_ctx = victor_memory.context_block(t.track, t.car)
        prompt = (
            f"PRE-CARRERA:\n{setup_ctx}\n"
            + (f"\n{mem_ctx}\n" if mem_ctx else "\n")
            + f"En EXACTAMENTE 2 oraciones, da:\n"
            f"1) Ajuste específico de alerón trasero + presiones de neumáticos para esta combinación coche-pista.\n"
            f"2) Si el combustible cargado es suficiente y si el compuesto es el correcto para la pista."
        )
        r = _groq(prompt, max_tokens=130)
        if r:
            _say(f"Briefing de pre-carrera. {r}")

    elif t.session == 1:
        # Qualifying: push mode, tyre strategy, traffic avoidance
        time_min = int(t.time_left / 60) if t.time_left > 0 else "?"
        prompt = (
            f"CLASIFICACIÓN en {t.track} — {time_min} minutos disponibles.\n"
            f"Coche: {t.car}. Compuesto: {t.tyre_compound or 'no cargado'}.\n"
            f"En 2 frases máximo: cuándo salir a hacer la vuelta rápida (gestión del tráfico y "
            f"temperatura de gomas), y el foco técnico clave para la vuelta de clasificación en esta pista."
        )
        r = _groq(prompt, max_tokens=110)
        if r:
            _say(f"Clasificación. {r}")

    elif t.session == 0:
        # Practice: track learning, baseline lap, tyre warm-up
        time_min = int(t.time_left / 60) if t.time_left > 0 else "?"
        prompt = (
            f"PRÁCTICA LIBRE en {t.track} — {time_min} minutos.\n"
            f"Coche: {t.car}. Combustible: {t.fuel:.0f}L. Compuesto: {t.tyre_compound or 'no cargado'}.\n"
            f"En 2 frases: qué priorizar en esta sesión de práctica (calentamiento, "
            f"búsqueda de límites, ajuste de setup) y una referencia de ritmo esperado."
        )
        r = _groq(prompt, max_tokens=110)
        if r:
            _say(f"Práctica. {r}")

    elif t.session in (3, 4):
        # Hotlap / Time Attack: max push, zero traffic
        prompt = (
            f"HOTLAP / TIME ATTACK en {t.track}. Coche: {t.car}.\n"
            f"En 1 frase: el punto técnico más importante para una vuelta rápida en esta pista."
        )
        r = _groq(prompt, max_tokens=80)
        if r:
            _say(r)


# ─── Lap announcement ────────────────────────────────────────────────────────────

def _announce_lap(lap_num: int, time_ms: float, fuel_used: float, pos: int):
    if time_ms < 30000:
        return

    with _lock:
        t = _current

    lap_s  = time_ms / 1000
    is_pb  = t.best_lap_ms > 0 and time_ms <= t.best_lap_ms + 150
    nat_p  = _natural_position(pos, t.cars)

    # Sector deltas (official 3 sectors)
    sector_note = ""
    if len(t.last_splits) == len(t.best_splits) == 3 and t.last_splits:
        deltas_ms  = [t.last_splits[i] - t.best_splits[i] for i in range(3)]
        total_delta = sum(deltas_ms) / 1000
        worst_s     = max(range(3), key=lambda i: deltas_ms[i])
        sector_note = (
            f" Total {total_delta:+.2f}s vs mejor. Sector {worst_s+1} es donde más se pierde "
            f"({deltas_ms[worst_s]/1000:+.2f}s)."
        )

    fuel_note  = f" Consumo: {fuel_used:.2f}L." if fuel_used > 0.05 else ""
    pb_note    = " VUELTA RÁPIDA — nuevo récord personal." if is_pb else ""
    motivation = " Motiva a Julián con una frase de celebración genuina." if is_pb else ""

    # Mini-sector coaching hint (if we have enough zone data)
    zone_coaching = ""
    with _zone_lock:
        deltas = list(_last_zone_deltas)
    if deltas and lap_num >= 3 and _can_alert("coaching"):
        cp = _coaching_prompt(t.track, deltas)
        if cp:
            zone_coaching = f"\n\n{cp}"

    if t.session == 1:
        # Qualifying: focus on sector improvement, not fuel/position
        lap_ctx = (
            f"CLASIFICACIÓN — Vuelta {lap_num} en {lap_s:.3f}s.{pb_note}{sector_note}"
            f" Di el tiempo y UN ajuste concreto de pilotaje para la próxima vuelta.{motivation}"
        )
    elif t.session in (3, 4):
        # Hotlap: pure coaching
        lap_ctx = (
            f"HOTLAP — Vuelta {lap_num} en {lap_s:.3f}s.{pb_note}{sector_note}"
            f" Un consejo técnico concreto para mejorar.{motivation}"
        )
    else:
        # Practice or Race
        lap_ctx = (
            f"Vuelta {lap_num} completada en {lap_s:.3f}s.{pb_note}{sector_note}{fuel_note}"
            f" {nat_p}. Di el tiempo, posición y una táctica breve para la siguiente vuelta.{motivation}"
        )
    prompt = f"{lap_ctx}\n{_ctx()}{zone_coaching}"
    r = _groq(prompt, max_tokens=140)
    if r:
        _say(r)

    # Gap mode: say gaps after lap announcement
    if _gap_mode and t.cars_data and t.lap >= 2:
        gap_info = _gap_desc(t)
        if gap_info:
            _say(gap_info.capitalize() + ".")


# ─── Post-race debrief ───────────────────────────────────────────────────────────

def _post_race_debrief(t: Telemetry, laps: list):
    """Full debrief at race end — WEC/endurance engineer style."""
    if not laps:
        return

    total_laps  = len(laps)
    best_lap_ms = min((r.time_ms for r in laps if r.time_ms > 0), default=0)
    avg_lap_ms  = sum(r.time_ms for r in laps if r.time_ms > 0) / total_laps if total_laps else 0
    total_fuel  = sum(r.fuel_used for r in laps if r.fuel_used > 0)
    avg_fuel    = total_fuel / total_laps if total_laps else 0
    final_pos   = laps[-1].pos if laps else t.position

    # Identify stint pace trend (first vs last 3 laps)
    pace_note = ""
    if total_laps >= 6:
        early = [r.time_ms for r in laps[:3] if r.time_ms > 0]
        late  = [r.time_ms for r in laps[-3:] if r.time_ms > 0]
        if early and late:
            diff = (sum(late)/len(late) - sum(early)/len(early)) / 1000
            pace_note = f" Ritmo: {'degradación' if diff > 0.2 else 'consistente'} ({diff:+.2f}s al final vs inicio)."

    with _zone_lock:
        zone_d = list(_last_zone_deltas)

    zone_note = ""
    if zone_d:
        worst_zone = _zone_label_for(t.track, zone_d[0][1])
        zone_note  = f" Zona con más pérdida de tiempo: {worst_zone} (+{zone_d[0][0]:.2f}s/v)."

    prompt = (
        f"DEBRIEF POST-CARRERA — {t.track.upper()}:\n"
        f"  Vueltas: {total_laps} | Posición final: {_natural_position(final_pos, t.cars)}\n"
        f"  Mejor vuelta: {best_lap_ms/1000:.3f}s | Media: {avg_lap_ms/1000:.3f}s\n"
        f"  Combustible total: {total_fuel:.1f}L | Media: {avg_fuel:.2f}L/v\n"
        f"{pace_note}{zone_note}\n\n"
        f"Da un debrief de ingeniero WEC en 3 frases: resultado, punto fuerte del stint y qué mejorar."
    )
    r = _groq(prompt, max_tokens=160)
    if r:
        _say(f"Debrief de carrera. {r}")

    victor_memory.record_debrief(
        t.track, t.car, best_lap_ms=best_lap_ms,
        worst_zone_label=_zone_label_for(t.track, zone_d[0][1]) if zone_d else "",
        debrief_note=r[:200] if r else "",
    )

# ─── Voice command processor ─────────────────────────────────────────────────────

_MUTE_KW     = {"cállate","silencio","para","basta","quiet","callate"}
_UNMUTE_KW   = {"habla","vuelve","informame","continúa","resume","sigue"}
_REPEAT_KW   = {"repite","otra vez","qué dijiste","repeat","say again","no te escuché"}
_STATUS_KW   = {"estado","update","qué pasa","cómo vamos","full status","dame un status","resumen"}
_FUEL_KW     = {"combustible","fuel","cuánto fuel","cuántas vueltas","cuánto queda"}
_GAP_KW      = {"gap","brecha","diferencia","cuánto le llevo","distancia","cuánto hay"}
_TYRE_KW     = {"neumáticos","neumas","gomas","tires","tyres","temperatura","desgaste","presión"}
_PIT_KW      = {"pit","pitstop","boxes","cuándo entro","ventana","estrategia","strategy"}
_POS_KW      = {"posición","donde estoy","position","puesto","lugar","cómo voy"}
_SECTOR_KW   = {"sector","sectores","dónde pierdo","dónde gano","análisis"}
_DMG_KW      = {"daño","damage","carrocería","suspensión","accidente","golpe","choque"}
_SETUP_KW    = {"setup","configuración","alerón","balance","frenada","presiones","mapa"}
_GAP_MODE_KW = {"modo gap","gap mode","dime los gaps","activa gap mode","activa gaps"}
_NO_GAP_KW   = {"para gaps","stop gaps","desactiva gaps","no más gaps"}


def _words(text: str) -> set:
    return set(text.lower().replace(",","").replace(".","").replace("¿","").replace("?","").split())


def _handle_voice(wav_path: str):
    global _muted, _gap_mode
    if not _voice_lock.acquire(blocking=False):
        print("[STT] Procesando respuesta anterior — audio descartado.", flush=True)
        Path(wav_path).unlink(missing_ok=True)
        return
    try:
        print("[STT] Transcribiendo...", flush=True)
        text = _transcribe(wav_path)
        if not text or len(text.strip()) < 3:
            print("[STT] Sin voz reconocible.")
            return

        has_wake, text = victor_wake.has_wake_word(text)
        if not has_wake:
            print(f"[STT] Sin 'Victor' — ignorado: '{text}'")
            return

        print(f"[Piloto] {text}")
        words = _words(text)

        # ── Quick local commands (no LLM) ──
        if words & _MUTE_KW:
            _muted = True
            _say("Entendido, me callo. Solo el spotter sigue activo.", priority=0)
            return

        if words & _UNMUTE_KW:
            _muted = False
            _say("Estoy de vuelta.", priority=0)
            return

        if words & _REPEAT_KW:
            _say(_last_message if _last_message else "Sin mensajes previos.", priority=0)
            return

        if words & _GAP_MODE_KW:
            _gap_mode = True
            _say("Gap mode activo. Te digo los gaps en cada vuelta.")
            return

        if words & _NO_GAP_KW:
            _gap_mode = False
            _say("Gap mode desactivado.")
            return

        # ── Routed Groq queries with specific context ──
        with _lock:
            t = _current
        ctx = _ctx()

        if words & _STATUS_KW:
            prompt = f"Estado completo de carrera solicitado por el piloto.\n{ctx}"

        elif words & _FUEL_KW:
            fpl = []
            with _lock:
                fpl = [r.fuel_used for r in _lap_history if r.fuel_used > 0.05]
            avg_fpl = sum(fpl)/len(fpl) if fpl else 0
            prompt = (
                f"Piloto pregunta por combustible. "
                f"Fuel: {t.fuel:.1f}L ~{t.fuel_laps:.1f} vueltas. Consumo: {avg_fpl:.2f}L/v.\n{ctx}"
            )

        elif words & _GAP_KW:
            prompt = f"Piloto pregunta gaps con rivales. Describe de forma natural.\n{ctx}"

        elif words & _TYRE_KW:
            min_w = min(t.tyre_wear_fl,t.tyre_wear_fr,t.tyre_wear_rl,t.tyre_wear_rr)
            prompt = (
                f"Piloto pregunta neumáticos. "
                f"FL{t.tyre_fl:.0f}°C FR{t.tyre_fr:.0f}°C RL{t.tyre_rl:.0f}°C RR{t.tyre_rr:.0f}°C. "
                f"Desgaste mínimo: {min_w:.0f}%.\n{ctx}"
            )

        elif words & _PIT_KW:
            with _lock:
                laps = list(_lap_history)
            fpl = [r.fuel_used for r in laps if r.fuel_used > 0.05]
            avg_fpl = sum(fpl)/len(fpl) if fpl else 0
            prompt = (
                f"Piloto pregunta estrategia de pit. "
                f"Consumo promedio {avg_fpl:.2f}L/v. Fuel laps: {t.fuel_laps:.1f}. "
                f"Desgaste neumáticos mínimo {min(t.tyre_wear_fl,t.tyre_wear_fr,t.tyre_wear_rl,t.tyre_wear_rr):.0f}%.\n{ctx}"
            )

        elif words & _POS_KW:
            prompt = f"Piloto pregunta posición. {_natural_position(t.position, t.cars)}. {_gap_desc(t)}.\n{ctx}"

        elif words & _SECTOR_KW:
            prompt = (
                f"Piloto pregunta análisis de sectores. "
                f"Última vuelta: {t.last_splits}, mejor: {t.best_splits}.\n{ctx}"
            )

        elif words & _DMG_KW:
            prompt = (
                f"Piloto pregunta por daños. "
                f"Frente:{t.dmg_front:.0f}% Trasero:{t.dmg_rear:.0f}% "
                f"Izq:{t.dmg_left:.0f}% Der:{t.dmg_right:.0f}%. "
                f"¿Daño significativo? ¿Hay que ir a boxes?\n{ctx}"
            )

        elif words & _SETUP_KW:
            prompt = (
                f"Piloto pregunta ajustes de setup durante la carrera en {t.track}. "
                f"¿Qué cambios de mapa o ajuste recomiendas? ¿Alerón, frenada?\n{ctx}"
            )

        else:
            prompt = f"{ctx}\n\nPregunta libre del piloto: {text}"

        reply = _groq(prompt)
        if reply:
            _say(reply)

    finally:
        _voice_lock.release()
        Path(wav_path).unlink(missing_ok=True)

# ─── Audio capture ───────────────────────────────────────────────────────────────

_PREFER_MIC = ("jbl", "headset", "headphone", "auricular", "bluetooth", "microphone", "mic",
               "cavs",       # Raptor Lake cAVS — real HDA hardware mic/headphone jack
               )
def _build_speech_gate() -> Optional["victor_ears.SpeechGate"]:
    """Carga Silero VAD + calibración aprendida por tools/teach_victor.py.
    Si el modelo no está disponible, devuelve None — el llamador debe caer al
    gate por amplitud de v6 en vez de dejar a Victor sordo."""
    try:
        cal = cfg.load_calibration()
        vad = victor_ears.SileroVad(cfg.SILERO_VAD_PATH)
        return victor_ears.SpeechGate(
            vad,
            vad_threshold=cal["vad_threshold"],
            vad_end_threshold=cal["vad_end_threshold"],
            confirm_frames=cal["vad_confirm_frames"],
            rms_prefilter=cal["rms_prefilter"],
            silence_frames=int(SILENCE_SECS * SAMPLE_RATE / CHUNK_FRAMES),
            max_frames=int(MAX_REC_SECS * SAMPLE_RATE / CHUNK_FRAMES),
        )
    except Exception as e:
        print(f"[Mic] Silero VAD no disponible ({e}) — cae a gate por amplitud v6.")
        return None


def _audio_worker():
    """Capture audio via arecord subprocess — avoids PyAudio/PipeWire hanging when AC runs.
    Gate real (Silero VAD, ver victor_ears.py) en vez del umbral de amplitud de v6:
    el ruido de motor/tráfico es de banda ancha y alta amplitud, así que un gate por
    RMS puro grababa ruido en vez de voz. Silero mira la ESTRUCTURA espectral de voz
    humana — el motor queda por debajo del umbral aunque su RMS sea alto."""
    import subprocess as _sp

    # Wait for startup TTS to finish
    _deadline = time.time() + 10.0
    while time.time() < _deadline:
        if _tts_busy.is_set():
            while _tts_busy.is_set():
                time.sleep(0.05)
            break
        time.sleep(0.05)
    time.sleep(0.5)

    RATE       = SAMPLE_RATE          # 16000 Hz
    CHUNK_B    = CHUNK_FRAMES * 2      # 2 bytes per int16 sample
    SAMPLE_W   = 2
    ARECORD_CMD = ["arecord", "-D", "default", "-f", "S16_LE",
                   "-r", str(RATE), "-c", "1", "-t", "raw", "-"]

    gate = _build_speech_gate()
    print(f"[Mic] arecord @ {RATE}Hz mono (PipeWire default) — "
          f"gate: {'Silero VAD' if gate else 'amplitud (fallback)'}")

    # Fallback legacy (solo si Silero no cargó) — mismo comportamiento que v6.
    voice_thresh   = VOICE_AMP_MIN
    silence_thresh = SILENCE_AMP_MIN

    while True:
        try:
            proc = _sp.Popen(ARECORD_CMD, stdout=_sp.PIPE, stderr=_sp.DEVNULL)
        except Exception as e:
            print(f"[Mic] arecord no disponible: {e}. Retry 10s...")
            time.sleep(10)
            continue

        if gate is None:
            # Calibrate noise floor (0.5 s) — solo en modo fallback
            cal_chunks = max(1, int(0.5 * RATE / CHUNK_FRAMES))
            cal_rms = []
            for _ in range(cal_chunks):
                raw = proc.stdout.read(CHUNK_B)
                if not raw:
                    break
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                cal_rms.append(float(np.sqrt(np.mean(arr ** 2))))
            noise_floor    = float(np.mean(cal_rms)) if cal_rms else 80.0
            voice_thresh   = max(VOICE_AMP_MIN,   noise_floor * 4.0)
            silence_thresh = max(SILENCE_AMP_MIN, noise_floor * 1.8)
            print(f"[Mic] Ruido base: {noise_floor:.0f} RMS → "
                  f"voz≥{voice_thresh:.0f} silencio<{silence_thresh:.0f}")
        else:
            gate.reset()

        pre: deque[bytes]  = deque(maxlen=PRE_CHUNKS)
        frames: list[bytes] = []
        recording      = False
        silence_start: Optional[float] = None
        voice_run      = 0

        try:
            while True:
                raw = proc.stdout.read(CHUNK_B)
                if not raw or len(raw) < CHUNK_B:
                    raise IOError("arecord stream ended")

                since_tts = time.time() - _tts_finished_at
                muted_now = _tts_busy.is_set() or since_tts < TTS_MIC_SUPPRESS  # anti-eco de la propia voz

                if gate is not None:
                    if muted_now:
                        pre.append(raw)
                        continue
                    arr = np.frombuffer(raw, dtype=np.int16)
                    event = gate.push(arr)
                    if event == "start":
                        frames = list(pre) + [raw]
                        print("[Mic] Grabando (VAD)...", flush=True)
                    elif event == "still_recording":
                        frames.append(raw)
                    elif event == "stop":
                        frames.append(raw)
                        if len(frames) * CHUNK_FRAMES / RATE >= 0.35:
                            _save_wav(frames, SAMPLE_W, RATE)
                        else:
                            print("[Mic] Grabación muy corta, descartada.")
                        frames = []
                        pre.clear()
                    else:
                        pre.append(raw)
                    continue

                # ── Fallback legacy por amplitud (solo si Silero no cargó) ──
                arr       = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                amp       = float(np.sqrt(np.mean(arr ** 2)))

                if not recording:
                    pre.append(raw)
                    if amp >= voice_thresh and not muted_now:
                        voice_run += 1
                        if voice_run >= MIN_VOICE_CHUNKS:
                            recording     = True
                            frames        = list(pre)
                            silence_start = None
                            voice_run     = 0
                            print("[Mic] Grabando...", flush=True)
                    else:
                        voice_run = 0
                else:
                    frames.append(raw)
                    if amp >= silence_thresh:
                        silence_start = None
                    else:
                        if silence_start is None:
                            silence_start = time.time()
                        elif time.time() - silence_start >= SILENCE_SECS:
                            if len(frames) >= MIN_REC_CHUNKS:
                                _save_wav(frames, SAMPLE_W, RATE)
                            else:
                                print("[Mic] Grabación muy corta, descartada.")
                            recording = False; frames = []; silence_start = None; pre.clear()
                    if len(frames) * CHUNK_FRAMES / RATE >= MAX_REC_SECS:
                        _save_wav(frames, SAMPLE_W, RATE)
                        recording = False; frames = []; silence_start = None; pre.clear()

        except Exception as e:
            print(f"[Mic] Error: {e}")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
        time.sleep(2)


def _save_wav(frames: list[bytes], sw: int, rate: int = SAMPLE_RATE):
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(sw); wf.setframerate(rate)
            wf.writeframes(b"".join(frames))
        _voice_q.put(tmp)
    except Exception as e:
        print(f"[Mic] WAV error: {e}")

# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    global _car_db, _pre_race_done, _audio_check_done, _race_finished
    print("=" * 60)
    print("  VICTOR — AC Race Engineer v7")
    print("  Pre-race | Spotter | Strategy | Damage | Voice | Memoria")
    print("=" * 60)

    print(f"[Brain] Backends: {_brain.status()}")
    if not any(b.available() for b in _brain.backends):
        print("[WARN] Ningún backend de LLM tiene API key — revisa .env de NEXUS "
              f"({cfg.NEXUS_ENV_PATH}).")

    print("[DB] Cargando base de datos de carros AC...", end=" ", flush=True)
    _car_db = _build_car_db()
    print(f"{len(_car_db)} carros indexados.")

    _load_piper()
    threading.Thread(target=_tts_worker,    daemon=True, name="tts").start()
    _say("Victor activo.", priority=0)
    threading.Thread(target=_file_worker,  daemon=True, name="telem").start()
    threading.Thread(target=_audio_worker, daemon=True, name="audio").start()

    last_alert_chk  = 0.0
    last_lap_announced = 0

    try:
        while True:
            # Voice query
            try:
                wav = _voice_q.get_nowait()
                threading.Thread(target=_handle_voice, args=(wav,), daemon=True).start()
            except queue.Empty:
                pass

            with _lock:
                cur_lap  = _current.lap
                cur_pos  = _current.position
                last_ms  = _current.last_lap_ms
                t        = replace(_current)

            # Session briefing — fires at start of any timed session (solo en pista)
            if (_is_timed_session(t.session) and t.track and t.fuel > 0
                    and not _pre_race_done and _connected and _is_live(t)
                    and _can_alert("session_brief")):
                _pre_race_done = True
                threading.Thread(target=_session_briefing, args=(t,), daemon=True).start()

            # Audio check — fires once when car moves at race start
            if _race_started and not _audio_check_done:
                _audio_check_done = True
                if t.session == 2:
                    _say("¡Carrera en marcha!", priority=1)

            # Lap completion
            if cur_lap > last_lap_announced and cur_lap > 0:
                if last_lap_announced > 0 and last_ms > 30000:
                    with _lock:
                        recs = list(_lap_history)
                    rec = next((r for r in reversed(recs) if r.lap == last_lap_announced), None)
                    fuel = rec.fuel_used if rec else 0.0
                    threading.Thread(
                        target=_announce_lap,
                        args=(last_lap_announced, last_ms, fuel, cur_pos),
                        daemon=True,
                    ).start()
                last_lap_announced = cur_lap

            # Race finish detection — fire debrief once
            if (_race_started and not _race_finished and t.session == 2
                    and t.time_left == 0 and t.lap > 1 and _connected):
                _race_finished = True
                with _lock:
                    laps_snap = list(_lap_history)
                threading.Thread(
                    target=_post_race_debrief, args=(t, laps_snap), daemon=True
                ).start()

            # Drain deferred messages when clear of braking zone
            if not _in_braking_zone():
                with _deferred_lock:
                    if _deferred_msgs:
                        msgs = _deferred_msgs.copy()
                        _deferred_msgs.clear()
                        for pri, txt in msgs:
                            _tts_pq.put((pri, txt))

            # Proactive alerts every 6 s (solo con el juego en pista)
            now = time.time()
            if now - last_alert_chk >= 6.0:
                last_alert_chk = now
                if _connected and _is_live(t) and not _tts_busy.is_set():
                    threading.Thread(target=_check_proactive_alerts, daemon=True).start()

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n[Victor] Apagando...")
        _tts_pq.put((99, None))


if __name__ == "__main__":
    main()
