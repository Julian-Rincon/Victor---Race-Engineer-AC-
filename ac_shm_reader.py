#!/usr/bin/env python3
"""
AC Shared Memory Reader — reemplaza el bridge ACEngineer.py widget por completo.

Cómo funciona:
  AC (Windows/Wine) expone 4 regiones de Windows Named Shared Memory:
    Local\acpmf_physics   → física del coche del jugador
    Local\acpmf_graphics  → timing, posición, spline, sesión
    Local\acpmf_static    → track, car model (datos estáticos)
    Local\acpmf_crewchief → datos de TODOS los coches (64 slots)

  Este script corre como proceso Wine (NO en AC's Python app system)
  y lee esas regiones directamente con ctypes, luego escribe JSON a /tmp/.

Lanzar ANTES de que AC empiece (o en cualquier momento — reintenta solo):
  WINEPREFIX=~/.local/share/Steam/steamapps/compatdata/244210/pfx \
  wine python3 ~/Games/ac-engineer/ac_shm_reader.py

  O más simple: el start.sh lo lanzará automáticamente.
"""

import ctypes
import ctypes.wintypes
import json
import os
import sys
import time

# ── Rutas de salida (Z:\tmp\ = /tmp/ en Linux via Wine) ────────────────────────
FAST_FILE = "Z:\\tmp\\ac_telemetry_fast.json"
SLOW_FILE = "Z:\\tmp\\ac_telemetry_slow.json"
FAST_HZ   = 10.0   # 10 Hz para spotter/alertas
SLOW_HZ   = 1.0    # 1 Hz para datos estáticos

# ── Windows API ────────────────────────────────────────────────────────────────
kernel32 = ctypes.windll.kernel32  # type: ignore  (sólo disponible en Wine)

FILE_MAP_READ      = 0x0004
PAGE_READONLY      = 0x02
INVALID_HANDLE     = ctypes.wintypes.HANDLE(-1).value


def _open_shm(name: str) -> ctypes.wintypes.HANDLE:
    h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
    return h


def _map_view(handle, size: int) -> ctypes.c_void_p:
    return kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)


def _close(handle, view):
    if view:
        kernel32.UnmapViewOfFile(view)
    if handle:
        kernel32.CloseHandle(handle)


# ── Structs ctypes (Pack=4, misma que C# StructLayout Pack=4) ─────────────────

