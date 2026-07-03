/*
 * ac_shm_reader.c — AC Shared Memory → JSON writer
 *
 * Reads AC's Windows Named Shared Memory directly and writes telemetry
 * JSON files to Z:\tmp\ (= /tmp/ on Linux via Wine/Proton).
 *
 * Compile:
 *   x86_64-w64-mingw32-gcc -O2 -o ac_shm_reader.exe ac_shm_reader.c
 *
 * Run (same Proton prefix as AC):
 *   STEAM_COMPAT_CLIENT_INSTALL_PATH=~/.local/share/Steam \
 *   STEAM_COMPAT_DATA_PATH=~/.local/share/Steam/steamapps/compatdata/244210 \
 *   ~/.local/share/Steam/compatibilitytools.d/GE-Proton10-34/proton \
 *       waitforexitandrun ~/Games/ac-engineer/ac_shm_reader.exe
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

/* ── Output paths ────────────────────────────────────────────────────────── */
/* Written next to the exe (Z:\home\...\ac-engineer\) — always writable       */
#define FAST_HZ   10.0
#define SLOW_HZ   1.0

static char g_fast_file[MAX_PATH];
static char g_slow_file[MAX_PATH];

/* ── Structs — must match ACSData.cs exactly (Pack=4) ───────────────────── */
#pragma pack(push, 4)

typedef struct { float x, y, z; } Vec3;

typedef struct {
    int   packetId;
    float gas, brake, fuel;
    int   gear, rpms;
    float steerAngle, speedKmh;
    float velocity[3], accG[3];
    float wheelSlip[4], wheelLoad[4], wheelsPressure[4], wheelAngularSpeed[4];
    float tyreWear[4], tyreDirtyLevel[4], tyreCoreTemp[4], camberRAD[4];
    float suspensionTravel[4];
    float drs, tc, heading, pitch, roll, cgHeight;
    float carDamage[5];
    int   numberOfTyresOut, pitLimiterOn;
    float abs_, kersCharge, kersInput;
    int   autoShifterOn;
    float rideHeight[2], turboBoost, ballast, airDensity, airTemp, roadTemp;
    float localAngularVel[3], finalFF, performanceMeter;
    int   engineBrake, ersRecoveryLevel, ersPowerLevel, ersHeatCharging, ersIsCharging;
    float kersCurrentKJ;
    int   drsAvailable, drsEnabled;
    float brakeTemp[4], clutch;
    float tyreTempI[4], tyreTempM[4], tyreTempO[4];
    int   isAIControlled;
    Vec3  tyreContactPoint[4], tyreContactNormal[4], tyreContactHeading[4];
    float brakeBias;
    Vec3  localVelocity;
} SPhysics;

typedef struct {
    int     packetId, status, session;
    wchar_t currentTime[15], lastTime[15], bestTime[15], split[15];
    int     completedLaps, position, iCurrentTime, iLastTime, iBestTime;
    float   sessionTimeLeft, distanceTraveled;
    int     isInPit, currentSectorIndex, lastSectorTime, numberOfLaps;
    wchar_t tyreCompound[33];
    float   replayTimeMultiplier, normalizedCarPosition;
    float   carCoordinates[3];
    float   penaltyTime;
    int     flag, idealLineOn, isInPitLane;
    float   surfaceGrip;
    int     mandatoryPitDone;
} SGraphic;

typedef struct {
    wchar_t smVersion[15], acVersion[15];
    int     numberOfSessions, numCars;
    wchar_t carModel[33], track[33];
    wchar_t playerName[33], playerSurname[33], playerNick[33];
    int     sectorCount;
    float   maxTorque, maxPower;
    int     maxRpm;
    float   maxFuel;
    float   suspensionMaxTravel[4], tyreRadius[4];
    float   maxTurboBoost, deprecated_1, deprecated_2;
    int     penaltiesEnabled;
    float   aidFuelRate, aidTireRate, aidMechanicalDamage;
    int     aidAllowTyreBlankets;
    float   aidStability;
    int     aidAutoClutch, aidAutoBlip, hasDRS, hasERS, hasKERS;
    float   kersMaxJ;
    int     engineBrakeSettingsCount, ersPowerControllerCount;
    float   trackSPlineLength;
    wchar_t trackConfiguration[33];
    float   ersMaxJ;
    int     isTimedRace, hasExtraLap;
    wchar_t carSkin[33];
    int     reversedGridPositions, PitWindowStart, PitWindowEnd;
} SStatic;

