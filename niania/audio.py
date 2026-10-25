"""Mikrofon: poziom dźwięku, rozpoznawanie płaczu (YAMNet) i strumień na żywo."""
import asyncio
import collections
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from mediapipe.tasks.python import audio as mp_audio
from mediapipe.tasks.python.components.containers import audio_data as mp_audio_data
from mediapipe.tasks.python.core.base_options import BaseOptions

SR = 16000
CHUNK = 1600                 # 100 ms
WINDOW = 15600               # okno YAMNet ~0,975 s
CRY_CLASSES = (19, 20)       # "Crying, sobbing", "Baby cry, infant cry"


SKIP = ("mapper", "primary", "miks stereo", "stereo mix")


def list_mics():
    """Mikrofony do wyboru: wejścia DirectSound (pełne nazwy, przyjmują 16 kHz), bez sterowników zbiorczych."""
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if (d["max_input_channels"] > 0 and apis[d["hostapi"]]["name"] == "Windows DirectSound"
                and not any(s in d["name"].lower() for s in SKIP)):
            out.append({"index": i, "name": d["name"]})
    return out


def pick_mic(name_hint: str):
    """Zwraca (index, nazwa) wejścia audio: pasujące do name_hint, potem domyślne Windows."""
    devices = sd.query_devices()
    apis = sd.query_hostapis()
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    order = {"Windows DirectSound": 0, "MME": 1, "Windows WASAPI": 2}
    inputs.sort(key=lambda x: order.get(apis[x[1]["hostapi"]]["name"], 9))
    inputs = [(i, d) for i, d in inputs if not any(s in d["name"].lower() for s in SKIP)]
    hint = (name_hint or "").lower()
    if hint:
        for i, d in inputs:
            if hint in d["name"].lower():
                return i, d["name"]
    default_in = sd.default.device[0]
    if default_in is not None and default_in >= 0:
        for i, d in inputs:   # ta sama nazwa, ale przez DirectSound
            if devices[default_in]["name"][:20] in d["name"]:
                return i, d["name"]
    if inputs:
        return inputs[0][0], inputs[0][1]["name"]
    return None, None


