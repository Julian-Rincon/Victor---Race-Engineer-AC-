# Victor Wake Word Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Victor solo ejecuta comandos de voz (locales o al LLM) si el piloto dijo "Victor" primero — filtra música/ruido/conversación ambiente que hoy pasa el VAD y dispara respuestas sin que nadie le hable.

**Architecture:** Nuevo módulo puro `victor_wake.py` (sin efectos secundarios de importación, testeable con pytest limpio) con `has_wake_word(text) -> tuple[bool, str]`. Se engancha en `engineer.py::_handle_voice()` justo después de la transcripción y antes de cualquier dispatch de comando. `victor_ears.py` (la puerta de audio VAD/RMS) no cambia — esto es un filtro de contenido, no de audio.

**Tech Stack:** Python 3, `difflib` (stdlib, sin dependencias nuevas), pytest.

## Global Constraints

- Sin dependencias nuevas — usar solo `difflib` (stdlib), igual que el resto del proyecto evita paquetes extra para piezas chicas.
- Match tolerante: ratio `difflib.SequenceMatcher(None, palabra, "victor").ratio() > 0.75` **y** diferencia de longitud contra "victor" (6 caracteres) `<= 1`.
- Solo revisar las primeras 3 palabras del texto transcrito.
- Aplica a TODO lo disparado por voz (comandos locales y preguntas al LLM). Las alertas proactivas (fuel/tyres/flags/spotter) no pasan por voz — no se tocan.
- Spec de referencia: `docs/superpowers/specs/2026-07-09-victor-wake-word-design.md`.

---

### Task 1: `victor_wake.py` — matching del wake word

**Files:**
- Create: `victor_wake.py`
- Test: `test_v7_wake.py`

**Interfaces:**
- Produces: `has_wake_word(text: str) -> tuple[bool, str]` — usado por `engineer.py` en Task 2.

- [ ] **Step 1: Escribir los tests (deben fallar — el módulo no existe aún)**

Crear `/home/JulianRincon/Games/ac-engineer/test_v7_wake.py`:

```python
"""Tests para victor_wake: filtro de wake word 'Victor' post-transcripción."""
from __future__ import annotations

import victor_wake as vw


def test_exact_wake_word_stripped():
    matched, rest = vw.has_wake_word("victor cállate")
    assert matched is True
    assert rest == "cállate"


def test_fuzzy_mishearing_bictor():
    # ratio real medido: SequenceMatcher("bictor","victor").ratio() == 0.833
    matched, rest = vw.has_wake_word("bictor cuanto combustible")
    assert matched is True
    assert rest == "cuanto combustible"


def test_fuzzy_mishearing_vitor():
    # ratio real medido: SequenceMatcher("vitor","victor").ratio() == 0.909, diff largo 1
    matched, rest = vw.has_wake_word("vitor dame el gap")
    assert matched is True
    assert rest == "dame el gap"


def test_no_wake_word_in_ambient_talk():
    matched, rest = vw.has_wake_word("hola como estas")
    assert matched is False
    assert rest == "hola como estas"


def test_unrelated_word_starting_like_victor_not_matched():
    # "victorioso": diff de longitud 4 (>1) y ratio 0.750 (no supera 0.75) — no debe matchear
    matched, rest = vw.has_wake_word("el victorioso ganó la carrera")
    assert matched is False
    assert rest == "el victorioso ganó la carrera"


def test_empty_text():
    matched, rest = vw.has_wake_word("")
    assert matched is False
    assert rest == ""


def test_wake_word_only_checked_in_first_three_words():
    # "victor" aparece en la 4ta palabra — no cuenta, el piloto no abrió con el wake word
    matched, rest = vw.has_wake_word("dame el gap con victor")
    assert matched is False
    assert rest == "dame el gap con victor"


def test_accent_normalization():
    matched, rest = vw.has_wake_word("Víctor, ¿cómo va el combustible?")
    assert matched is True
    assert rest == "¿cómo va el combustible?"
```

- [ ] **Step 2: Correr los tests para confirmar que fallan**