typedef struct {
    int   carId;
    char  driverName[64];   /* ANSI/CP-1252, null-terminated */
    char  carModel[64];     /* ANSI/CP-1252, null-terminated */
    float speedMS;
    int   bestLapMS, lapCount, currentLapInvalid, currentLapTimeMS, lastLapTimeMS;
    Vec3  worldPosition;
    int   isCarInPitline, isCarInPit;
    int   carLeaderboardPosition, carRealTimeLeaderboardPosition;
    float spLineLength;
    int   isConnected;
    float suspensionDamage[4];
    float engineLifeLeft;
    float tyreInflation[4];
} AVehicleInfo;

typedef struct {
    int          numVehicles, focusVehicle;
    char         serverName[512];
    AVehicleInfo vehicle[64];
    char         acInstallPath[512];
    int          isInternalMemoryModuleLoaded;
    char         pluginVersion[32];
} SCrewChief;

#pragma pack(pop)

/* ── SHM handles ─────────────────────────────────────────────────────────── */
static HANDLE h_phy = NULL, h_grx = NULL, h_sta = NULL, h_cc = NULL;
static void  *v_phy = NULL, *v_grx = NULL, *v_sta = NULL, *v_cc = NULL;

static void close_shm(void) {
    if (v_phy) { UnmapViewOfFile(v_phy); v_phy = NULL; }
    if (v_grx) { UnmapViewOfFile(v_grx); v_grx = NULL; }
    if (v_sta) { UnmapViewOfFile(v_sta); v_sta = NULL; }
    if (v_cc)  { UnmapViewOfFile(v_cc);  v_cc  = NULL; }
    if (h_phy) { CloseHandle(h_phy); h_phy = NULL; }
    if (h_grx) { CloseHandle(h_grx); h_grx = NULL; }
    if (h_sta) { CloseHandle(h_sta); h_sta = NULL; }
    if (h_cc)  { CloseHandle(h_cc);  h_cc  = NULL; }
}

static int open_shm(void) {
    close_shm();
    h_phy = OpenFileMappingA(FILE_MAP_READ, FALSE, "Local\\acpmf_physics");
    h_grx = OpenFileMappingA(FILE_MAP_READ, FALSE, "Local\\acpmf_graphics");
    h_sta = OpenFileMappingA(FILE_MAP_READ, FALSE, "Local\\acpmf_static");
    /* acpmf_crewchief is optional — only exists when CrewChief plugin is active */
    h_cc  = OpenFileMappingA(FILE_MAP_READ, FALSE, "Local\\acpmf_crewchief");

    if (!h_phy || !h_grx || !h_sta) {
        DWORD err = GetLastError();
        printf("[SHM] OpenFileMapping failed error=%lu phy=%p grx=%p sta=%p\n",
               err, (void*)h_phy, (void*)h_grx, (void*)h_sta);
        fflush(stdout);
        close_shm(); return 0;
    }
    v_phy = MapViewOfFile(h_phy, FILE_MAP_READ, 0, 0, sizeof(SPhysics));
    v_grx = MapViewOfFile(h_grx, FILE_MAP_READ, 0, 0, sizeof(SGraphic));
    v_sta = MapViewOfFile(h_sta, FILE_MAP_READ, 0, 0, sizeof(SStatic));
    if (h_cc) v_cc = MapViewOfFile(h_cc, FILE_MAP_READ, 0, 0, sizeof(SCrewChief));
    if (!v_phy || !v_grx || !v_sta) { close_shm(); return 0; }
    printf("[SHM] crewchief SHM: %s\n", v_cc ? "OK" : "no disponible (sin datos multi-coche)");
    fflush(stdout);
    return 1;
}

/* ── JSON helpers ────────────────────────────────────────────────────────── */
static char g_buf[1 << 20];   /* 1 MB output buffer */
static int  g_pos;

static void jb(const char *s) {
    int n = strlen(s);
    memcpy(g_buf + g_pos, s, n);
    g_pos += n;
}

static void jf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    g_pos += vsprintf(g_buf + g_pos, fmt, ap);
    va_end(ap);
}

/* Write a JSON string with escaping */
static void js(const char *s) {
    g_buf[g_pos++] = '"';
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if      (c == '"')  { jb("\\\""); }
        else if (c == '\\') { jb("\\\\"); }
        else if (c == '\n') { jb("\\n");  }
        else if (c == '\r') { jb("\\r");  }
        else if (c == '\t') { jb("\\t");  }
        else if (c < 32)    { jf("\\u%04x", c); }
        else                { g_buf[g_pos++] = (char)c; }
    }
    g_buf[g_pos++] = '"';
}

/* Convert wchar_t → UTF-8 then write as JSON string */
static void jws(const wchar_t *ws) {
    char tmp[256] = {0};
    WideCharToMultiByte(CP_UTF8, 0, ws, -1, tmp, sizeof(tmp)-1, NULL, NULL);
    /* trim trailing spaces */
    int n = strlen(tmp);
    while (n > 0 && tmp[n-1] == ' ') tmp[--n] = 0;
    js(tmp);
}

