#!/usr/bin/env python3
"""
Victor — AI Race Engineer
Un clic = todo arranca. Espera automáticamente a que Assetto Corsa esté activo.
"""

import os
import sys
import subprocess
import threading
import time
from pathlib import Path

try:
    import pystray
    from pystray import MenuItem as item, Menu
    from PIL import Image, ImageDraw
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "pillow", "-q"])
    import pystray
    from pystray import MenuItem as item, Menu
    from PIL import Image, ImageDraw

ENGINEER_DIR = Path(__file__).parent
PID_FILE     = Path("/tmp/ac-engineer.pid")
LOG_FILE     = Path("/tmp/ac-engineer.log")
ICON_64      = ENGINEER_DIR / "victor_icon_64.png"

# ── estado ───────────────────────────────────────────────────────────────────────

def _is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def _is_ac_connected() -> bool:
    """True si el log muestra que AC está enviando datos (modificado en los últimos 15s).

    Lee solo el final del archivo (seek desde el final) en vez de cargarlo
    entero — engineer.py corre horas seguidas durante una sesión y este
    chequeo se llama cada 4s, así que un read_text() completo se vuelve más
    caro cuanto más dura la sesión. Los strings buscados también estaban
    desactualizados ("[UDP] ...") — el log real dice "[Telem] AC conectado
    via {fuente}." / "[Telem] Sin señal AC (timeout)...", así que esta
    función nunca detectaba una conexión real.
    """
    try:
        if not LOG_FILE.exists():
            return False
        if time.time() - LOG_FILE.stat().st_mtime > 20:
            return False
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 3000))
            tail = f.read().decode("utf-8", errors="ignore")
        return ("[Telem] AC conectado" in tail
                and "[Telem] Sin señal" not in tail.split("[Telem] AC conectado")[-1])
    except Exception:
        return False

# ── icono ────────────────────────────────────────────────────────────────────────

def _make_icon(ac_connected: bool) -> Image.Image:
    try:
        img = Image.open(ICON_64).convert("RGBA")
    except Exception:
        img = Image.new("RGBA", (64, 64), (30, 30, 30, 255))

    # Indicador de conexión AC: círculo en esquina inferior derecha
    draw = ImageDraw.Draw(img)
    dot  = (0, 210, 80, 255) if ac_connected else (180, 180, 180, 200)
    draw.ellipse([47, 47, 62, 62], fill=dot, outline=(0, 0, 0, 180))
    return img

# ── daemon ───────────────────────────────────────────────────────────────────────

def _start_daemon():
    if _is_running():
        return
    log = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, str(ENGINEER_DIR / "engineer.py")],
        stdout=log, stderr=log,
    )
    PID_FILE.write_text(str(proc.pid))

def _stop_daemon():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 15)   # SIGTERM
        except Exception:
            pass
        PID_FILE.unlink(missing_ok=True)

# ── acciones de menú ─────────────────────────────────────────────────────────────

def action_open_logs(icon, _item):
    for term in [
        ["konsole", "--noclose", "-e", f"tail -f {LOG_FILE}"],
        ["xterm", "-e", f"tail -f {LOG_FILE}"],
        ["gnome-terminal", "--", "bash", "-c", f"tail -f {LOG_FILE}; read"],
    ]:
        try:
            subprocess.Popen(term)
            return
        except FileNotFoundError:
            continue

def action_stop(icon, _item):
    _stop_daemon()
    _refresh(icon)

def action_restart(icon, _item):
    _stop_daemon()
    time.sleep(0.8)
    _start_daemon()
    time.sleep(1)
    _refresh(icon)

def action_quit(icon, _item):
    _stop_daemon()
    icon.stop()

# ── refresco ─────────────────────────────────────────────────────────────────────

def _build_menu(ac_connected: bool) -> Menu:
    ac_label = "● AC conectado — listo" if ac_connected else "○ Esperando Assetto Corsa..."
    return Menu(
        item(ac_label,          lambda *_: None,  enabled=False),
        Menu.SEPARATOR,
        item("⟳  Reiniciar Victor",  action_restart),
        item("■  Detener Victor",    action_stop),
        Menu.SEPARATOR,
        item("📋  Ver logs",          action_open_logs),
        Menu.SEPARATOR,
        item("✕  Salir",             action_quit),
    )

def _refresh(icon):
    connected  = _is_ac_connected()
    icon.icon  = _make_icon(connected)
    icon.title = "Victor | AC listo" if connected else "Victor | Esperando AC..."
    icon.menu  = _build_menu(connected)

def _monitor_loop(icon):
    """Monitoreo cada 4s: refresca icono y relanza daemon si murió inesperadamente."""
    while True:
        time.sleep(4)
        try:
            if not _is_running():
                # Relanza si no fue detenido voluntariamente (PID file ausente = parada manual)
                if PID_FILE.exists():
                    _start_daemon()
            _refresh(icon)
        except Exception:
            pass

# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    # Arranque automático del daemon
    _start_daemon()

    icon = pystray.Icon(
        name  = "victor",
        icon  = _make_icon(False),
        title = "Victor | Iniciando...",
        menu  = _build_menu(False),
    )

    threading.Thread(target=_monitor_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
