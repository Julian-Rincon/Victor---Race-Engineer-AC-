#!/usr/bin/env python3
"""
E2E: shm_simulator.exe → ac_shm_reader.exe (wine real) → engineer._file_worker.

Valida que el ingeniero ingiere los archivos REALES escritos por el lector:
conexión, track, track_len (spotter habilitado), status, oponentes con
nombres, y el mapa de pilotos por posición. Sin audio (mocks).

Ejecutar CON AC parado:  python3 test_e2e_pipeline.py
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.modules['pyaudio'] = mock.MagicMock()
_piper_mock = mock.MagicMock()
_piper_mock.PiperVoice.load.return_value = mock.MagicMock()
sys.modules['piper'] = _piper_mock
sys.modules['piper.voice'] = _piper_mock

import engineer as eng  # noqa: E402
from test_shm_pipeline import wine_env, WINE, DIR, FAST, SLOW  # noqa: E402

eng._tts_pq.put = lambda item: None  # mudo

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra and not cond else ""))


def main() -> int:
    if subprocess.run(["pgrep", "-f", "acs.exe"], capture_output=True).returncode == 0:
        print("AC está corriendo — cierra el juego primero.")
        return 2
    subprocess.run(["pkill", "-f", "shm_simulator.exe"], capture_output=True)
    subprocess.run(["pkill", "-f", "ac_shm_reader.exe"], capture_output=True)
    time.sleep(1)
    FAST.unlink(missing_ok=True)
    SLOW.unlink(missing_ok=True)

    env = wine_env()
    print("[1/3] Simulador + reader bajo wine ...")
    sim = subprocess.Popen([str(WINE), str(DIR / "shm_simulator.exe")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    rdr = subprocess.Popen([str(WINE), str(DIR / "ac_shm_reader.exe")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 30
    while time.time() < deadline and not (FAST.exists() and SLOW.exists()):
        time.sleep(0.5)
    if not (FAST.exists() and SLOW.exists()):
        print("TIMEOUT esperando JSONs del reader")
        sim.terminate(); rdr.terminate()
        return 1

    print("[2/3] Arrancando _file_worker del ingeniero ...")
    threading.Thread(target=eng._file_worker, daemon=True, name="telem").start()
    deadline = time.time() + 10
    while time.time() < deadline and not eng._connected:
        time.sleep(0.2)
    time.sleep(2.5)  # dejar que procese slow packet + varios fast

    print("[3/3] Verificando estado interno del ingeniero ...")
    with eng._lock:
        t = eng._current
    check("conectado", eng._connected is True)
    check("track", t.track == "ks_test_track", f"got={t.track!r}")
    check("car", t.car == "test_car_gt3", f"got={t.car!r}")
    check("session RACE", t.session == 2, f"got={t.session}")
    check("track_len llegó (spotter habilitado)", abs(t.track_len - 4318.0) < 1, f"got={t.track_len}")
    check("status LIVE", t.status == 2, f"got={t.status}")
    check("fuel", abs(t.fuel - 33.5) < 0.01, f"got={t.fuel}")
    check("posición P3", t.position == 3, f"got={t.position}")
    check("2 rivales en cars_data", len(t.cars_data) == 2, f"got={len(t.cars_data)}")
    check("sector_index", t.sector_index == 1, f"got={t.sector_index}")
    names = {c.get("driver_name") for c in t.cars_data}
    check("nombres de rivales", names == {"Ayrton Senna", "Alain Prost"}, f"got={names}")
    with eng._drivers_lock:
        drivers = dict(eng._drivers)
    check("mapa pilotos por posición (P1=Prost)",
          drivers.get(1, {}).get("name") == "Alain Prost", f"got={drivers}")
    check("mapa pilotos (P2=Senna)",
          drivers.get(2, {}).get("name") == "Ayrton Senna", f"got={drivers}")

    sim.terminate(); rdr.terminate()
    subprocess.run(["pkill", "-f", "shm_simulator.exe"], capture_output=True)
    subprocess.run(["pkill", "-f", "ac_shm_reader.exe"], capture_output=True)

    print("\n" + "=" * 60)
    print(f"  {len(PASS)} OK, {len(FAIL)} FAIL" + (f" → {FAIL}" if FAIL else ""))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