static void jnull(void) { jb("null"); }

/* Atomic file write: buffer → .tmp → rename */
static void write_buf(const char *path) {
    char tmp[MAX_PATH];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) {
        printf("[SHM] ERROR write_buf fopen('%s') errno=%d\n", tmp, errno);
        fflush(stdout);
        return;
    }
    size_t written = fwrite(g_buf, 1, g_pos, f);
    fclose(f);
    if (written != (size_t)g_pos) {
        printf("[SHM] ERROR write_buf wrote %zu/%d bytes\n", written, g_pos);
        fflush(stdout);
        return;
    }
    DeleteFileA(path);
    if (!MoveFileA(tmp, path)) {
        printf("[SHM] ERROR MoveFileA err=%lu\n", GetLastError());
        fflush(stdout);
    }
}

/* ── Fast packet ─────────────────────────────────────────────────────────── */
static void write_fast(void) {
    SPhysics   *phy = (SPhysics *)v_phy;
    SGraphic   *grx = (SGraphic *)v_grx;
    SStatic    *sta = (SStatic *)v_sta;
    SCrewChief *cc  = v_cc ? (SCrewChief *)v_cc : NULL;
    int n = cc ? cc->numVehicles : 1;

    g_pos = 0;
    jb("{");
    jb("\"t\":\"fast\",");
    /* status: 0=OFF 1=REPLAY 2=LIVE 3=PAUSE — engineer.py silencia si != 2 */
    jf("\"status\":%d,",  grx->status);
    jf("\"speed\":%.1f,", phy->speedKmh);
    jf("\"rpm\":%d,",     phy->rpms);
    jf("\"gear\":%d,",    phy->gear);
    jf("\"fuel\":%.2f,",  phy->fuel);
    jb("\"fuel_laps\":0.0,");
    jf("\"lap\":%d,",     grx->completedLaps);
    jf("\"lap_time_ms\":%d,", grx->iCurrentTime);
    jf("\"best_lap_ms\":%d,", grx->iBestTime);
    jf("\"last_lap_ms\":%d,", grx->iLastTime);
    jf("\"position\":%d,",    grx->position);
    jf("\"cars\":%d,",        n);
    jf("\"spline\":%.6f,",    grx->normalizedCarPosition);
    jb("\"tyre_compound\":");  jws(grx->tyreCompound); jb(",");
    jf("\"tyre_fl\":%.1f,",   phy->tyreCoreTemp[0]);
    jf("\"tyre_fr\":%.1f,",   phy->tyreCoreTemp[1]);
    jf("\"tyre_rl\":%.1f,",   phy->tyreCoreTemp[2]);
    jf("\"tyre_rr\":%.1f,",   phy->tyreCoreTemp[3]);
    jf("\"tyre_wear_fl\":%.1f,", phy->tyreWear[0]);
    jf("\"tyre_wear_fr\":%.1f,", phy->tyreWear[1]);
    jf("\"tyre_wear_rl\":%.1f,", phy->tyreWear[2]);
    jf("\"tyre_wear_rr\":%.1f,", phy->tyreWear[3]);
    jb("\"last_splits\":[],\"best_splits\":[],");
    jf("\"track_len\":%.1f,", sta->trackSPlineLength);
    jf("\"sector_index\":%d,",   grx->currentSectorIndex);
    jf("\"last_sector_ms\":%d,", grx->lastSectorTime);
    jf("\"in_pitlane\":%s,",     grx->isInPitLane ? "true" : "false");
    jf("\"wx\":%.2f,", grx->carCoordinates[0]);
    jf("\"wz\":%.2f,", grx->carCoordinates[2]);
    jf("\"vx\":%.2f,", phy->localVelocity.x);
    jf("\"vz\":%.2f,", phy->localVelocity.z);
    jf("\"dmg_front\":%.1f,",  phy->carDamage[0]);
    jf("\"dmg_rear\":%.1f,",   phy->carDamage[1]);
    jf("\"dmg_left\":%.1f,",   phy->carDamage[2]);
    jf("\"dmg_right\":%.1f,",  phy->carDamage[3]);
    jf("\"dmg_centre\":%.1f,", phy->carDamage[4]);

    /* Nearby cars — only if crewchief SHM is available */
    jb("\"cars_data\":[");
    if (cc) {
        int first = 1;
        for (int i = 1; i < n && i < 64; i++) {
            AVehicleInfo *v = &cc->vehicle[i];
            if (!v->isConnected) continue;
            if (!first) jb(",");
            first = 0;
            jb("{");
            jf("\"idx\":%d,", i);
            jf("\"race_pos\":%d,", v->carLeaderboardPosition);
            jf("\"lap\":%d,",      v->lapCount);
            jf("\"spline\":%.6f,", v->spLineLength);
            jf("\"speed\":%.1f,",  v->speedMS * 3.6f);
            jf("\"last_lap\":%d,", v->lastLapTimeMS);
            jf("\"best_lap\":%d,", v->bestLapMS);
            jf("\"wx\":%.2f,",     v->worldPosition.x);
            jf("\"wz\":%.2f,",     v->worldPosition.z);
            jb("\"driver_name\":"); js(v->driverName); jb(",");
            jf("\"real_pos\":%d,", v->carRealTimeLeaderboardPosition);
            jf("\"in_pitlane\":%s,", v->isCarInPitline ? "true" : "false");
            jf("\"in_pit\":%s",    v->isCarInPit ? "true" : "false");
            jb("}");
        }
    }
    jb("]}");

    write_buf(g_fast_file);
}

