# Victor — Patrones de manejo entre sesiones (2026-07-09)

## Contexto y motivación

Julian preguntó si Victor se adapta al estilo de manejo aprendiendo de la
telemetría, o si es solo por sesión. Respuesta antes de este cambio: parcial
— `victor_memory.py` ya persistía mejor vuelta, zonas débiles recurrentes y
nota del último debrief por combinación pista+coche, pero **solo se
grababa al final de una carrera** (`_post_race_debrief`), nunca en
práctica/hotlap, y no capturaba patrones de manejo recurrentes (salidas de
pista, bloqueos de freno, choques) que Victor ya detecta en vivo pero
descartaba al terminar la sesión.

Pedido: "guardar y organizar lo que aprende" al terminar cualquier sesión,
sin romper lo compacto del sistema actual.

## Diseño

### `victor_memory.record_session_patterns()` (nuevo)

```python
def record_session_patterns(track, car, *, laps, off_track_count=0,
                             brake_lockup_count=0, crash_count=0):
```

Guarda un rolling de las **últimas 5 sesiones** por combinación pista+coche
(mismo patrón que `recurring_weak_zones[-5:]` ya usado en `record_debrief`).
Se ignoran sesiones sin vueltas (`laps <= 0`) — evita ensuciar el historial
con sesiones que se cerraron apenas empezadas.

### `context_block()` extendido

Promedia los contadores de las sesiones guardadas y solo menciona un patrón
si el promedio es **≥0.5 por sesión** — un incidente aislado en 5 sesiones
no cuenta como "patrón", evita ruido para un piloto que maneja limpio la
mayoría del tiempo. Ejemplo de salida: `"Patrón reciente (5 sesiones): ~1.2
salidas de pista por sesión, ~0.8 bloqueos de freno por sesión."`

### Enganche en `engineer.py`

- 3 contadores de sesión nuevos (`_session_off_track_count`,
  `_session_lockup_count`, `_session_crash_count`), incrementados en los
  mismos puntos donde ya se dispara el aviso hablado (`_check_fast_alerts`
  para salida de pista/bloqueo, `_check_proactive_alerts` para choque) — no
  hay tracking nuevo, solo un contador junto al aviso que ya existía.
- En `_file_worker`, el bloque de detección de cambio de sesión (el mismo
  que resetea `_alert_times`/`_lap_history`) ahora también:
  1. Llama `record_session_patterns()` con los contadores de la sesión que
     TERMINA, usando track/car/vueltas capturados **antes** de que el
     paquete "slow" los sobreescriba con los de la sesión nueva (bug que se
     hubiera introducido si se leía `_current.track` después del
     `with _lock:` que lo actualiza).
  2. Resetea los 3 contadores a 0 para la sesión que empieza.

Corre en CUALQUIER cambio de sesión (práctica → hotlap → carrera, etc.), no
solo al terminar una carrera — a diferencia de `record_debrief`, que sigue
siendo race-only (debrief hablado con LLM, eso no cambió).

## Fuera de alcance

- No arma un perfil "general" de manejo que cruce distintas pistas — sigue
  siendo por combinación pista+coche, igual que el resto de la memoria
  existente. Cruzar pistas es una extensión futura si hace falta.
- No agrega ningún mensaje hablado nuevo — los datos se acumulan en
  silencio y solo se mencionan si aparecen en el próximo `context_block()`
  (que ya se inyecta en el briefing de pre-sesión).

## Testing

`test_v7_memory.py` — 5 tests nuevos: sesiones sin vueltas se ignoran,
rolling de 5 sesiones, el patrón aparece en el context block cuando hay
señal real, se mantiene en silencio para manejo limpio, y coexiste
correctamente con `record_debrief` en la misma entrada.

## Verificación

Suite completa (pytest 38/38 + 5 scripts legacy) en verde. Reiniciado en
vivo sin errores.
