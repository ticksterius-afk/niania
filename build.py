"""Buduje dist\\Niania\\Niania.exe (PyInstaller, tryb folderowy – szybki start)."""
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent

# ikona z tego samego rysunku co w zasobniku
from tray import make_icon  # noqa: E402

ico = ROOT / "build" / "niania.ico"
ico.parent.mkdir(exist_ok=True)
make_icon("ok").save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])

PyInstaller.__main__.run([
    str(ROOT / "tray.py"),
    "--name", "Niania",
    "--noconfirm",
    "--windowed",
    "--icon", str(ico),
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    "--add-data", f"{ROOT / 'niania' / 'static'};niania/static",
    "--add-data", f"{ROOT / 'models' / 'yamnet.tflite'};models",
    "--collect-all", "mediapipe",
    "--collect-all", "sounddevice",
    "--collect-all", "cv2_enumerate_cameras",
    "--collect-data", "certifi",
    "--collect-submodules", "uvicorn",
    "--hidden-import", "pystray._win32",
])
