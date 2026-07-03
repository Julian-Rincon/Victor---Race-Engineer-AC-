#!/usr/bin/env bash
# Standalone launcher for AC Race Engineer (for testing without CM)
set -euo pipefail

ENGINEER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="/tmp/ac-engineer.pid"
SHM_PID_FILE="/tmp/ac-engineer-shm.pid"
TRAY_PID_FILE="/tmp/ac-engineer-tray.pid"
# Proton/Steam paths for AC (Steam AppID 244210)
STEAM_ROOT="$HOME/.local/share/Steam"
AC_COMPAT_DATA="$STEAM_ROOT/steamapps/compatdata/244210"
AC_PROTON="$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/proton"

case "${1:-tray}" in
  tray)
    echo "[Victor] Iniciando bandeja del sistema..."
    nohup python3 "$ENGINEER_DIR/victor_tray.py" \
      > "/tmp/ac-engineer-tray.log" 2>&1 &
    echo $! > "$TRAY_PID_FILE"
    echo "[Victor] Tray PID $! — icono en la bandeja del sistema"
    ;;

  start)
    # Kill any lingering instances (from tray, CM hook, or previous sessions)
    pkill -f "engineer\.py" 2>/dev/null && echo "[Victor] Deteniendo instancias anteriores..." || true
    sleep 0.5

    # Lanzar SHM reader como exe Windows via Wine (mismo prefix que AC)
    echo "[Victor] Iniciando SHM reader (ac_shm_reader.exe via Wine)..."
    AC_WINE="$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/files/bin/wine"
    AC_WINEPREFIX="$AC_COMPAT_DATA/pfx"
    XAUTH_FILE=$(ls /run/user/$(id -u)/xauth_* 2>/dev/null | head -1)
    if [[ -f "$AC_WINE" && -f "$ENGINEER_DIR/ac_shm_reader.exe" ]]; then
      WINEPREFIX="$AC_WINEPREFIX/" \
      WINEDEBUG=-all \
      WINENTSYNC=1 \
      WINEESYNC=1 \
      DISPLAY=:0 \
      WAYLAND_DISPLAY=wayland-0 \
      XDG_RUNTIME_DIR="/run/user/$(id -u)" \
      XAUTHORITY="${XAUTH_FILE:-/run/user/$(id -u)/.Xauthority}" \
      WINEDLLPATH="$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/files/lib/vkd3d:$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/files/lib/wine" \
      LD_LIBRARY_PATH="$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/files/lib" \
      nohup "$AC_WINE" "$ENGINEER_DIR/ac_shm_reader.exe" \
        > "/tmp/ac-engineer-shm.log" 2>&1 &
      echo $! > "$SHM_PID_FILE"
      echo "[Victor] SHM reader PID $! — log en /tmp/ac-engineer-shm.log"
    else
      echo "[Victor] WARN: Wine o ac_shm_reader.exe no encontrado"
    fi

    echo "[Victor] Iniciando AI Race Engineer daemon..."
    nohup python3 -u "$ENGINEER_DIR/engineer.py" \
      > "/tmp/ac-engineer.log" 2>/tmp/ac-engineer-alsa.log &
    echo $! > "$PID_FILE"
    echo "[Victor] Daemon PID $! — log en /tmp/ac-engineer.log"
    ;;

  stop)
    pkill -f "engineer\.py"     2>/dev/null && echo "[Victor] Daemon(s) detenido(s)." || echo "[Victor] Daemon no encontrado."
    pkill -f "ac_shm_reader\.exe" 2>/dev/null && echo "[Victor] SHM reader detenido."  || true
    rm -f "$PID_FILE" "$SHM_PID_FILE"
    ;;

  stop-tray)
    if [[ -f "$TRAY_PID_FILE" ]]; then
      PID=$(cat "$TRAY_PID_FILE")
      kill "$PID" 2>/dev/null && echo "[Victor] Tray detenido." || echo "[Victor] Tray ya terminado."
      rm -f "$TRAY_PID_FILE"
    fi
    ;;

  logs)
    tail -f /tmp/ac-engineer.log
    ;;

  shm-log)
    tail -f /tmp/ac-engineer-shm.log
    ;;

  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Daemon:  Corriendo (PID $(cat "$PID_FILE"))"
    else
      echo "Daemon:  Detenido"
    fi
    if [[ -f "$SHM_PID_FILE" ]] && kill -0 "$(cat "$SHM_PID_FILE")" 2>/dev/null; then
      echo "SHM:     Corriendo (PID $(cat "$SHM_PID_FILE"))"
    else
      echo "SHM:     Detenido"
    fi
    if [[ -f "$TRAY_PID_FILE" ]] && kill -0 "$(cat "$TRAY_PID_FILE")" 2>/dev/null; then
      echo "Tray:   Corriendo (PID $(cat "$TRAY_PID_FILE"))"
    else
      echo "Tray:   Detenido"
    fi
    ;;

  *)
    echo "Uso: $0 {tray|start|stop|stop-tray|logs|status}"
    echo "  tray      — Lanza la app en bandeja del sistema (recomendado)"
    echo "  start     — Solo el daemon (sin GUI)"
    echo "  stop      — Detiene el daemon"
    echo "  stop-tray — Detiene la bandeja"
    echo "  logs      — Ver logs en tiempo real"
    echo "  status    — Estado actual"
    exit 1
    ;;
esac
