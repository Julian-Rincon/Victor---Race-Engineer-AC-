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
CONFIG_INFO="$AC_COMPAT_DATA/config_info"

_resolve_proton_dir() {
  local proton_version=""
  if [[ -f "$CONFIG_INFO" ]]; then
    proton_version="$(head -1 "$CONFIG_INFO")"
  fi

  if [[ -n "$proton_version" && -x "$STEAM_ROOT/compatibilitytools.d/$proton_version/proton" ]]; then
    printf '%s\n' "$STEAM_ROOT/compatibilitytools.d/$proton_version"
  elif [[ -x "$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34/proton" ]]; then
    printf '%s\n' "$STEAM_ROOT/compatibilitytools.d/GE-Proton10-34"
  elif [[ -x "$STEAM_ROOT/compatibilitytools.d/GE-Proton9-20/proton" ]]; then
    printf '%s\n' "$STEAM_ROOT/compatibilitytools.d/GE-Proton9-20"
  else
    return 1
  fi
}

PROTON_DIR="$(_resolve_proton_dir || true)"
AC_PROTON="${PROTON_DIR:+$PROTON_DIR/proton}"

_spawn_detached() {
  # --fork fuerza que setsid siempre bifurque un hijo nuevo. Sin esto, si
  # quien invoca este script tiene control de trabajos activo (`set -m`,
  # ej. una shell interactiva), setsid bifurca IGUAL por su cuenta pero el
  # PID que queda en "$!" es el del proceso padre que termina casi al
  # instante — el proceso real sigue vivo con un PID nunca registrado en el
  # PID file, y _stop_tracked no lo puede matar ni encontrar (kill -0
  # falla, y como el PID file SÍ existe tampoco cae al fallback por
  # nombre). --fork deja el comportamiento igual sin importar el contexto
  # de quien llama.
  if command -v setsid >/dev/null 2>&1; then
    exec setsid --fork "$@"
  else
    exec nohup "$@"
  fi
}

# Mata por PID file cuando existe (solo la instancia que ESTE script lanzó);
# recién si no hay PID file cae a pkill por nombre (huérfanos de antes de que
# existiera este tracking, o crashes que no llegaron a escribirlo). Sin esto,
# "stop" mataba por patrón de nombre sin importar de qué lanzamiento venía el
# proceso — con dos instancias de Content Manager corriendo a la vez (ej. Steam
# + una sesión de prueba vieja sin cerrar), cerrar una se llevaba puesto el
# daemon de Victor de la OTRA.
_stop_tracked() {
  local pidfile="$1" label="$2" pattern="$3"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null && echo "[Victor] $label detenido (PID $pid)." || true
    fi
    rm -f "$pidfile"
  elif pkill -f "$pattern" 2>/dev/null; then
    echo "[Victor] $label detenido (sin PID file — huérfano de una instancia anterior)."
  fi
}

case "${1:-tray}" in
  tray)
    echo "[Victor] Iniciando bandeja del sistema..."
    nohup python3 "$ENGINEER_DIR/victor_tray.py" \
      > "/tmp/ac-engineer-tray.log" 2>&1 &
    echo $! > "$TRAY_PID_FILE"
    echo "[Victor] Tray PID $! — icono en la bandeja del sistema"
    ;;

  start)
    # Detiene solo LA instancia que este script lanzó antes (por PID file) —
    # no cualquier proceso con ese nombre en el sistema (ver _stop_tracked).
    _stop_tracked "$PID_FILE" "Daemon anterior" "engineer\.py"
    _stop_tracked "$SHM_PID_FILE" "SHM reader anterior" "ac_shm_reader\.exe"
    sleep 0.5
    rm -f "$ENGINEER_DIR"/ac_telemetry_fast.json \
          "$ENGINEER_DIR"/ac_telemetry_slow.json \
          "$ENGINEER_DIR"/ac_telemetry_fast.json.tmp \
          "$ENGINEER_DIR"/ac_telemetry_slow.json.tmp

    # Lanzar SHM reader como exe Windows via Wine (mismo prefix que AC)
    echo "[Victor] Iniciando SHM reader (ac_shm_reader.exe via Wine)..."
    AC_WINE="${PROTON_DIR:+$PROTON_DIR/files/bin/wine}"
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
      WINEDLLPATH="$PROTON_DIR/files/lib/vkd3d:$PROTON_DIR/files/lib/wine" \
      LD_LIBRARY_PATH="$PROTON_DIR/files/lib" \
      _spawn_detached "$AC_WINE" "$ENGINEER_DIR/ac_shm_reader.exe" \
        > "/tmp/ac-engineer-shm.log" 2>&1 &
      echo $! > "$SHM_PID_FILE"
      echo "[Victor] SHM reader PID $! — log en /tmp/ac-engineer-shm.log"
    else
      echo "[Victor] WARN: Wine de Proton o ac_shm_reader.exe no encontrado"
    fi

    echo "[Victor] Iniciando AI Race Engineer daemon..."
    _spawn_detached python3 -u "$ENGINEER_DIR/engineer.py" \
      > "/tmp/ac-engineer.log" 2>/tmp/ac-engineer-alsa.log &
    echo $! > "$PID_FILE"
    echo "[Victor] Daemon PID $! — log en /tmp/ac-engineer.log"
    ;;

  stop)
    _stop_tracked "$PID_FILE" "Daemon" "engineer\.py"
    _stop_tracked "$SHM_PID_FILE" "SHM reader" "ac_shm_reader\.exe"
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
