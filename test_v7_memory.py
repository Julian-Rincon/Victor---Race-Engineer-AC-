"""Tests para victor_memory: persistencia e inyección de memoria del piloto."""
from __future__ import annotations

import victor_memory as vm


def test_no_memory_gives_empty_context_block(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    assert vm.context_block("monza", "some_gt3") == ""


def test_record_debrief_persists_best_lap(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    vm.record_debrief("monza", "some_gt3", best_lap_ms=95123.0)
    block = vm.context_block("monza", "some_gt3")
    assert "95.123s" in block


def test_best_lap_never_regresses(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    vm.record_debrief("monza", "some_gt3", best_lap_ms=95000.0)
    vm.record_debrief("monza", "some_gt3", best_lap_ms=96000.0)  # peor — no debe sobreescribir
    mem = vm.load()
    assert mem["monza::some_gt3"]["best_lap_ms"] == 95000.0


def test_recurring_weak_zones_accumulate_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    vm.record_debrief("spa", "gt3car", best_lap_ms=0, worst_zone_label="Eau Rouge")
    vm.record_debrief("spa", "gt3car", best_lap_ms=0, worst_zone_label="Eau Rouge")
    vm.record_debrief("spa", "gt3car", best_lap_ms=0, worst_zone_label="Pouhon")
    mem = vm.load()
    zones = mem["spa::gt3car"]["recurring_weak_zones"]
    assert zones == ["Eau Rouge", "Pouhon"]


def test_context_block_includes_last_debrief_note(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    vm.record_debrief("imola", "gte1", best_lap_ms=0, debrief_note="Frenada tardía en Tosa")
    block = vm.context_block("imola", "gte1")
    assert "Frenada tardía en Tosa" in block


def test_different_track_car_combos_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.cfg, "DRIVER_MEMORY_PATH", tmp_path / "mem.json")
    vm.record_debrief("monza", "carA", best_lap_ms=90000.0)
    assert vm.context_block("monza", "carB") == ""
    assert vm.context_block("spa", "carA") == ""
