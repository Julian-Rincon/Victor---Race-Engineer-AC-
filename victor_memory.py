"""Victor v7 — memoria del piloto entre sesiones.

Paridad con la "driver memory" de RACEngineer.ai: Victor recuerda, por combinación
(pista, coche), la mejor vuelta histórica, las zonas donde el piloto pierde tiempo
de forma recurrente, y la nota del último debrief — y lo inyecta en el briefing de
la siguiente sesión en esa misma combinación.
"""
from __future__ import annotations

import json
from pathlib import Path

import victor_config as cfg


def _key(track: str, car: str) -> str:
    return f"{track}::{car}"


def load() -> dict:
    try:
        return json.loads(cfg.DRIVER_MEMORY_PATH.read_text())
    except Exception:
        return {}


def save(mem: dict) -> None:
    cfg.DRIVER_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg.DRIVER_MEMORY_PATH.write_text(json.dumps(mem, indent=2, ensure_ascii=False))


def record_debrief(track: str, car: str, *, best_lap_ms: float,
                    worst_zone_label: str = "", debrief_note: str = "") -> None:
    """Actualiza la memoria tras un debrief. Solo mejora el récord, nunca lo empeora."""
    mem = load()
    key = _key(track, car)
    entry = mem.get(key, {})
    prev_best = entry.get("best_lap_ms", 0.0)
    if best_lap_ms > 30000 and (prev_best <= 0 or best_lap_ms < prev_best):
        entry["best_lap_ms"] = best_lap_ms
    if worst_zone_label:
        recurring = entry.get("recurring_weak_zones", [])
        if worst_zone_label not in recurring:
            recurring.append(worst_zone_label)
        entry["recurring_weak_zones"] = recurring[-5:]   # las 5 más recientes
    if debrief_note:
        entry["last_debrief_note"] = debrief_note
    mem[key] = entry
    save(mem)


def record_session_patterns(track: str, car: str, *, laps: int,
                             off_track_count: int = 0,
                             brake_lockup_count: int = 0,
                             crash_count: int = 0) -> None:
    """Guarda patrones de manejo de ESTA sesión (salidas de pista, bloqueos de
    freno, choques) — a diferencia de record_debrief, se llama al terminar
    CUALQUIER sesión con vueltas reales (práctica, hotlap, carrera), no solo
    al final de una carrera. Guarda un rolling de las últimas 5 sesiones por
    combinación pista+coche; context_block() promedia sobre esas para avisar
    de patrones recurrentes sin reaccionar a una sola sesión rara."""
    if laps <= 0:
        return
    mem = load()
    key = _key(track, car)
    entry = mem.get(key, {})
    sessions = entry.get("recent_sessions", [])
    sessions.append({
        "laps": laps, "off_track": off_track_count,
        "lockups": brake_lockup_count, "crashes": crash_count,
    })
    entry["recent_sessions"] = sessions[-5:]
    mem[key] = entry
    save(mem)


def context_block(track: str, car: str) -> str:
    """Bloque de texto para inyectar en el prompt de briefing de sesión. Vacío si
    no hay historial para esta combinación pista+coche."""
    mem = load()
    entry = mem.get(_key(track, car))
    if not entry:
        return ""
    lines = ["MEMORIA DEL PILOTO EN ESTA COMBINACIÓN PISTA-COCHE:"]
    if entry.get("best_lap_ms", 0) > 0:
        lines.append(f"  Mejor vuelta histórica: {entry['best_lap_ms']/1000:.3f}s")
    zones = entry.get("recurring_weak_zones", [])
    if zones:
        lines.append(f"  Zonas donde suele perder tiempo: {', '.join(zones)}")
    if entry.get("last_debrief_note"):
        lines.append(f"  Nota del último debrief: {entry['last_debrief_note']}")

    sessions = entry.get("recent_sessions", [])
    if sessions:
        n = len(sessions)
        avg_off  = sum(s.get("off_track", 0) for s in sessions) / n
        avg_lock = sum(s.get("lockups", 0)   for s in sessions) / n
        avg_crash = sum(s.get("crashes", 0)  for s in sessions) / n
        # Umbral 0.5: solo menciona un patrón si pasa "casi cada sesión en
        # promedio" — un solo incidente en 5 sesiones no cuenta como patrón,
        # evita ruido para un piloto que en general maneja limpio.
        parts = []
        if avg_off >= 0.5:
            parts.append(f"~{avg_off:.1f} salidas de pista por sesión")
        if avg_lock >= 0.5:
            parts.append(f"~{avg_lock:.1f} bloqueos de freno por sesión")
        if avg_crash >= 0.5:
            parts.append(f"~{avg_crash:.1f} choques por sesión")
        if parts:
            lines.append(f"  Patrón reciente ({n} sesiones): {', '.join(parts)}.")
    return "\n".join(lines)
