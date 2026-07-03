/*
 * shm_simulator.c — Simula las páginas de shared memory de Assetto Corsa
 * (acpmf_physics / graphics / static / crewchief) para probar ac_shm_reader.exe
 * SIN abrir el juego.
 *
 * Los valores se escriben a OFFSETS DE BYTE calculados a mano desde el
 * marshaling C# de CrewChief (ACSData.cs, Pack=4, CharSet.Unicode) — NO desde
 * los structs del lector. Así, si el lector tiene el layout desalineado, el
 * test del pipeline lo detecta en vez de auto-validarse.
 *
 * Compilar:  x86_64-w64-mingw32-gcc -O2 -o shm_simulator.exe shm_simulator.c
 * Ejecutar:  con el mismo wine/prefix que AC (ver test_shm_pipeline.py)
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>

/* ── Tamaños de página (derivados del C#) ────────────────────────────────── */
/* physics: tyreContact{Point,Normal,Heading} son acsVec3[4] = 48 bytes c/u →
   brakeBias@564, localVelocity@568, total 580 */
#define PHYSICS_SIZE    580
#define GRAPHICS_SIZE   288
#define STATIC_SIZE     684
#define CC_VEH_STRIDE   228
#define CC_VEH_BASE     520          /* numVehicles(4)+focusVehicle(4)+serverName(512) */
#define CREWCHIEF_SIZE  (CC_VEH_BASE + 64*CC_VEH_STRIDE + 512 + 4 + 32)   /* 15660 */

/* ── Helpers de escritura cruda ──────────────────────────────────────────── */
static void w_i32(uint8_t *b, int off, int32_t v)  { memcpy(b + off, &v, 4); }
static void w_f32(uint8_t *b, int off, float v)    { memcpy(b + off, &v, 4); }
/* String UTF-16LE (ASCII → wchar de 2 bytes, con terminador) */
static void w_wstr(uint8_t *b, int off, const char *s, int max_chars) {
    for (int i = 0; i < max_chars; i++) {
        uint16_t c = (i < (int)strlen(s)) ? (uint16_t)s[i] : 0;
        memcpy(b + off + i*2, &c, 2);
    }
}
static void w_str(uint8_t *b, int off, const char *s, int max_bytes) {
    strncpy((char *)(b + off), s, max_bytes - 1);
}

static void *make_page(const char *name, int size, uint8_t **out) {
    HANDLE h = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                                  0, size, name);
    if (!h) { printf("[SIM] ERROR CreateFileMapping %s err=%lu\n", name, GetLastError()); return NULL; }
    void *v = MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, size);
    if (!v) { printf("[SIM] ERROR MapViewOfFile %s err=%lu\n", name, GetLastError()); return NULL; }
    memset(v, 0, size);
    *out = (uint8_t *)v;
    return v;
}

/* ── Un vehículo del page crewchief ──────────────────────────────────────── */
static void write_vehicle(uint8_t *b, int idx, int carId, const char *name,
                          const char *model, float speedMS, int bestMS,
                          int lapCount, int curMS, int lastMS,
                          float wx, float wy, float wz,
                          int inPitline, int inPit, int lbPos, int rtPos,
                          float spline, int connected) {
    int o = CC_VEH_BASE + idx * CC_VEH_STRIDE;
    w_i32(b, o + 0,   carId);
    w_str(b, o + 4,   name,  64);
    w_str(b, o + 68,  model, 64);
    w_f32(b, o + 132, speedMS);
    w_i32(b, o + 136, bestMS);
    w_i32(b, o + 140, lapCount);
    w_i32(b, o + 144, 0);            /* currentLapInvalid */
    w_i32(b, o + 148, curMS);
    w_i32(b, o + 152, lastMS);
    w_f32(b, o + 156, wx); w_f32(b, o + 160, wy); w_f32(b, o + 164, wz);
    w_i32(b, o + 168, inPitline);
    w_i32(b, o + 172, inPit);
    w_i32(b, o + 176, lbPos);
    w_i32(b, o + 180, rtPos);
    w_f32(b, o + 184, spline);
    w_i32(b, o + 188, connected);
}