```bash
cd ~/Games/ac-engineer && python3 -m pytest test_v7_wake.py -v
```
Expected: `ModuleNotFoundError: No module named 'victor_wake'` (o similar) en todos los casos.

- [ ] **Step 3: Implementar `victor_wake.py`**

Crear `/home/JulianRincon/Games/ac-engineer/victor_wake.py`:

```python
"""Victor — wake word: filtra comandos de voz que no van dirigidos a Victor.

Capa de CONTENIDO (post-transcripción) — victor_ears.py/SpeechGate ya confirma
"hay voz humana" (VAD real, no solo amplitud); esto confirma "esa voz le habla
a Victor". Necesario porque cualquier voz que pasa el VAD (música con letra,
conversación ambiente) antes disparaba comandos locales o el LLM sin que nadie
le hablara a Victor.
"""

from __future__ import annotations

import difflib

_WAKE_WORD = "victor"
_MAX_CHECK_WORDS = 3
_RATIO_THRESHOLD = 0.75
_MAX_LEN_DIFF = 1
_STRIP_CHARS = ".,¿?¡!"

_ACCENTS = str.maketrans("áéíóúü", "aeiouu")


def _normalize(word: str) -> str:
    return word.lower().translate(_ACCENTS)


def has_wake_word(text: str) -> tuple[bool, str]:
    """Busca 'Victor' (tolerante a errores de transcripción) en las primeras
    `_MAX_CHECK_WORDS` palabras de `text`.

    Devuelve (True, texto_sin_wake_word) si lo encuentra — la palabra se quita
    para que el resto del texto siga matcheando contra las keywords de
    comandos existentes en engineer.py. Devuelve (False, text) si no hay match.
    """
    words = text.split()
    for i, raw in enumerate(words[:_MAX_CHECK_WORDS]):
        word = _normalize(raw.strip(_STRIP_CHARS))
        if not word:
            continue
        if abs(len(word) - len(_WAKE_WORD)) > _MAX_LEN_DIFF:
            continue
        ratio = difflib.SequenceMatcher(None, word, _WAKE_WORD).ratio()
        if ratio > _RATIO_THRESHOLD:
            remaining = words[:i] + words[i + 1:]
            return True, " ".join(remaining).strip()
    return False, text
```

- [ ] **Step 4: Correr los tests para confirmar que pasan**

```bash
cd ~/Games/ac-engineer && python3 -m pytest test_v7_wake.py -v
```
Expected: `8 passed`.

Si algún caso fuzzy falla (ratio real distinto al documentado), ajustar
`_RATIO_THRESHOLD`/`_MAX_LEN_DIFF` — no el test — y volver a correr. Los
valores del test ya están verificados con `difflib` real en esta sesión, así
que no debería hacer falta, pero si la versión de Python difiere el
algoritmo de `SequenceMatcher` es determinístico así que no debería cambiar.

- [ ] **Step 5: Commit**

```bash
cd ~/Games/ac-engineer
git add victor_wake.py test_v7_wake.py
git commit -m "feat: wake word 'Victor' — filtro de contenido para comandos de voz

Sin esto cualquier voz que pasa el VAD (música con letra, conversación
ambiente) dispara comandos locales o el LLM sin que nadie le hable a
Victor. Módulo puro, sin dependencias nuevas (difflib stdlib), separado
de engineer.py para poder testear con pytest limpio (engineer.py carga
TTS/DB al importar)."
```

---

### Task 2: Enganchar en `engineer.py::_handle_voice`

**Files:**
- Modify: `engineer.py:42-45` (imports), `engineer.py:2000-2014` (`_handle_voice`)

**Interfaces:**
- Consumes: `victor_wake.has_wake_word(text: str) -> tuple[bool, str]` (Task 1).

- [ ] **Step 1: Agregar el import**

En `engineer.py`, el bloque de imports locales actual es (líneas 42-45):

```python
import victor_brain
import victor_config as cfg
import victor_ears
import victor_memory
```

Reemplazar por:

```python
import victor_brain
import victor_config as cfg
import victor_ears
import victor_memory
import victor_wake
```

- [ ] **Step 2: Enganchar el filtro en `_handle_voice`**

