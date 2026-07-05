"""Victor v7 — configuración central.

Carga las API keys desde el .env de nexus-core (claves NEXUS_*) y resuelve los
assets locales del proyecto (voz Piper, modelo Silero VAD). La calibración del
pipeline de voz (umbral VAD aprendido con tools/teach_victor.py) se persiste en
data/victor_calibration.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DIR = Path(__file__).parent.resolve()

# .env de NEXUS — fuente única de API keys (el .env del viejo jarvis-ironman ya no existe)
NEXUS_ENV_PATH = Path.home() / "Documentos/Proyectos/nexus-core/.env"

# Assets locales (autocontenidos — copiados de nexus-core el 2026-07-05)
PIPER_MODEL_PATH = _DIR / "voices/es_davefx_medium/model.onnx"
SILERO_VAD_PATH  = _DIR / "models/silero_vad.onnx"
CALIBRATION_PATH = _DIR / "data/victor_calibration.json"
DRIVER_MEMORY_PATH = _DIR / "data/driver_memory.json"


def _load_dotenv(p: Path) -> dict:
    env: dict = {}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


_env = _load_dotenv(NEXUS_ENV_PATH)


def get_key(name: str, default: str = "") -> str:
    """Busca primero en el entorno del proceso, luego en el .env de NEXUS."""
    return os.getenv(name) or _env.get(name, default)


# ── API keys / modelos (mismos proveedores que el TieredBrain de NEXUS) ──────
GROQ_API_KEY     = get_key("NEXUS_GROQ_API_KEY") or get_key("JARVIS_GROQ_API_KEY")
CEREBRAS_API_KEY = get_key("NEXUS_CEREBRAS_API_KEY")
NVIDIA_API_KEY   = get_key("NEXUS_NVIDIA_API_KEY")
GEMINI_API_KEY   = get_key("NEXUS_GEMINI_API_KEY")
DEEPGRAM_API_KEY = get_key("NEXUS_DEEPGRAM_API_KEY")

GROQ_MODEL     = get_key("NEXUS_GROQ_MODEL", "llama-3.3-70b-versatile")
CEREBRAS_MODEL = get_key("NEXUS_CEREBRAS_MODEL", "gpt-oss-120b")
NVIDIA_MODEL   = get_key("NEXUS_NVIDIA_MODEL", "meta/llama-4-maverick")
GEMINI_MODEL   = get_key("NEXUS_GEMINI_MODEL", "gemini-2.0-flash")

# ── Calibración del pipeline de voz ──────────────────────────────────────────
# Defaults conservadores; teach_victor.py los reescribe con valores aprendidos.
DEFAULT_CALIBRATION = {
    "vad_threshold": 0.60,       # prob Silero para considerar voz
    "vad_end_threshold": 0.35,   # por debajo de esto cuenta como silencio
    "vad_confirm_frames": 6,     # frames de 32 ms consecutivos para disparar (~192 ms)
    "rms_prefilter": 250.0,      # RMS mínimo para molestar al VAD (ahorra CPU)
    "calibrated_at": None,
    "false_positive_rate": None,
    "sensitivity": None,
}


def load_calibration() -> dict:
    cal = dict(DEFAULT_CALIBRATION)
    try:
        cal.update(json.loads(CALIBRATION_PATH.read_text()))
    except Exception:
        pass
    return cal


def save_calibration(cal: dict) -> None:
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps(cal, indent=2, ensure_ascii=False))