int main(void) {
    uint8_t *phy, *grx, *sta, *cc;
    if (!make_page("Local\\acpmf_physics",   PHYSICS_SIZE,   &phy)) return 1;
    if (!make_page("Local\\acpmf_graphics",  GRAPHICS_SIZE,  &grx)) return 1;
    if (!make_page("Local\\acpmf_static",    STATIC_SIZE,    &sta)) return 1;
    if (!make_page("Local\\acpmf_crewchief", CREWCHIEF_SIZE, &cc))  return 1;

    /* ── Physics (offsets C#: Pack=4) ────────────────────────────────────── */
    w_i32(phy, 0,   42);        /* packetId */
    w_f32(phy, 12,  33.50f);    /* fuel */
    w_i32(phy, 16,  4);         /* gear */
    w_i32(phy, 20,  5500);      /* rpms */
    w_f32(phy, 28,  123.4f);    /* speedKmh */
    /* tyreWear[4] @120 */
    w_f32(phy, 120, 94.5f); w_f32(phy, 124, 93.2f);
    w_f32(phy, 128, 95.1f); w_f32(phy, 132, 96.0f);
    /* tyreCoreTemperature[4] @152 */
    w_f32(phy, 152, 82.1f); w_f32(phy, 156, 84.3f);
    w_f32(phy, 160, 79.8f); w_f32(phy, 164, 80.5f);
    /* carDamage[5] @224 */
    w_f32(phy, 224, 10.0f); w_f32(phy, 228, 0.0f); w_f32(phy, 232, 5.0f);
    w_f32(phy, 236, 0.0f);  w_f32(phy, 240, 2.0f);
    /* localVelocity @568 (tras contactPoint@420+48, normal@468+48, heading@516+48, brakeBias@564) */
    w_f32(phy, 568, 1.5f); w_f32(phy, 572, 0.0f); w_f32(phy, 576, 34.2f);

    /* ── Graphics ────────────────────────────────────────────────────────── */
    w_i32(grx, 0,   42);        /* packetId */
    w_i32(grx, 4,   2);         /* status = AC_LIVE */
    w_i32(grx, 8,   2);         /* session = RACE */
    w_i32(grx, 132, 5);         /* completedLaps */
    w_i32(grx, 136, 3);         /* position */
    w_i32(grx, 140, 61234);     /* iCurrentTime */
    w_i32(grx, 144, 95321);     /* iLastTime */
    w_i32(grx, 148, 94500);     /* iBestTime */
    w_f32(grx, 152, 1800.0f);   /* sessionTimeLeft */
    w_i32(grx, 160, 0);         /* isInPit */
    w_i32(grx, 164, 1);         /* currentSectorIndex */
    w_i32(grx, 168, 30500);     /* lastSectorTime */
    w_wstr(grx, 176, "Soft (S)", 33);   /* tyreCompound */
    w_f32(grx, 248, 0.421337f); /* normalizedCarPosition */
    w_f32(grx, 252, 100.5f); w_f32(grx, 256, 12.0f); w_f32(grx, 260, -200.25f);
    w_i32(grx, 276, 0);         /* isInPitLane */

    /* ── Static ──────────────────────────────────────────────────────────── */
    w_wstr(sta, 0,   "1.7", 15);           /* smVersion */
    w_wstr(sta, 30,  "1.16.4", 15);        /* acVersion */
    w_i32(sta, 60,  1);                    /* numberOfSessions */
    w_i32(sta, 64,  3);                    /* numCars */
    w_wstr(sta, 68,  "test_car_gt3", 33);  /* carModel */
    w_wstr(sta, 134, "ks_test_track", 33); /* track */
    w_i32(sta, 400, 3);                    /* sectorCount */
    w_f32(sta, 520, 4318.0f);              /* trackSPlineLength */
    w_wstr(sta, 524, "layout_a", 33);      /* trackConfiguration */

    /* ── CrewChief (oponentes) ───────────────────────────────────────────── */
    w_i32(cc, 0, 3);   /* numVehicles */
    w_i32(cc, 4, 0);   /* focusVehicle */
    w_str(cc, 8, "victor_sim_server", 512);
    /*            idx id  name            model           m/s   best   lap curMS  last    wx     wy    wz   pl pit lb rt  spline    conn */
    write_vehicle(cc, 0, 0, "Julian Rincon", "test_car_gt3", 34.3, 94500, 5, 61234, 95321, 100.5, 12.0, -200.25, 0, 0, 3, 3, 0.421337f, 1);
    write_vehicle(cc, 1, 1, "Ayrton Senna",  "test_car_gt3", 34.5, 94100, 5, 61000, 94800, 104.5, 12.0, -198.00, 0, 0, 2, 2, 0.423000f, 1);
    write_vehicle(cc, 2, 2, "Alain Prost",   "test_car_gt3", 33.0, 93900, 5, 60500, 94200, 150.0, 12.0, -150.00, 0, 0, 1, 1, 0.440000f, 1);

    printf("[SIM] Páginas SHM creadas (phy=%d grx=%d sta=%d cc=%d bytes). Manteniendo vivas...\n",
           PHYSICS_SIZE, GRAPHICS_SIZE, STATIC_SIZE, CREWCHIEF_SIZE);
    fflush(stdout);

    /* Mantener vivas las páginas; packetId incrementa para parecer vivo */
    int32_t pkt = 42;
    while (1) {
        Sleep(500);
        pkt++;
        w_i32(phy, 0, pkt);
        w_i32(grx, 0, pkt);
    }
    return 0;
}