class Audio:
    def __init__(self, cfg, model_path):
        self.cfg = cfg
        self.name = None
        self.level_db = -90.0
        self.cry = 0.0               # ostatni wynik "płacz" 0..1
        self.top = ("", 0.0)         # najbardziej prawdopodobny dźwięk
        self.cry_active = False
        self.last_data_t = 0.0
        self._history = collections.deque(maxlen=200)   # (czas, wynik płaczu)
        self._q = queue.Queue(maxsize=100)
        self._ring = np.zeros(WINDOW, dtype=np.float32)
        self._listeners = set()      # (loop, asyncio.Queue)
        self._listeners_lock = threading.Lock()
        opts = mp_audio.AudioClassifierOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            max_results=-1,
            running_mode=mp_audio.RunningMode.AUDIO_CLIPS,
        )
        self._clf = mp_audio.AudioClassifier.create_from_options(opts)
        self.mics = []
        self._rescan = threading.Event()
        self._rescanned = threading.Event()
        self._stop = False
        threading.Thread(target=self._run, daemon=True, name="audio").start()

    @property
    def ok(self):
        return time.time() - self.last_data_t < 5

    def stop(self):
        self._stop = True

    def reopen(self, wait=5.0):
        """Ponownie wykrywa urządzenia i otwiera mikrofon wg cfg['mic_name']; zwraca listę mikrofonów."""
        self._rescanned.clear()
        self._rescan.set()
        self._rescanned.wait(wait)
        return self.mics

    # --- słuchanie na żywo -------------------------------------------------
    def add_listener(self, loop):
        q = asyncio.Queue(maxsize=50)
        with self._listeners_lock:
            self._listeners.add((loop, q))
        return q

    def remove_listener(self, q):
        with self._listeners_lock:
            self._listeners = {(l, x) for l, x in self._listeners if x is not q}

    def _broadcast(self, pcm16: bytes):
        with self._listeners_lock:
            listeners = list(self._listeners)
        for loop, q in listeners:
            def put(q=q):
                if not q.full():
                    q.put_nowait(pcm16)
            loop.call_soon_threadsafe(put)

    # --- przechwytywanie ---------------------------------------------------
    def _callback(self, indata, frames, t, status):
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def _open(self):
        # PortAudio zna tylko urządzenia z chwili startu – odśwież, żeby zobaczyć podłączone później
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:  # noqa: BLE001
            print(f"[mikrofon] odświeżanie urządzeń: {e}", flush=True)
        self.mics = list_mics()
        self._rescanned.set()
        idx, name = pick_mic(self.cfg.get("mic_name"))
        if idx is None:
            return None, None
        dev = sd.query_devices(idx)
        for rate in (SR, int(dev["default_samplerate"])):
            try:
                stream = sd.InputStream(device=idx, channels=1, samplerate=rate, dtype="float32",
                                        blocksize=int(rate * CHUNK / SR), callback=self._callback)
                stream.start()
                self.name = name
                print(f"[mikrofon] {name} (#{idx}, {rate} Hz)", flush=True)
                return stream, rate
            except Exception as e:  # noqa: BLE001
                print(f"[mikrofon] {name} @ {rate} Hz: {e}", flush=True)
        return None, None

    def _close(self, stream):
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        while not self._q.empty():
            self._q.get_nowait()

    def _run(self):
        stream, rate = None, SR
        pending = np.zeros(0, dtype=np.float32)
        since_classify = 0
        last_check = time.time()
        while not self._stop:
            hint = (self.cfg.get("mic_name") or "").lower()
            if stream is not None and (self._rescan.is_set() or (
                    hint and hint not in (self.name or "").lower() and time.time() - last_check > 30)):
                # na prośbę albo co 30 s, gdy wybrany mikrofon był niedostępny – sprawdź ponownie
                self._rescan.clear()
                last_check = time.time()
                self._close(stream)
                stream = None
            if stream is None:
                stream, rate = self._open()
                if stream is None:
                    time.sleep(3)
                    continue
            try:
                block = self._q.get(timeout=3)
            except queue.Empty:
                # mikrofon przestał dawać dane (np. odłączony) – otwórz ponownie
                self._close(stream)
                stream = None
                continue
            if rate != SR:
                n = int(len(block) * SR / rate)
                block = np.interp(np.linspace(0, len(block) - 1, n), np.arange(len(block)), block).astype(np.float32)
            pending = np.concatenate([pending, block])
            while len(pending) >= CHUNK:
                chunk, pending = pending[:CHUNK], pending[CHUNK:]
                self._on_chunk(chunk)
                since_classify += CHUNK
                if since_classify >= SR // 2:      # klasyfikacja co 0,5 s
                    since_classify = 0
                    self._classify()
        if stream is not None:
            stream.close()

    def _on_chunk(self, chunk):
        self.last_data_t = time.time()
        rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
        self.level_db = max(-90.0, 20 * np.log10(rms))
        self._ring = np.concatenate([self._ring[CHUNK:], chunk])
        pcm = (np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes()
        self._broadcast(pcm)

    def _classify(self):
        data = mp_audio_data.AudioData.create_from_array(self._ring.copy(), SR)
        result = self._clf.classify(data)
        if not result:
            return
        cats = result[-1].classifications[0].categories
        scores = {c.index: c.score for c in cats}
        self.cry = max(scores.get(i, 0.0) for i in CRY_CLASSES)
        best = max(cats, key=lambda c: c.score)
        self.top = (best.category_name, float(best.score))
        now = time.time()
        self._history.append((now, self.cry))
        self._update_cry_active(now)

    def _update_cry_active(self, now):
        span = self.cfg["cry_min_seconds"]
        recent = [s for t, s in self._history if now - t <= span]
        if len(recent) < max(2, int(span * 2) - 1):
            self.cry_active = False
            return
        hits = sum(1 for s in recent if s >= self.cfg["cry_threshold"])
        # płacz ma pauzy na oddech – wystarczy 60% okien
        self.cry_active = hits / len(recent) >= 0.6
