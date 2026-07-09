<p align="center">
  <img src="victor_logo.png" alt="Victor" width="220">
</p>

<h1 align="center">Victor — AI Race Engineer para Assetto Corsa</h1>

<p align="center">
  Un ingeniero de carrera con voz, que escucha telemetría real y responde en
  español, corriendo 100% nativo en Linux.
</p>

---

## ¿Qué es esto?

Victor es un asistente de voz para [Assetto Corsa](https://assettocorsa.gg/)
pensado como alternativa a herramientas como [CrewChief](https://gitlab.com/mr_belowski/CrewChief)
o [Crew Chief V4](https://thecrewchief.org/), pero:

- **Nativo en Linux** — no necesita Windows Speech Recognition ni correr por
  fuera del prefix de Wine/Proton donde vive el juego.
- **Habla español** de verdad (no un locutor pregrabado con huecos rellenados
  a la fuerza) — usa un LLM en la nube para redactar cada respuesta en
  contexto.
- **Escucha y responde preguntas libres**, no solo comandos de una gramática
  fija — "¿cómo van los neumáticos?" y "dame el estado de las gomas"
  funcionan igual.
- **Lee la memoria compartida real de AC** (misma fuente que usa CrewChief),
  así que sabe combustible, temperaturas, daños, posición, sectores y rivales
  con la misma fidelidad.

No es un proyecto de una empresa — es una herramienta personal que comparto
por si le sirve a alguien más de la comunidad de sim racing en Linux.

## Qué hace

- **Spotter determinístico** (sin LLM, <150ms) — avisa coches al lado,
  "sigue ahí", "despejado", con la misma lógica de zonas que usa CrewChief.
- **Alertas proactivas** — combustible bajo (aviso <3.5 vueltas, crítico
  <1.8), temperatura de neumáticos (aviso >95°C, crítico >105°C), desgaste,
  bandera azul, rival que se acerca, ventana de pit.
- **Resumen de vuelta** — tiempo, consumo, ¿vuelta rápida personal?, delta
  contra el mejor tiempo por sectores.
- **Estrategia de pits** — cálculo de vueltas de combustible restantes y
  recomendación de cuándo entrar.
- **Memoria del piloto** — recuerda tu mejor vuelta y tus zonas débiles
  recurrentes por combinación pista+auto entre sesiones, y las menciona en
  el briefing de la siguiente.
- **Comandos de voz y consultas libres** — "cállate", "repite", "modo gap",
  o cualquier pregunta abierta sobre la carrera; todo pasa por un LLM con el
  contexto completo de la telemetría actual.
- **Wake word** — nada se ejecuta si no empezás diciendo "Victor" primero
  (tolerante a que la transcripción lo escuche mal). Evita que música o
  conversación de fondo dispare respuestas sin que nadie le hable.
- **Cerebro multi-backend con failover** — Groq → Cerebras → NVIDIA NIM →
  Gemini, con circuit breaker; si un proveedor falla o está caído, sigue
  funcionando con el siguiente sin que se note.

## Cómo funciona (arquitectura)

```
Assetto Corsa (Wine/Proton)
   │  Shared Memory de Windows (acpmf_physics / graphics / static / crewchief)
   ▼
ac_shm_reader.exe   (C, compilado con MinGW, corre con el wine del prefix de AC)
   │  escribe JSON cada 100ms junto al propio .exe
   ▼
ac_telemetry_fast.json / ac_telemetry_slow.json
   │
   ▼
engineer.py   (daemon Linux — nada de esto corre bajo Wine)
   ├─ victor_ears.py    → VAD real (Silero, ONNX) + gate de grabación
   ├─ victor_wake.py    → exige "Victor" antes de aceptar un comando
   ├─ victor_brain.py   → LLM multi-backend (Groq/Cerebras/NVIDIA/Gemini)
   └─ victor_memory.py  → memoria del piloto entre sesiones (JSON local)
```

La misma memoria compartida (`acpmf_*`) que usa CrewChief es la fuente de
datos — por eso Victor puede correr en paralelo a CrewChief sin pisarlo, o
reemplazarlo directamente.

## Requisitos

- Linux, con Assetto Corsa corriendo vía Steam + Proton (probado con
  GE-Proton, appid `244210`).
- Python 3.10+.
- `arecord` (paquete `alsa-utils`) para capturar el micrófono.
- Una API key de al menos uno de estos proveedores (todos tienen capa
  gratuita): [Groq](https://console.groq.com/), [Cerebras](https://cloud.cerebras.ai/),
  [NVIDIA NIM](https://build.nvidia.com/), [Gemini](https://aistudio.google.com/).
  Cuantos más configures, más resiliente es el failover.
- `mingw-w64-gcc` si querés recompilar `ac_shm_reader.exe` (el binario
  compilado no se versiona — ver más abajo).

## Instalación

```bash
git clone git@github.com:Julian-Rincon/Victor---Race-Engineer-AC-.git
cd Victor---Race-Engineer-AC-
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Assets de voz y VAD (no van en el repo)

Dos modelos no están versionados (pesan varios MB y son de terceros,
descargables libremente):

1. **Voz Piper** (TTS en español) — cualquier voz de
   [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/es)
   sirve. Colocar el `.onnx` en `voices/<nombre>/model.onnx` y ajustar
   `PIPER_MODEL_PATH` en `victor_config.py` si el nombre no coincide.
2. **Silero VAD v5** — descargar `silero_vad.onnx` de
   [snakers4/silero-vad](https://github.com/snakers4/silero-vad) y colocarlo
   en `models/silero_vad.onnx`.

### Compilar el lector de memoria compartida

```bash
x86_64-w64-mingw32-gcc -O2 -o ac_shm_reader.exe ac_shm_reader.c
```

Este `.exe` corre con el **mismo Wine/Proton que usa Assetto Corsa** — lee la
memoria compartida de Windows que el juego expone y no se puede reemplazar
por un lector nativo de Linux.

### Configuración (API keys)

`victor_config.py` busca las keys primero en el entorno del proceso, y si no
las encuentra cae a un `.env` con nombres `NEXUS_*` (herencia de un proyecto
personal más grande del que Victor salió). **No hace falta ese otro
proyecto** — alcanza con exportar las variables vos mismo o crear un `.env`
en la raíz con este formato:

```bash
NEXUS_GROQ_API_KEY=gsk_...
NEXUS_CEREBRAS_API_KEY=csk_...
NEXUS_NVIDIA_API_KEY=nvapi-...
NEXUS_GEMINI_API_KEY=AIza...
```

Ninguna es obligatoria por sí sola — Victor usa la primera que encuentre
disponible y sigue con la siguiente si una falla.

## Uso

```bash
bash start.sh start      # daemon + lector SHM, sin interfaz
bash start.sh tray        # lo mismo, más un ícono en la bandeja del sistema
bash start.sh stop        # detiene todo
bash start.sh status      # ¿qué está corriendo?
bash start.sh logs        # tail -f del log del daemon
bash start.sh shm-log     # tail -f del log del lector de memoria compartida
```

Arrancalo **antes** de entrar a pista (no hace falta ningún widget dentro de
AC — el lector de memoria compartida se conecta solo apenas el juego expone
sus datos).

Una vez corriendo, hablale empezando con **"Victor"**:

> "Victor, ¿cómo vamos de combustible?"
> "Victor, cállate" / "Victor, repite" / "Victor, modo gap"

Las alertas proactivas (combustible, neumáticos, banderas, spotter) no
necesitan que le hables — hablan solas cuando corresponde.

## Tests

```bash
python3 -m pytest test_v7_wake.py test_v7_ears.py test_v7_brain.py test_v7_memory.py -v
```

Estos cuatro son la suite moderna, pensada para pytest limpio. Los demás
`test_*.py` (`test_features.py`, `test_e2e_pipeline.py`, etc.) son bancos de
prueba más viejos que simulan la memoria compartida de AC sin necesitar el
juego corriendo — son scripts standalone, se corren uno por uno
(`python3 test_shm_pipeline.py`), **no junto con pytest** en la misma
invocación.

## Estado del proyecto

Desarrollo activo, uso personal diario en carreras propias. La arquitectura
(spotter, memoria compartida, estrategia de pits) está directamente
inspirada e investigada a partir de CrewChief V4 — gracias a ese proyecto por
años de código abierto de referencia en este espacio.

## Licencia

[MIT](LICENSE).
