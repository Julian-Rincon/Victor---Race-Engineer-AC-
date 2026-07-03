#!/usr/bin/env python3
"""
Test end-to-end del pipeline SHM de Victor SIN Assetto Corsa:

  shm_simulator.exe (crea páginas acpmf_* con valores conocidos, offsets del
  C# de CrewChief)  →  ac_shm_reader.exe  →  ac_telemetry_{fast,slow}.json

Valida campo a campo que el lector interpreta el layout correctamente y que
emite todo lo que engineer.py necesita (track_len, status, oponentes, etc.).

Ejecutar:  python3 test_shm_pipeline.py     (con AC CERRADO)
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
STEAM = Path.home() / ".local/share/Steam"
WINE = STEAM / "compatibilitytools.d/GE-Proton10-34/files/bin/wine"
PREFIX = STEAM / "steamapps/compatdata/244210/pfx"
FAST = DIR / "ac_telemetry_fast.json"
SLOW = DIR / "ac_telemetry_slow.json"

FAILS = []


def check(name, got, want, tol=None):
    if tol is not None:
        ok = got is not None and abs(got - want) <= tol
    else:
        ok = got == want
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {name}: {got!r}" + ("" if ok else f"  (esperado {want!r})"))
    if not ok:
        FAILS.append(name)


def wine_env():
    env = os.environ.copy()
    xauth = sorted(Path(f"/run/user/{os.getuid()}").glob("xauth_*"))
    env.update({
        "WINEPREFIX": str(PREFIX) + "/",
        "WINEDEBUG": "-all",
        "WINENTSYNC": "1",
        "WINEESYNC": "1",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "XAUTHORITY": str(xauth[0]) if xauth else "",
        "WINEDLLPATH": f"{STEAM}/compatibilitytools.d/GE-Proton10-34/files/lib/vkd3d:"
                       f"{STEAM}/compatibilitytools.d/GE-Proton10-34/files/lib/wine",
        "LD_LIBRARY_PATH": f"{STEAM}/compatibilitytools.d/GE-Proton10-34/files/lib",
    })
    env.setdefault("DISPLAY", ":0")
    return env


def main() -> int:
    if subprocess.run(["pgrep", "-f", "AssettoCorsa.exe|acs.exe"],
                      capture_output=True).returncode == 0:
        print("AC está corriendo — cierra el juego para usar el simulador.")
        return 2

    subprocess.run(["pkill", "-f", "shm_simulator.exe"], capture_output=True)
    subprocess.run(["pkill", "-f", "ac_shm_reader.exe"], capture_output=True)
    time.sleep(1)
    for f in (FAST, SLOW):
        f.unlink(missing_ok=True)

    env = wine_env()
    print("[1/4] Lanzando shm_simulator.exe ...")
    sim = subprocess.Popen([str(WINE), str(DIR / "shm_simulator.exe")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(6)  # dejar que wineserver arranque y las páginas existan

    print("[2/4] Lanzando ac_shm_reader.exe ...")
    rdr = subprocess.Popen([str(WINE), str(DIR / "ac_shm_reader.exe")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    print("[3/4] Esperando JSONs ...")
    deadline = time.time() + 30
    while time.time() < deadline:
        if FAST.exists() and SLOW.exists():
            break
        time.sleep(0.5)
    else:
        print("TIMEOUT: el reader no escribió los JSON. Log del reader:")
        rdr.terminate()
        print((rdr.stdout.read() or b"").decode(errors="replace")[-2000:])
        sim.terminate()
        return 1
    time.sleep(1.5)  # un par de ciclos más

    fast = json.loads(FAST.read_text())
    slow = json.loads(SLOW.read_text())

    print("[4/4] Verificando campos ...")
    print("── fast (physics/graphics) ──")
    check("speed", fast.get("speed"), 123.4, tol=0.11)
    check("rpm", fast.get("rpm"), 5500)
    check("gear", fast.get("gear"), 4)
    check("fuel", fast.get("fuel"), 33.5, tol=0.01)
    check("lap", fast.get("lap"), 5)
    check("position", fast.get("position"), 3)
    check("lap_time_ms", fast.get("lap_time_ms"), 61234)
    check("best_lap_ms", fast.get("best_lap_ms"), 94500)
    check("last_lap_ms", fast.get("last_lap_ms"), 95321)
    check("spline", fast.get("spline"), 0.421337, tol=1e-5)
    check("tyre_compound", fast.get("tyre_compound"), "Soft (S)")
    check("tyre_fl", fast.get("tyre_fl"), 82.1, tol=0.05)
    check("tyre_wear_fr", fast.get("tyre_wear_fr"), 93.2, tol=0.05)
    check("wx", fast.get("wx"), 100.5, tol=0.01)
    check("wz", fast.get("wz"), -200.25, tol=0.01)
    check("vx", fast.get("vx"), 1.5, tol=0.01)
    check("vz", fast.get("vz"), 34.2, tol=0.01)
    check("dmg_front", fast.get("dmg_front"), 10.0, tol=0.05)
    check("cars", fast.get("cars"), 3)

    print("── fast (campos que engineer.py NECESITA) ──")
    check("track_len", fast.get("track_len"), 4318.0, tol=0.5)
    check("status (LIVE=2)", fast.get("status"), 2)
    check("sector_index", fast.get("sector_index"), 1)
    check("last_sector_ms", fast.get("last_sector_ms"), 30500)
    check("in_pitlane", fast.get("in_pitlane"), False)

    print("── fast.cars_data (oponentes vía crewchief SHM) ──")
    cars = fast.get("cars_data", [])
    check("n oponentes", len(cars), 2)
    senna = next((c for c in cars if c.get("driver_name") == "Ayrton Senna"), None)
    prost = next((c for c in cars if c.get("driver_name") == "Alain Prost"), None)
    check("Senna presente", senna is not None, True)
    check("Prost presente", prost is not None, True)
    if senna:
        check("Senna race_pos", senna.get("race_pos"), 2)
        check("Senna spline", senna.get("spline"), 0.4230, tol=1e-4)
        check("Senna speed km/h", senna.get("speed"), 124.2, tol=0.2)
        check("Senna lap", senna.get("lap"), 5)
        check("Senna wx", senna.get("wx"), 104.5, tol=0.01)
        check("Senna wz", senna.get("wz"), -198.0, tol=0.01)
        check("Senna in_pit", senna.get("in_pit"), False)
    if prost:
        check("Prost race_pos", prost.get("race_pos"), 1)

    print("── slow (static/session) ──")
    check("track", slow.get("track"), "ks_test_track")
    check("track_layout", slow.get("track_layout"), "layout_a")
    check("car", slow.get("car"), "test_car_gt3")
    check("session", slow.get("session"), 2)
    check("time_left", slow.get("time_left"), 1800)

    print("── slow.all_drivers (nombres por posición) ──")
    drivers = slow.get("all_drivers", {})
    check("n drivers", len(drivers), 3)
    by_pos = {v.get("pos"): v.get("name") for v in drivers.values()} if drivers else {}
    check("P1", by_pos.get(1), "Alain Prost")
    check("P2", by_pos.get(2), "Ayrton Senna")
    check("P3", by_pos.get(3), "Julian Rincon")

    sim.terminate()
    rdr.terminate()
    subprocess.run(["pkill", "-f", "shm_simulator.exe"], capture_output=True)
    subprocess.run(["pkill", "-f", "ac_shm_reader.exe"], capture_output=True)

    print()
    if FAILS:
        print(f"RESULTADO: {len(FAILS)} fallos → {FAILS}")
        return 1
    print("RESULTADO: pipeline SHM completo OK ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