class Vec3(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId",          ctypes.c_int),
        ("gas",               ctypes.c_float),
        ("brake",             ctypes.c_float),
        ("fuel",              ctypes.c_float),
        ("gear",              ctypes.c_int),
        ("rpms",              ctypes.c_int),
        ("steerAngle",        ctypes.c_float),
        ("speedKmh",          ctypes.c_float),
        ("velocity",          ctypes.c_float * 3),
        ("accG",              ctypes.c_float * 3),
        ("wheelSlip",         ctypes.c_float * 4),
        ("wheelLoad",         ctypes.c_float * 4),
        ("wheelsPressure",    ctypes.c_float * 4),
        ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear",          ctypes.c_float * 4),
        ("tyreDirtyLevel",    ctypes.c_float * 4),
        ("tyreCoreTemp",      ctypes.c_float * 4),
        ("camberRAD",         ctypes.c_float * 4),
        ("suspensionTravel",  ctypes.c_float * 4),
        ("drs",               ctypes.c_float),
        ("tc",                ctypes.c_float),
        ("heading",           ctypes.c_float),
        ("pitch",             ctypes.c_float),
        ("roll",              ctypes.c_float),
        ("cgHeight",          ctypes.c_float),
        ("carDamage",         ctypes.c_float * 5),   # front, rear, left, right, centre
        ("numberOfTyresOut",  ctypes.c_int),
        ("pitLimiterOn",      ctypes.c_int),
        ("abs",               ctypes.c_float),
        ("kersCharge",        ctypes.c_float),
        ("kersInput",         ctypes.c_float),
        ("autoShifterOn",     ctypes.c_int),
        ("rideHeight",        ctypes.c_float * 2),
        ("turboBoost",        ctypes.c_float),
        ("ballast",           ctypes.c_float),
        ("airDensity",        ctypes.c_float),
        ("airTemp",           ctypes.c_float),
        ("roadTemp",          ctypes.c_float),
        ("localAngularVel",   ctypes.c_float * 3),
        ("finalFF",           ctypes.c_float),
        ("performanceMeter",  ctypes.c_float),
        ("engineBrake",       ctypes.c_int),
        ("ersRecoveryLevel",  ctypes.c_int),
        ("ersPowerLevel",     ctypes.c_int),
        ("ersHeatCharging",   ctypes.c_int),
        ("ersIsCharging",     ctypes.c_int),
        ("kersCurrentKJ",     ctypes.c_float),
        ("drsAvailable",      ctypes.c_int),
        ("drsEnabled",        ctypes.c_int),
        ("brakeTemp",         ctypes.c_float * 4),
        ("clutch",            ctypes.c_float),
        ("tyreTempI",         ctypes.c_float * 4),
        ("tyreTempM",         ctypes.c_float * 4),
        ("tyreTempO",         ctypes.c_float * 4),
        ("isAIControlled",    ctypes.c_int),
        ("tyreContactPoint",  Vec3 * 4),
        ("tyreContactNormal", Vec3 * 4),
        ("tyreContactHeading",Vec3 * 4),
        ("brakeBias",         ctypes.c_float),
        ("localVelocity",     Vec3),
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId",              ctypes.c_int),
        ("status",                ctypes.c_int),    # AC_STATUS enum
        ("session",               ctypes.c_int),    # AC_SESSION_TYPE enum
        ("currentTime",           ctypes.c_wchar * 15),
        ("lastTime",              ctypes.c_wchar * 15),
        ("bestTime",              ctypes.c_wchar * 15),
        ("split",                 ctypes.c_wchar * 15),
        ("completedLaps",         ctypes.c_int),
        ("position",              ctypes.c_int),
        ("iCurrentTime",          ctypes.c_int),
        ("iLastTime",             ctypes.c_int),
        ("iBestTime",             ctypes.c_int),
        ("sessionTimeLeft",       ctypes.c_float),
        ("distanceTraveled",      ctypes.c_float),
        ("isInPit",               ctypes.c_int),
        ("currentSectorIndex",    ctypes.c_int),
        ("lastSectorTime",        ctypes.c_int),
        ("numberOfLaps",          ctypes.c_int),
        ("tyreCompound",          ctypes.c_wchar * 33),
        ("replayTimeMultiplier",  ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),
        ("carCoordinates",        ctypes.c_float * 3),
        ("penaltyTime",           ctypes.c_float),
        ("flag",                  ctypes.c_int),    # AC_FLAG_TYPE enum
        ("idealLineOn",           ctypes.c_int),
        ("isInPitLane",           ctypes.c_int),
        ("surfaceGrip",           ctypes.c_float),
        ("mandatoryPitDone",      ctypes.c_int),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion",            ctypes.c_wchar * 15),
        ("acVersion",            ctypes.c_wchar * 15),
        ("numberOfSessions",     ctypes.c_int),
        ("numCars",              ctypes.c_int),
        ("carModel",             ctypes.c_wchar * 33),
        ("track",                ctypes.c_wchar * 33),
        ("playerName",           ctypes.c_wchar * 33),
        ("playerSurname",        ctypes.c_wchar * 33),
        ("playerNick",           ctypes.c_wchar * 33),
        ("sectorCount",          ctypes.c_int),
        ("maxTorque",            ctypes.c_float),
        ("maxPower",             ctypes.c_float),
        ("maxRpm",               ctypes.c_int),
        ("maxFuel",              ctypes.c_float),
        ("suspensionMaxTravel",  ctypes.c_float * 4),
        ("tyreRadius",           ctypes.c_float * 4),
        ("maxTurboBoost",        ctypes.c_float),
        ("deprecated_1",         ctypes.c_float),
        ("deprecated_2",         ctypes.c_float),
        ("penaltiesEnabled",     ctypes.c_int),
        ("aidFuelRate",          ctypes.c_float),
        ("aidTireRate",          ctypes.c_float),
        ("aidMechanicalDamage",  ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int),
        ("aidStability",         ctypes.c_float),
        ("aidAutoClutch",        ctypes.c_int),
        ("aidAutoBlip",          ctypes.c_int),
        ("hasDRS",               ctypes.c_int),
        ("hasERS",               ctypes.c_int),
        ("hasKERS",              ctypes.c_int),
        ("kersMaxJ",             ctypes.c_float),
        ("engineBrakeSettingsCount", ctypes.c_int),
        ("ersPowerControllerCount",  ctypes.c_int),
        ("trackSPlineLength",    ctypes.c_float),
        ("trackConfiguration",   ctypes.c_wchar * 33),
        ("ersMaxJ",              ctypes.c_float),
        ("isTimedRace",          ctypes.c_int),
        ("hasExtraLap",          ctypes.c_int),
        ("carSkin",              ctypes.c_wchar * 33),
        ("reversedGridPositions", ctypes.c_int),
        ("PitWindowStart",       ctypes.c_int),
        ("PitWindowEnd",         ctypes.c_int),
    ]