/* ── Slow packet ─────────────────────────────────────────────────────────── */
static void write_slow(void) {
    SStatic    *sta = (SStatic *)v_sta;
    SGraphic   *grx = (SGraphic *)v_grx;
    SCrewChief *cc  = v_cc ? (SCrewChief *)v_cc : NULL;

    g_pos = 0;
    jb("{");
    jb("\"t\":\"slow\",");
    jb("\"track\":"); jws(sta->track); jb(",");
    jb("\"track_layout\":"); jws(sta->trackConfiguration); jb(",");
    jb("\"car\":"); jws(sta->carModel); jb(",");
    jf("\"session\":%d,",    grx->session);
    jf("\"time_left\":%.0f,", grx->sessionTimeLeft);
    /* all_drivers: nombre/coche por posición (incluye al jugador, idx 0) */
    jb("\"all_drivers\":{");
    if (cc) {
        int first = 1;
        int n = cc->numVehicles < 64 ? cc->numVehicles : 64;
        for (int i = 0; i < n; i++) {
            AVehicleInfo *v = &cc->vehicle[i];
            if (!v->isConnected) continue;
            if (!first) jb(",");
            first = 0;
            jf("\"%d\":{", i);
            jf("\"pos\":%d,", v->carLeaderboardPosition);
            jb("\"name\":"); js(v->driverName); jb(",");
            jb("\"car\":"); js(v->carModel);
            jb("}");
        }
    }
    jb("}}");

    write_buf(g_slow_file);
}

/* ── Main loop ───────────────────────────────────────────────────────────── */
int main(void) {
    int connected = 0;
    int writes    = 0;
    DWORD last_fast = 0, last_slow = 0;
    DWORD fast_ms = (DWORD)(1000.0 / FAST_HZ);
    DWORD slow_ms = (DWORD)(1000.0 / SLOW_HZ);

    /* Build output paths next to the exe — always writable from Wine */
    {
        char exe_path[MAX_PATH] = {0};
        GetModuleFileNameA(NULL, exe_path, MAX_PATH);
        char *sep = strrchr(exe_path, '\\');
        if (!sep) sep = strrchr(exe_path, '/');
        if (sep) *sep = 0;
        snprintf(g_fast_file, MAX_PATH, "%s\\ac_telemetry_fast.json", exe_path);
        snprintf(g_slow_file, MAX_PATH, "%s\\ac_telemetry_slow.json", exe_path);
    }

    printf("[SHM] AC Shared Memory reader iniciando...\n");
    printf("[SHM] Fast -> %s\n", g_fast_file);
    printf("[SHM] Slow -> %s\n", g_slow_file);
    fflush(stdout);

    while (1) {
        if (!connected) {
            if (open_shm()) {
                connected = 1;
                printf("[SHM] Conectado a AC shared memory OK\n");
                fflush(stdout);
            } else {
                printf("[SHM] AC no disponible — reintentando en 3s...\n");
                fflush(stdout);
                Sleep(3000);
                continue;
            }
        }

        DWORD now = GetTickCount();

        if ((now - last_fast) >= fast_ms) {
            last_fast = now;
            /* Only phy + grx are required; cc is optional */
            if (!v_phy || !v_grx) {
                close_shm(); connected = 0; Sleep(2000); continue;
            }
            write_fast();
            writes++;
            if (writes % 50 == 0) {
                SPhysics *phy = (SPhysics *)v_phy;
                printf("[SHM] %d writes — spd=%.0f fuel=%.1f\n",
                       writes, phy->speedKmh, phy->fuel);
                fflush(stdout);
            }
        }

        if ((now - last_slow) >= slow_ms) {
            last_slow = now;
            if (v_sta && v_grx) write_slow();
        }

        Sleep(fast_ms / 2);
    }

    return 0;
}
