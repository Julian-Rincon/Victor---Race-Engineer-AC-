# Victor — Wake word "Victor" (2026-07-09)

## Contexto y motivación

El 2026-07-09 se encontró y arregló un bug de rendimiento en `victor_ears.py`
(la sesión ONNX de Silero VAD corría sin límite de hilos, saturando la CPU y
tumbando el arranque de Content Manager — ver commit del mismo día). Con la CPU
resuelta, quedó expuesto un problema distinto: la puerta VAD (`SpeechGate`)
confirma "es voz humana" pero no confirma que esa voz vaya dirigida a Victor.
Con música de fondo o conversación ambiente, cualquier voz que pase el umbral
dispara transcripción y, si el texto no coincide con un comando local,
termina generando una respuesta completa del LLM (visto en vivo: "Carajos me
dices hola" → Victor respondió un saludo de bienvenida a carrera sin que
nadie le hablara).

Julian pidió replicar el patrón "assertive" de CrewChief — que exige una
palabra de activación antes de aceptar un comando (su `triggerSreWrapper` con
grammar propia, separado del reconocedor de comandos completo) — pero
adaptado a lo que Victor ya tiene, sin nuevas dependencias ni modelos.

## Requisitos

1. Ningún comando por voz (local o LLM) se ejecuta si el piloto no dijo
   "Victor" primero. Aplica parejo a comandos locales (cállate, repite, modo
   gap, etc.) y a preguntas libres — decisión de Julian: consistencia total,
   no solo para el LLM.
2. Las alertas proactivas (combustible, neumáticos, banderas, spotter) no se
   tocan — son 100% telemetría, sin voz, ya no pasan por este filtro.
3. Tolerante a que Whisper transcriba mal "Victor" bajo música/ruido (ej.
   "Bictor", "Vitor", "Picto") — sin agregar dependencias nuevas.
4. Sin gasto adicional de recursos relevante: sigue usando la transcripción
   de Whisper que ya se hacía (no hay forma barata de evitar esa llamada sin
   un spotter de audio dedicado, que se descarta por requerir entrenar un
   modelo nuevo para español — fuera de alcance).
5. Suite de tests en verde; nueva cobertura para el matching del wake word.

## Diseño

### `has_wake_word(text) -> tuple[bool, str]` en `victor_wake.py` (nuevo módulo)

Módulo nuevo, no dentro de `engineer.py` — sigue el mismo patrón que
`victor_ears.py`/`victor_memory.py`/`victor_brain.py`: pieza chica,
sin efectos secundarios de importación, testeable con pytest limpio.
Importar `engineer.py` directamente en tests no es viable — carga TTS/DB al
importar, y los tests legacy (`test_v6_*.py`, `test_features.py`,
`test_e2e_pipeline.py`) mockean `sys.modules['piper']` de forma global,
contaminando el proceso si se mezclan con pytest (ver notas de v7 en
memoria del proyecto). `engineer.py` solo importa y llama a la función.

- Normaliza `text`: minúsculas, sin acentos (tabla de reemplazo simple,
  á/é/í/ó/ú/ü → a/e/i/o/u).
- Revisa solo las primeras 3 palabras del texto normalizado (el wake word se
  dice al principio; revisar todo el texto arriesga falsos positivos con
  palabras parecidas mencionadas de pasada).
- Para cada una de esas palabras, compara contra `"victor"` con
  `difflib.SequenceMatcher(None, palabra, "victor").ratio()`. Hay match si
  el ratio supera 0.75 **y** la diferencia de longitud contra "victor" (6
  caracteres) es de a lo sumo 1. Valores medidos con `difflib` real (no
  supuestos): "bictor"→0.833, "vitor"→0.909 (diff 1) — ambos pasan; pero
  "victoria" da ratio 0.857 con diff 2, que con un guardia de 2 colaría como
  falso positivo (es una palabra real, no un error de transcripción) — por
  eso el guardia se ajustó a 1. "victorioso" (diff 4, ratio 0.750) queda
  excluido por ambos lados. Trade-off aceptado y documentado: "vector"
  (diff 0, ratio 0.833) pasaría el filtro si alguien lo dice de pasada —
  no se persigue precisión perfecta, solo reducir falsos positivos de
  música/ruido ambiente.
- Devuelve `(True, texto_sin_wake_word)` si hay match — el texto resultante
  quita esa palabra para que el matching de keywords existente (`_MUTE_KW`,
  `_FUEL_KW`, etc.) siga funcionando igual sobre el resto de la frase.
- Devuelve `(False, text)` si no hay match.

### Enganche en `_handle_voice()`

Justo después de:
```python
text = _transcribe(wav_path)
if not text or len(text.strip()) < 3:
    ...
```
se agrega (con `import victor_wake` al inicio del archivo):
```python
has_wake, text = victor_wake.has_wake_word(text)
if not has_wake:
    print(f"[STT] Sin 'Victor' — ignorado: '{text}'")
    return
```
antes de imprimir `[Piloto] {text}` y de calcular `words = _words(text)`. Todo
lo que sigue (comandos locales, prompts de Groq) queda sin cambios — ya
recibe el texto limpio.

### Fuera de alcance

- `victor_ears.py` / `SpeechGate` no cambian — la puerta de audio (RMS + VAD)
  sigue igual. El wake word es un filtro de *contenido* post-transcripción,
  no de *audio*.
- No se agrega un spotter de wake word dedicado (tipo openWakeWord) — pediría
  entrenar/adaptar un modelo para "Victor" en español, y Julian pidió usar
  lo que ya tenemos.
- No cambia el comportamiento cuando SÍ hay wake word — mismo flujo de
  comandos locales / LLM que ya existía.

## Testing

- `test_v7_ears.py` no cambia (no se tocó `victor_ears.py`).
- Nuevo `test_v7_wake.py` para `victor_wake.has_wake_word` — casos medidos
  con `difflib` real: "victor cállate" → True, resto "cállate"; "bictor
  cuanto combustible" → True, fuzzy (ratio 0.833); "vitor dame el gap" →
  True, fuzzy (ratio 0.909, diff 1); "hola como estas" → False (ratio 0.2);
  "el victorioso ganó" → False (diff 4, ratio 0.750, no supera el umbral);
  frase vacía → False.

## Verificación en vivo

Julian va a probar hablándole a Victor con música de fondo (sin audífonos)
para confirmar: (a) que música/ruido ambiente ya no dispara respuestas sin
mencionar a Victor, (b) que decir "Victor, ..." sí funciona incluso con
ruido, y (c) que la telemetría real de AC se lee y se usa correctamente en
las respuestas — esto último requiere una sesión de AC realmente cargada
(no solo el menú de Content Manager) para que `ac_shm_reader.exe` deje de
reportar "AC no disponible" y el daemon reciba datos reales.