class AcsVehicleInfo(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("carId",                          ctypes.c_int),
        ("driverName",                     ctypes.c_byte * 64),
        ("carModel",                       ctypes.c_byte * 64),
        ("speedMS",                        ctypes.c_float),
        ("bestLapMS",                      ctypes.c_int),
        ("lapCount",                       ctypes.c_int),
        ("currentLapInvalid",              ctypes.c_int),
        ("currentLapTimeMS",               ctypes.c_int),
        ("lastLapTimeMS",                  ctypes.c_int),
        ("worldPosition",                  Vec3),
        ("isCarInPitline",                 ctypes.c_int),
        ("isCarInPit",                     ctypes.c_int),
        ("carLeaderboardPosition",         ctypes.c_int),
        ("carRealTimeLeaderboardPosition", ctypes.c_int),
        ("spLineLength",                   ctypes.c_float),
        ("isConnected",                    ctypes.c_int),
        ("suspensionDamage",               ctypes.c_float * 4),
        ("engineLifeLeft",                 ctypes.c_float),
        ("tyreInflation",                  ctypes.c_float * 4),
    ]


class SPageFileCrewChief(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("numVehicles",               ctypes.c_int),
        ("focusVehicle",              ctypes.c_int),
        ("serverName",                ctypes.c_byte * 512),
        ("vehicle",                   AcsVehicleInfo * 64),
        ("acInstallPath",             ctypes.c_byte * 512),
        ("isInternalMemoryModuleLoaded", ctypes.c_int),
        ("pluginVersion",             ctypes.c_byte * 32),
    ]


# ── Lector SHM ────────────────────────────────────────────────────────────────

class ACSharedMemory:
    NAMES = {
        "physics":   "Local\\acpmf_physics",
        "graphics":  "Local\\acpmf_graphics",
        "static":    "Local\\acpmf_static",
        "crewchief": "Local\\acpmf_crewchief",
    }
    STRUCTS = {
        "physics":   SPageFilePhysics,
        "graphics":  SPageFileGraphic,
        "static":    SPageFileStatic,
        "crewchief": SPageFileCrewChief,
    }

    def __init__(self):
        self._handles = {}
        self._views   = {}
        self._structs = {}

    def connect(self) -> bool:
        try:
            for key, name in self.NAMES.items():
                h = _open_shm(name)
                if not h:
                    self.disconnect()
                    return False
                size  = ctypes.sizeof(self.STRUCTS[key])
                view  = _map_view(h, size)
                if not view:
                    _close(h, None)
                    self.disconnect()
                    return False
                self._handles[key] = h
                self._views[key]   = view
            return True
        except Exception as e:
            print(f"[SHM] connect error: {e}")
            self.disconnect()
            return False

    def read(self, key: str):
        view   = self._views.get(key)
        struct = self.STRUCTS[key]
        if not view:
            return None
        return struct.from_address(view)

    def disconnect(self):
        for key in list(self._handles):
            _close(self._handles.pop(key), self._views.pop(key, None))


# ── JSON serialiser ────────────────────────────────────────────────────────────

def _ansi_bytes(b) -> str:
    """Decode AcsVehicleInfo.driverName / .carModel — CharSet.Ansi (Windows-1252), null-terminated."""
    try:
        raw = bytes(b)
        end = raw.find(b"\x00")
        if end >= 0:
            raw = raw[:end]
        return raw.decode("cp1252", errors="replace")
    except Exception:
        return ""


