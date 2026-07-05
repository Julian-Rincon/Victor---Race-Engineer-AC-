# Victor v7 — Diseño (2026-07-05)

## Contexto y motivación

Auditoría del 2026-07-05 encontró que Victor v6 estaba **funcionalmente decapitado**:
`engineer.py` cargaba la API key de Groq y el modelo Piper desde
`~/Documentos/Proyectos/jarvis-ironman/` — carpeta remanente del viejo JARVIS que ya
no contiene ni `.env` ni `voices/`. Resultado real: sin LLM, sin STT, y TTS caído a
espeak. Además la investigación de mercado (CrewChief V4, DRE, RACEngineer.ai,
RaceCrewAI, Simulator Controller) muestra tres brechas competitivas: fiabilidad del
cerebro (un solo backend), memoria del piloto entre sesiones, y modos de verbosidad.
El pedido de Julian: inteligencia vía las APIs de NEXUS, escucha siempre activa sin
falsos positivos "enseñándole" con Piper de forma interna, sistema completo y
optimizado.

## Requisitos

1. LLM vía las APIs que ya maneja NEXUS, con fallback — nunca mudo por un 429/500.
2. Siempre atento: escucha continua manos-en-volante (no push-to-talk como
   RACEngineer), sin falsos positivos con ruido de motor/juego.
3. Piper interno como herramienta de enseñanza: sintetizar comandos y ruido para
   calibrar/validar el pipeline de voz offline.
4. Autocontenido: sin dependencias a carpetas muertas; assets locales.
5. Igualar o superar comparables: memoria de piloto entre sesiones + verbosidad.
6. Suite de tests en verde; sin regresiones v6.

## Arquitectura

### victor_config.py
Carga `.env` de nexus-core (claves `NEXUS_*`), resuelve rutas de assets locales
(`voices/es_davefx_medium/model.onnx`, `models/silero_vad.onnx`), y persiste
calibración en `data/victor_calibration.json`.

### victor_brain.py — cerebro multi-backend
Cadena **cloud-only**: Groq (llama-3.3-70b-versatile) → Cerebras (gpt-oss-120b) →
NVIDIA NIM (llama-4-maverick) → Gemini (endpoint OpenAI-compat). Un solo code path
OpenAI-compatible. Circuit breaker por backend (3 fallos → 5 min pausa, patrón
TieredBrain de NEXUS). **Sin Ollama a propósito**: Victor solo corre durante juegos y
el modelo local ocupa ~5 GB de VRAM (incidente freeze 2026-07-03).
STT: Groq whisper-large-v3 (con rechazo anti-alucinación por no_speech_prob/logprob)
→ fallback Deepgram Nova-3.

### victor_ears.py — oídos con VAD real
`SileroVad`: wrapper ONNX (frames 512 muestras @16 kHz, estado 2x1x128, entrada
float32 normalizada desde int16 de arecord). `SpeechGate`: máquina de estados
testeable offline que reemplaza el gate por amplitud del audio worker:
RMS como prefiltro barato → Silero confirma voz (prob ≥ umbral, N frames
consecutivos) → grabar; fin por prob < umbral bajo sostenido. El ruido de motor es
banda ancha sin estructura de voz → Silero lo rechaza aunque el RMS sea alto.
Se mantienen `_tts_busy` y supresión post-TTS (anti-eco de la propia voz).

### tools/teach_victor.py — enseñanza con Piper interno
Sintetiza con Piper: (a) comandos reales ("cuánto combustible queda", "victor,
estado"...), (b) frases propias del TTS de Victor. Genera ruido tipo motor
(ruido marrón filtrado + armónicos) a varios niveles RMS. Corre todo por el
`SpeechGate` y mide: sensibilidad (voz Piper debe disparar) y falsos positivos
(ruido NO debe disparar). Ajusta umbral Silero por búsqueda simple y escribe
`data/victor_calibration.json`. Es también test de regresión (`test_v7_ears.py`).

### victor_memory.py — memoria del piloto
JSON por (pista, coche): mejor vuelta histórica, zonas débiles recurrentes, nota del
último debrief. Se inyecta como bloque "MEMORIA DEL PILOTO" en el briefing de sesión;
se actualiza tras cada debrief. Paridad con "driver memory" de RACEngineer.ai.

### engineer.py v7 (edits mínimos)
`_groq()` → `_llm()` delegando en victor_brain; `_transcribe()` → brain; audio worker
usa SpeechGate; comandos de verbosidad ("modo escueto/normal/coach") que escalan
max_tokens y estilo; inyección de memoria en briefing/debrief.

## Manejo de errores
- Backend caído → siguiente en cadena; todos caídos → frase fija local ("sin enlace
  con boxes") una sola vez por minuto, spotter sigue (nunca depende del LLM).
- Silero no carga → fallback al gate RMS v6 (degradación explícita en log).
- STT vacío/alucinado → descartar en silencio (como v6).

## Testing
- `test_v7_brain.py`: fallback en cadena y circuit breaker con requests mockeado.
- `test_v7_ears.py`: SpeechGate con audio real sintetizado (Piper + ruido) offline.
- `test_v7_memory.py`: persistencia e inyección.
- Suite v6 completa sin regresiones.

## Decisión de diseño tomada en autonomía
Julian pidió ejecución nocturna sin bloqueo; las decisiones de alcance (memoria de
piloto y verbosidad como diferenciales; excluir Ollama por VRAM) se derivaron de su
mensaje y de la memoria del proyecto, y quedan documentadas aquí para su revisión.