El inicio actual de `_handle_voice` (líneas 2006-2014) es:

```python
    try:
        print("[STT] Transcribiendo...", flush=True)
        text = _transcribe(wav_path)
        if not text or len(text.strip()) < 3:
            print("[STT] Sin voz reconocible.")
            return

        print(f"[Piloto] {text}")
        words = _words(text)
```

Reemplazar por:

```python
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
```

- [ ] **Step 3: Verificar que el módulo importa sin errores**

`engineer.py` carga TTS/DB de carros al importar (side effects pesados) —
no es seguro importarlo en un test rápido. En su lugar, verificar sintaxis
y que el import resuelve:

```bash
cd ~/Games/ac-engineer && python3 -c "import ast; ast.parse(open('engineer.py').read())" && echo "SINTAXIS OK"
python3 -c "import victor_wake; print(victor_wake.has_wake_word('victor hola'))"
```
Expected: `SINTAXIS OK` y `(True, 'hola')`.

- [ ] **Step 4: Commit**

```bash
cd ~/Games/ac-engineer
git add engineer.py
git commit -m "feat: exigir wake word 'Victor' antes de cualquier comando de voz

Aplica parejo a comandos locales (cállate, repite, modo gap) y a
preguntas libres al LLM, según lo pedido: consistencia total. Las
alertas proactivas (fuel/tyres/flags/spotter) no pasan por voz —
sin cambios ahí."
```

---

### Task 3: Verificación completa y smoke test en vivo

**Files:** ninguno (solo verificación)

- [ ] **Step 1: Correr toda la suite v7 (pytest limpio, sin mezclar con scripts legacy)**

```bash
cd ~/Games/ac-engineer
python3 -m pytest test_v7_wake.py test_v7_ears.py test_v7_brain.py test_v7_memory.py -v
```
Expected: todos los tests en verde (8 nuevos de wake word + los ya existentes de v7).

- [ ] **Step 2: Reiniciar el daemon de Victor con el código nuevo**

```bash
bash ~/Games/ac-engineer/start.sh stop
bash ~/Games/ac-engineer/start.sh start
bash ~/Games/ac-engineer/start.sh logs
```
(Ctrl+C para salir del `tail -f` de logs cuando se confirme el arranque limpio: banner de Victor v7, backends activos, sin tracebacks.)

- [ ] **Step 3: Smoke test manual de contenido (sin necesitar AC corriendo)**

Con el daemon corriendo, hablarle CERCA del micrófono sin decir "Victor"
(ej. "hola qué tal") y confirmar en el log (`tail -f /tmp/ac-engineer.log`)
la línea `[STT] Sin 'Victor' — ignorado: '...'` — no debe sonar ninguna
respuesta de Victor. Luego decir "Victor, ¿cómo vamos?" y confirmar que sí
aparece `[Piloto] ...` seguido de una respuesta hablada.

- [ ] **Step 4: Verificación en vivo pedida por Julian (con música de fondo, sin audífonos)**

Julian carga una sesión real en Content Manager (no solo el menú — el SHM
reader necesita una sesión activa para dejar de reportar "AC no
disponible") y prueba hablándole a Victor con música sonando de fondo.
Mientras tanto, seguir el log en paralelo:

```bash
tail -f /tmp/ac-engineer.log /tmp/ac-engineer-shm.log
```

Confirmar tres cosas:
1. La música/ruido de fondo ya NO dispara respuestas — deben verse líneas
   `[STT] Sin 'Victor' — ignorado: ...` para cualquier cosa que no lo
   mencione, en vez de respuestas del LLM.
2. Decir "Victor, ..." sí dispara respuesta, incluso con la música sonando.
3. El SHM reader deja de repetir "AC no disponible — reintentando" (señal
   de que hay sesión real) y las respuestas de Victor usan datos reales
   (fuel/vueltas/posición que coincidan con lo que se ve en pantalla, no
   ceros o placeholders).

Si el paso 3 falla (SHM sigue sin conectar), es un problema separado de la
sesión de AC/CM, no del wake word — no mezclar el diagnóstico.

- [ ] **Step 5: No hace falta commit — este task es solo verificación.**