def _build_fast(phy: SPageFilePhysics, grx: SPageFileGraphic, cc: SPageFileCrewChief) -> dict:
    n   = cc.numVehicles
    our = cc.vehicle[0]

    cars_data = []
    for i in range(1, min(n, 64)):
        v = cc.vehicle[i]
        if not v.isConnected:
            continue
        # normalise spline position (spLineLength is raw spline NOT normalised — use worldPos)
        cars_data.append({
            "idx":        i,
            "race_pos":   v.carLeaderboardPosition,
            "lap":        v.lapCount,
            "spline":     round(v.spLineLength, 6),   # NOTE: verify this is 0-1 normalised
            "speed":      round(v.speedMS * 3.6, 1),
            "last_lap":   v.lastLapTimeMS,
            "best_lap":   v.bestLapMS,
            "wx":         round(v.worldPosition.x, 2),
            "wz":         round(v.worldPosition.z, 2),
            "driver_name": _ansi_bytes(v.driverName),
            "in_pit":     bool(v.isCarInPit),
        })

    return {
        "t":           "fast",
        "speed":       round(phy.speedKmh, 1),
        "rpm":         phy.rpms,
        "gear":        phy.gear,
        "fuel":        round(phy.fuel, 2),
        "fuel_laps":   0.0,    # calculado en engineer.py con rolling average
        "lap":         grx.completedLaps,
        "lap_time_ms": grx.iCurrentTime,
        "best_lap_ms": grx.iBestTime,
        "last_lap_ms": grx.iLastTime,
        "position":    grx.position,
        "cars":        n,
        "spline":      round(grx.normalizedCarPosition, 6),
        "tyre_compound": grx.tyreCompound,
        "tyre_fl":     round(phy.tyreCoreTemp[0], 1),
        "tyre_fr":     round(phy.tyreCoreTemp[1], 1),
        "tyre_rl":     round(phy.tyreCoreTemp[2], 1),
        "tyre_rr":     round(phy.tyreCoreTemp[3], 1),
        "tyre_wear_fl": round(phy.tyreWear[0], 1),
        "tyre_wear_fr": round(phy.tyreWear[1], 1),
        "tyre_wear_rl": round(phy.tyreWear[2], 1),
        "tyre_wear_rr": round(phy.tyreWear[3], 1),
        "last_splits":  [],
        "best_splits":  [],
        "track_len":    0,
        "wx":   round(grx.carCoordinates[0], 2),
        "wz":   round(grx.carCoordinates[2], 2),
        "vx":   round(phy.localVelocity.x, 2),
        "vz":   round(phy.localVelocity.z, 2),
        "dmg_front":  round(phy.carDamage[0], 1),
        "dmg_rear":   round(phy.carDamage[1], 1),
        "dmg_left":   round(phy.carDamage[2], 1),
        "dmg_right":  round(phy.carDamage[3], 1),
        "dmg_centre": round(phy.carDamage[4], 1),
        "cars_data":  cars_data,
    }


def _build_slow(sta: SPageFileStatic, grx: SPageFileGraphic) -> dict:
    return {
        "t":            "slow",
        "track":        sta.track.strip(),
        "track_layout": sta.trackConfiguration.strip(),
        "car":          sta.carModel.strip(),
        "session":      grx.session,
        "time_left":    round(grx.sessionTimeLeft),
        "all_drivers":  {},   # rellena engineer.py desde cars_data del fast packet
    }


def _write_json(path: str, data: dict):
    tmp = path + ".tmp"
    txt = json.dumps(data)
    with open(tmp, "w") as f:
        f.write(txt)
    try:
        os.rename(tmp, path)
    except Exception:
        with open(path, "w") as f:
            f.write(txt)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    shm = ACSharedMemory()
    connected = False
    last_fast = 0.0
    last_slow = 0.0
    writes     = 0

    print("[SHM] Iniciando lector de shared memory de AC...")
    print(f"[SHM] Fast → {FAST_FILE}  |  Slow → {SLOW_FILE}")

    while True:
        if not connected:
            if shm.connect():
                connected = True
                print("[SHM] Conectado a AC shared memory OK")
            else:
                print("[SHM] AC no disponible — reintentando en 3s...")
                time.sleep(3)
                continue

        try:
            now = time.time()

            if now - last_fast >= 1.0 / FAST_HZ:
                last_fast = now
                phy = shm.read("physics")
                grx = shm.read("graphics")
                cc  = shm.read("crewchief")
                if phy and grx and cc:
                    _write_json(FAST_FILE, _build_fast(phy, grx, cc))
                    writes += 1
                    if writes % 50 == 0:
                        print(f"[SHM] {writes} writes — spd={phy.speedKmh:.0f} fuel={phy.fuel:.1f}")
                else:
                    raise RuntimeError("read devolvió None")

            if now - last_slow >= 1.0 / SLOW_HZ:
                last_slow = now
                sta = shm.read("static")
                grx = shm.read("graphics")
                if sta and grx:
                    _write_json(SLOW_FILE, _build_slow(sta, grx))

        except Exception as e:
            print(f"[SHM] Error leyendo: {e} — reconectando...")
            shm.disconnect()
            connected = False
            time.sleep(2)

        time.sleep(max(0, (1.0 / FAST_HZ) - (time.time() - last_fast)))


if __name__ == "__main__":
    main()
