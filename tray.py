"""Niania.exe – serwer niani w tle z ikoną w zasobniku systemowym (obok zegara)."""
import ctypes
import os
import sys
import threading
import time
import webbrowser
import winreg

import niania.server as srv

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Niania"


def redirect_output():
    """Aplikacja okienkowa nie ma konsoli – komunikaty idą do pliku niania.log."""
    srv.HOME.mkdir(parents=True, exist_ok=True)
    log_path = srv.HOME / "niania.log"
    if log_path.exists() and log_path.stat().st_size > 2_000_000:
        log_path.replace(srv.HOME / "niania.old.log")
    log = open(log_path, "a", encoding="utf-8", buffering=1)
    log.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    sys.stdout = sys.stderr = log


def already_running():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\NianiaSingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS


def make_icon(status):
    """Księżyc na granatowym tle + kropka stanu: zielona (czuwa), szara (wyłączone), czerwona (problem)."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(27, 38, 66, 255))
    d.ellipse((14, 12, 50, 48), fill=(250, 214, 120, 255))
    d.ellipse((24, 6, 58, 40), fill=(27, 38, 66, 255))
    dot = {"ok": (63, 185, 80), "off": (139, 148, 158), "bad": (248, 81, 73)}[status]
    d.ellipse((40, 40, 62, 62), fill=(*dot, 255), outline=(13, 17, 23, 255), width=3)
    return img


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def main():
    frozen = getattr(sys, "frozen", False)
    if frozen:
        redirect_output()
    cfg = srv.load_config()
    port = cfg["port"]
    local_url = f"http://localhost:{port}/"

    if already_running():
        webbrowser.open(local_url)
        return

    import pystray
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(srv.app, host="0.0.0.0", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()
    for _ in range(300):   # start kamerki i modelu trwa kilka sekund
        if server.started or not thread.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        ctypes.windll.user32.MessageBoxW(
            None, f"Nie udało się uruchomić niani na porcie {port}.\n"
                  f"Może działa już w innym oknie (start.bat)?\n\nSzczegóły: {srv.HOME / 'niania.log'}",
            "Niania", 0x10)
        os._exit(1)

    def monitor():
        return srv.monitor

    def toggle_armed(icon, item):
        m = monitor()
        m.cfg["armed"] = not m.cfg["armed"]
        srv.save_config(m.cfg)
        refresh(icon)

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def quit_app(icon, item):
        icon.visible = False
        server.should_exit = True
        thread.join(timeout=8)
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Otwórz podgląd", lambda: webbrowser.open(local_url), default=True),
        pystray.MenuItem("Kod QR dla telefonu", lambda: webbrowser.open(local_url + "qr")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Czuwanie (alarmy)", toggle_armed, checked=lambda item: monitor().cfg["armed"]),
        pystray.MenuItem("Uruchamiaj razem z Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("Folder z danymi", lambda: os.startfile(srv.HOME)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Zakończ", quit_app),
    )
    icon = pystray.Icon(APP_NAME, make_icon("ok"), "Niania", menu)
    state = {"status": None, "last_event": monitor().events[-1]["id"] if monitor().events else None}

    def refresh(icon):
        m = monitor()
        cam_ok, mic_ok = m.camera.ok, m.audio.ok
        status = "bad" if not (cam_ok and mic_ok) else ("ok" if m.cfg["armed"] else "off")
        if status != state["status"]:
            state["status"] = status
            icon.icon = make_icon(status)
        icon.title = (f"Niania – {'czuwa' if m.cfg['armed'] else 'alarmy wyłączone'}\n"
                      f"Kamera: {'OK' if cam_ok else 'brak!'} · Mikrofon: {'OK' if mic_ok else 'brak!'}\n{m.url}")

    def watch(icon):
        icon.visible = True
        icon.notify(f"Niania działa. Na telefonie otwórz {monitor().url}", "Niania")
        while True:
            try:
                refresh(icon)
                m = monitor()
                if m.events and m.events[-1]["id"] != state["last_event"]:
                    e = m.events[-1]
                    state["last_event"] = e["id"]
                    name = m.cfg["baby_name"]
                    icon.notify(f"{name} płacze" if e["type"] == "cry" else "Ruch w pokoju", "Niania")
            except Exception as e:  # noqa: BLE001
                print(f"[zasobnik] {e}", flush=True)
            time.sleep(2)

    icon.run(setup=lambda ic: threading.Thread(target=watch, args=(ic,), daemon=True).start())


if __name__ == "__main__":
    main()
