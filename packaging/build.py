"""Gera o app com PyInstaller: `uv run python packaging/build.py`.

Precisa rodar no próprio sistema de destino (PyInstaller não faz build cruzado):
no Mac gera dist/FaixaMP3.app, no Windows gera dist/FaixaMP3/FaixaMP3.exe.
Para embutir ffmpeg/deno no Windows, coloque ffmpeg.exe e deno.exe em packaging/bin/.
"""

import sys
from pathlib import Path

import PyInstaller.__main__

root = Path(__file__).resolve().parent
args = [
    str(root / "launcher.py"),
    "--name", "FaixaMP3",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--collect-data", "ytmusicapi",
    "--collect-all", "yt_dlp_ejs",
    "--distpath", str(root.parent / "dist"),
    "--workpath", str(root.parent / "build"),
    "--specpath", str(root.parent / "build"),
]
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "app.faixamp3"]
bin_dir = root / "bin"
if bin_dir.exists():
    for exe in bin_dir.iterdir():
        if exe.is_file():
            args += ["--add-binary", f"{exe}{':' if sys.platform != 'win32' else ';'}bin"]
icon = root / ("icon.ico" if sys.platform == "win32" else "icon.icns")
if icon.exists():
    args += ["--icon", str(icon)]

PyInstaller.__main__.run(args)
