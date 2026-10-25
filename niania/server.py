"""Niania – serwer: podgląd, dźwięk na żywo, zdarzenia i powiadomienia ntfy."""
import asyncio
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import certifi
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse

from .audio import Audio
from .camera import Camera, list_cameras, motion_threshold

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA = ROOT / "data"
EVENTS_DIR = DATA / "events"
EVENTS_LOG = DATA / "events.jsonl"
STATIC = Path(__file__).resolve().parent / "static"
KEEP_DAYS = 7
# magazyn certyfikatów Windows ma przeterminowany łańcuch dla ntfy.sh – używamy certifi
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

DEFAULTS = {
    "port": 8080,
    "baby_name": "Aron",
    "camera_name": "OBSBOT",
    "camera_index": None,
    "width": 1280,
    "height": 720,
    "fps": 15,
    "mic_name": "OBSBOT",
    "armed": True,
    "motion_enabled": True,
    "motion_sensitivity": 50,
    "motion_min_seconds": 2,
    "motion_cooldown_s": 120,
    "cry_enabled": True,
    "cry_threshold": 0.35,
    "cry_min_seconds": 3,
    "cry_cooldown_s": 60,
    "ntfy_enabled": True,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
}
EDITABLE = {
    "armed": bool, "motion_enabled": bool, "cry_enabled": bool, "ntfy_enabled": bool,
    "motion_sensitivity": float, "motion_min_seconds": float, "cry_threshold": float, "cry_min_seconds": float,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if not cfg["ntfy_topic"]:
        cfg["ntfy_topic"] = "niania-" + secrets.token_hex(8)
    save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # nic nie jest wysyłane, tylko wybór interfejsu
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Monitor:
    """Łączy kamerę i mikrofon, tworzy zdarzenia i wysyła powiadomienia."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = f"http://{lan_ip()}:{cfg['port']}/"
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        self.events = self._load_events()
        self.last_event_t = {"motion": 0.0, "cry": 0.0}
        self.health = {"camera": True, "audio": True}
        self.started = time.time()
        self.camera = Camera(cfg)
        self.audio = Audio(cfg, ROOT / "models" / "yamnet.tflite")
        self._stop = False
        threading.Thread(target=self._loop, daemon=True, name="monitor").start()

    def stop(self):
        self._stop = True
        self.camera.stop()
        self.audio.stop()

    # --- zdarzenia -----------------------------------------------------------
    def _load_events(self):
        events = []
        if EVENTS_LOG.exists():
            cutoff = time.time() - KEEP_DAYS * 86400
            for line in EVENTS_LOG.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e["t"] >= cutoff:
                    events.append(e)
            EVENTS_LOG.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            keep = {e.get("snapshot") for e in events}
            for f in EVENTS_DIR.glob("*.jpg"):
                if f.name not in keep:
                    f.unlink()
        return events[-300:]

    def _add_event(self, kind, score):
        now = time.time()
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S")
        snapshot = None
        jpeg, _ = self.camera.latest_jpeg()
        if jpeg:
            snapshot = f"{stamp}_{kind}.jpg"
            (EVENTS_DIR / snapshot).write_bytes(jpeg)
        event = {"id": int(now * 1000), "t": now, "type": kind, "score": round(score, 3), "snapshot": snapshot}
        self.events.append(event)
        self.events = self.events[-300:]
        with EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        name = self.cfg["baby_name"]
        hhmm = datetime.fromtimestamp(now).strftime("%H:%M")
        if kind == "cry":
            self.notify(f"{name} płacze", f"{hhmm} – wykryto płacz. Kliknij, żeby zobaczyć pokój.", 5, "baby,loudspeaker")
        else:
            self.notify("Niania: ruch w pokoju", f"{hhmm} – coś się rusza ({name}).", 4, "baby,eyes")
        print(f"[zdarzenie] {kind} {score:.3f}", flush=True)

    def _loop(self):
        while not self._stop:
            time.sleep(0.5)
            now = time.time()
            cfg = self.cfg
            if cfg["armed"]:
                if cfg["cry_enabled"] and self.audio.cry_active and now - self.last_event_t["cry"] > cfg["cry_cooldown_s"]:
                    self.last_event_t["cry"] = now
                    self._add_event("cry", self.audio.cry)
                if (cfg["motion_enabled"] and self.camera.motion_active
                        and now - self.last_event_t["motion"] > cfg["motion_cooldown_s"]
                        and now - self.last_event_t["cry"] > 10):   # przy płaczu nie dublujemy alarmu ruchem
                    self.last_event_t["motion"] = now
                    self._add_event("motion", self.camera.motion)
            if now - self.started > 20:
                self._check_health("camera", self.camera.ok, "Kamera")
                self._check_health("audio", self.audio.ok, "Mikrofon")

    def _check_health(self, key, ok, label):
        if ok == self.health[key]:
            return
        self.health[key] = ok
        if ok:
            self.notify(f"Niania: {label.lower()} znów działa", f"{label} wrócił(a) do pracy.", 3, "white_check_mark")
        else:
            self.notify(f"Niania: {label.lower()} nie działa!", f"{label} przestał(a) dawać dane – sprawdź komputer.", 5, "warning")

    # --- ntfy ----------------------------------------------------------------
    def notify(self, title, message, priority=3, tags="", force=False):
        if not (self.cfg["ntfy_enabled"] or force):
            return
        body = {
            "topic": self.cfg["ntfy_topic"], "title": title, "message": message, "priority": priority,
            "tags": [t for t in tags.split(",") if t], "click": self.url,
        }

        def send():
            req = urllib.request.Request(self.cfg["ntfy_server"].rstrip("/"), data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10, context=SSL_CTX).read()
            except Exception as e:  # noqa: BLE001
                print(f"[ntfy] błąd: {e}", flush=True)

        threading.Thread(target=send, daemon=True).start()

    def status(self):
        cam, aud, cfg = self.camera, self.audio, self.cfg
        return {
            "now": time.time(),
            "url": self.url,
            "ntfy": {"server": cfg["ntfy_server"], "topic": cfg["ntfy_topic"]},
            "settings": {k: cfg[k] for k in EDITABLE} | {"baby_name": cfg["baby_name"]},
            "camera": {"ok": cam.ok, "name": cam.name, "fps": round(cam.fps, 1), "motion": round(cam.motion, 4),
                       "motion_thr": round(motion_threshold(cfg["motion_sensitivity"]), 4),
                       "motion_active": cam.motion_active},
            "audio": {"ok": aud.ok, "name": aud.name, "level_db": round(aud.level_db, 1), "cry": round(aud.cry, 3),
                      "cry_active": aud.cry_active, "top": [aud.top[0], round(aud.top[1], 3)]},
            "last_event": self.events[-1] if self.events else None,
        }


monitor: Monitor | None = None


@asynccontextmanager
async def lifespan(app):
    global monitor
    monitor = Monitor(load_config())
    print(f"\n  Niania działa:  {monitor.url}\n  Temat ntfy:     {monitor.cfg['ntfy_topic']}\n", flush=True)
    monitor.notify("Niania uruchomiona", f"Podgląd: {monitor.url}", 2, "baby")
    yield
    monitor.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
def status():
    return monitor.status()


@app.get("/api/events")
def events(limit: int = 50):
    return list(reversed(monitor.events[-limit:]))


@app.post("/api/settings")
def settings(changes: dict):
    for key, value in changes.items():
        if key not in EDITABLE:
            raise HTTPException(400, f"nieznane ustawienie: {key}")
        monitor.cfg[key] = EDITABLE[key](value)
    save_config(monitor.cfg)
    return monitor.status()["settings"]


@app.get("/api/devices")
def devices(refresh: bool = False):
    mics = monitor.audio.reopen() if refresh or not monitor.audio.mics else monitor.audio.mics
    return {
        "cameras": list_cameras(), "mics": mics,
        "camera": monitor.camera.name, "mic": monitor.audio.name,
        "camera_pref": monitor.cfg["camera_name"], "mic_pref": monitor.cfg["mic_name"],
    }


@app.post("/api/device")
def set_device(choice: dict):
    if "camera" in choice:
        monitor.cfg["camera_name"] = str(choice["camera"])
        monitor.cfg["camera_index"] = None
        monitor.camera.reopen()
    if "mic" in choice:
        monitor.cfg["mic_name"] = str(choice["mic"])
        monitor.audio.reopen(wait=0)
    save_config(monitor.cfg)
    return {"ok": True}


@app.post("/api/test-notify")
def test_notify():
    monitor.notify("Niania: test", "Powiadomienia działają 👍", 3, "baby", force=True)
    return {"ok": True}


@app.get("/snapshot.jpg")
def snapshot():
    jpeg, _ = monitor.camera.latest_jpeg()
    if not jpeg:
        raise HTTPException(503, "brak obrazu")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/events/{name}")
def event_image(name: str):
    path = EVENTS_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/stream.mjpg")
async def stream():
    async def frames():
        last_id = -1
        while True:
            jpeg, fid = monitor.camera.latest_jpeg()
            if jpeg and fid != last_id:
                last_id = fid
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            await asyncio.sleep(0.03)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.websocket("/ws/audio")
async def audio_ws(ws: WebSocket):
    await ws.accept()
    q = monitor.audio.add_listener(asyncio.get_running_loop())
    try:
        while True:
            await ws.send_bytes(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        monitor.audio.remove_listener(q)
