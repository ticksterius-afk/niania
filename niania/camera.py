"""Kamera: przechwytywanie obrazu, JPEG do podglądu i wykrywanie ruchu."""
import threading
import time

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras


def list_cameras():
    return [{"index": c.index, "name": c.name} for c in enumerate_cameras(cv2.CAP_DSHOW)]


def pick_camera(name_hint: str, index: int | None):
    """Zwraca (index, nazwa). Preferuje fizyczną kamerę z nazwą zawierającą name_hint."""
    cams = list(enumerate_cameras(cv2.CAP_DSHOW))
    if index is not None:
        for c in cams:
            if c.index == index:
                return c.index, c.name
        return index, f"kamera #{index}"
    hint = (name_hint or "").lower()
    real = [c for c in cams if "virtual" not in c.name.lower()]
    # kolejność: fizyczna kamera z nazwą -> dowolna fizyczna -> wirtualna z nazwą -> cokolwiek
    for group in ([c for c in real if hint and hint in c.name.lower()], real,
                  [c for c in cams if hint and hint in c.name.lower()], cams):
        if group:
            return group[0].index, group[0].name
    return None, None


def motion_threshold(sensitivity: float) -> float:
    """Czułość 0..100 -> ułamek zmienionych pikseli (0.10 przy 0, 0.002 przy 100)."""
    s = min(max(sensitivity, 0), 100) / 100
    return 0.10 * (0.02 ** s)


class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = None
        self.lock = threading.Lock()
        self.jpeg = None
        self.frame_id = 0
        self.last_frame_t = 0.0
        self.fps = 0.0
        self.motion = 0.0          # ułamek zmienionych pikseli w ostatniej klatce
        self.motion_active = False  # ruch utrzymuje się dłużej niż motion_min_seconds
        self._above_since = None
        self._below_since = None
        self._bg = None
        self._reopen = False
        self._stop = False
        threading.Thread(target=self._run, daemon=True, name="camera").start()

    @property
    def ok(self):
        return time.time() - self.last_frame_t < 5

    def stop(self):
        self._stop = True

    def reopen(self):
        """Przełącza kamerę wg aktualnego cfg['camera_name']."""
        self._reopen = True

    def latest_jpeg(self):
        with self.lock:
            return self.jpeg, self.frame_id

    def _open(self):
        idx, name = pick_camera(self.cfg.get("camera_name"), self.cfg.get("camera_index"))
        if idx is None:
            return None
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
        self.name = name
        print(f"[kamera] {name} (#{idx})", flush=True)
        return cap

    def _run(self):
        cap = None
        frame_times = []
        min_dt = 1.0 / max(self.cfg["fps"], 1)
        last = 0.0
        last_check = time.time()
        while not self._stop:
            if cap is not None and not self._reopen and time.time() - last_check > 10:
                # co 10 s: czy pojawiła się kamera lepiej pasująca do wyboru (np. podłączony OBSBOT)?
                last_check = time.time()
                _, best = pick_camera(self.cfg.get("camera_name"), self.cfg.get("camera_index"))
                hint = (self.cfg.get("camera_name") or "").lower()
                if best and best != self.name and hint and hint in best.lower():
                    self._reopen = True
            if self._reopen and cap is not None:
                self._reopen = False
                cap.release()
                cap = None
            if cap is None:
                self._reopen = False
                cap = self._open()
                if cap is None:
                    time.sleep(3)
                    continue
                self._bg = None
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                cap = None
                time.sleep(2)
                continue
            now = time.time()
            if now - last < min_dt:
                continue
            last = now
            self._detect_motion(frame, now)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with self.lock:
                    self.jpeg = buf.tobytes()
                    self.frame_id += 1
                    self.last_frame_t = now
            frame_times = [t for t in frame_times if now - t < 2] + [now]
            self.fps = len(frame_times) / 2
        if cap is not None:
            cap.release()

    def _detect_motion(self, frame, now):
        small = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        if self._bg is None:
            self._bg = small.astype(np.float32)
            return
        diff = cv2.absdiff(small, cv2.convertScaleAbs(self._bg))
        frac = float(np.count_nonzero(diff > 25)) / diff.size
        cv2.accumulateWeighted(small, self._bg, 0.05)
        if frac > 0.6:
            # zapalenie/zgaszenie światła albo zmiana ekspozycji – to nie ruch
            self._bg = small.astype(np.float32)
            frac = 0.0
        self.motion = frac

        thr = motion_threshold(self.cfg["motion_sensitivity"])
        if frac > thr:
            self._below_since = None
            if self._above_since is None:
                self._above_since = now
            if now - self._above_since >= self.cfg["motion_min_seconds"]:
                self.motion_active = True
        else:
            if self._below_since is None:
                self._below_since = now
            # krótkie przerwy (<1 s) nie przerywają ruchu
            if now - self._below_since > 1.0:
                self._above_since = None
                self.motion_active = False
