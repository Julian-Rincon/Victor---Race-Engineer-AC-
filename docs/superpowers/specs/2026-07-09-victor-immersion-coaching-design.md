# Victor — Inmersión y coaching en tiempo real (2026-07-09)

## Contexto y motivación

Julian pidió que Victor narre eventos de inmersión "al punto más o menos
como lo publican los de GridFather": que avise cuando él se sale de pista,
que narre incidentes de otros pilotos (choques, salidas de pista), y que
haga coaching en el momento si detecta fallas de manejo (bloqueo de frenos,
neumáticos fríos). Pedido explícito: no sobreestimular con avisos — solo lo
justo y preciso.

Investigación de mercado (agente, 2026-07-09): GridFather narra banderas
amarillas por sector, tráfico lento, spotter de proximidad y coaching propio
(bloqueo de frenos, temperatura de neumáticos) sin confirmar públicamente
que use nombres de pilotos. CrewChief V4 es el más maduro narrando
incidentes de terceros (lógica de "pileup" — varios coches parados en la
misma curva). DRE es el más completo en telemetría de rivales.

## Qué ya existía (auditado antes de tocar nada)

- Daño propio + reacción a choque propio (`crash_reaction`/`damage`).
- Neumáticos fríos (`tyre_cold`) y sobrecalentados (`tyre_warn`/`tyre_crit`).
- "Perdiste N puestos de golpe — posible contacto de X" (`dirty_move`) — ya
  infiere un agresor cuando EL JUGADOR pierde posiciones de golpe.
- Spotter de proximidad (coche izq/der/despejado) — determinístico, ya vive
  en el motor de vueltas, no en `_check_proactive_alerts`.

No había: salida de pista propia, bloqueo de frenos propio, ni narración de
incidentes de rivales no relacionados directamente con el jugador.

## Requisitos

1. Aviso inmediato cuando el jugador se sale de pista significativamente.
2. Aviso inmediato cuando bloquea fuerte los frenos.
3. (Diseñado, no implementado esta sesión — ver "Pendiente") Narración de
   incidentes de OTROS pilotos, con nombre solo si es "seguro" de pronunciar,
   si no por posición/cercanía al jugador — sin listar cada incidente del
   campo completo, solo lo relevante al contexto del jugador.
4. Sin dependencias nuevas, reusar el patrón de cooldown (`_can_alert`) ya
   existente en todo el motor de alertas.

## Diseño e implementación (esta sesión)

### Campos nuevos en la memoria compartida

`ac_shm_reader.c::write_fast()` — AC ya expone estos campos en `SPhysics`,
solo faltaba emitirlos:
```c
jf("\"tyres_out\":%d,", phy->numberOfTyresOut);  // 0-4 ruedas fuera de pista
jf("\"brake\":%.3f,",   phy->brake);              // pedal de freno, 0-1
jf("\"slip_fl\":%.2f,", phy->wheelSlip[0]);        // slip por rueda
jf("\"slip_fr\":%.2f,", phy->wheelSlip[1]);
jf("\"slip_rl\":%.2f,", phy->wheelSlip[2]);
jf("\"slip_rr\":%.2f,", phy->wheelSlip[3]);
```
Recompilado: `x86_64-w64-mingw32-gcc -O2 -o ac_shm_reader.exe ac_shm_reader.c`.

`engineer.py::Telemetry` — 6 campos nuevos (`tyres_out`, `brake`, `slip_fl/fr/rl/rr`),
parseados en `_file_worker` con el mismo patrón `p.get(...)` que el resto.

### Detección — `_check_proactive_alerts()`

**Salida de pista**: `t.tyres_out >= 2` (constante `OFF_TRACK_WHEELS_WARN`),
excluye pit lane, cooldown 12s. Prioridad 1 (inmediato) — es la naturaleza
del evento pedido por Julian, no tiene "versión resumen".

**Bloqueo de frenos**: `t.brake > 0.3` y el slip máximo entre las 4 ruedas
supera `LOCKUP_SLIP_THRESHOLD = 6.0`. Cooldown 15s, prioridad 1.

**Umbrales sin calibrar con datos reales**: a diferencia del VAD (que tiene
`tools/teach_victor.py` para calibración empírica), el umbral de slip de
bloqueo es una primera pasada basada en el rango típico de `wheelSlip` de AC
(1-4 bajo agarre normal en frenada fuerte, mucho más alto con la rueda
bloqueada). **Julian debe probarlo en pista real y reportar si dispara de
más (umbral muy bajo) o de menos (muy alto)** — documentado en comentario
junto a las constantes en `engineer.py`.

## Pendiente (no implementado esta sesión — cola para la próxima)

**Narración de incidentes de rivales.** AC no expone un flag directo de "este
rival chocó" — el struct `SCrewChief`/`AVehicleInfo` (usado para `cars_data`)
solo da `worldPosition`, `speedMS`, `carLeaderboardPosition`, `isCarInPitline`
por rival, sin daño ni estado. Hay que inferirlo, igual que CrewChief:

- Mantener un historial corto de velocidad por `carId` (no existe hoy —
  `cars_data` es un snapshot instantáneo, no una serie temporal).
- Caída brusca de velocidad (ej. de >100 km/h a <20 km/h en pocos frames)
  sin estar en pit lane → "posible incidente".
- **Filtrar por relevancia al jugador** (decisión de Julian): no narrar
  cualquier incidente del campo — solo rivales cercanos en posición/gap al
  jugador, para no sobreestimular.
- **Nombre condicional** (decisión de Julian): decir el nombre del piloto
  solo si pasa un chequeo de "seguro de pronunciar" (heurística a definir:
  longitud, caracteres ASCII/acentos españoles vs símbolos raros de
  gamertags online) — si no, describir por posición/cercanía en vez de
  nombre ("el auto en el P5" en vez de intentar un nombre raro).

Esto es más grande que salida de pista/bloqueo (necesita tracking de estado
nuevo, no solo un umbral instantáneo) — se diseñó completo acá para no
perder el contexto, pero se implementa en una sesión aparte con tiempo para
probarlo bien en pista, no apurado.

## Verificación

- Suite completa (`test_v7_*.py` + los 5 scripts legacy standalone) en verde
  tras el cambio — ninguno testea directamente los campos nuevos (no había
  tests de `_check_proactive_alerts` antes de esta sesión tampoco).
- Verificado en vivo: los campos nuevos llegan correctos end-to-end en
  `ac_telemetry_fast.json` (reader recompilado, daemon reiniciado, sesión
  reconectada vía SHM).
- **Pendiente que haga Julian**: probar salida de pista y bloqueo de frenos
  en pista real y confirmar que los umbrales se sienten bien (ni mudo ni
  hablando de más).
