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
