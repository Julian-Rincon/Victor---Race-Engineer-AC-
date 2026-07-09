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
