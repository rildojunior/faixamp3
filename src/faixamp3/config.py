from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from platformdirs import user_config_dir, user_music_dir

APP_NAME = "faixamp3"
CONFIG_DIR = Path(user_config_dir(APP_NAME, appauthor=False))
_OLD_CONFIG_DIR = Path(user_config_dir("mp3car", appauthor=False))  # nome antigo do app
if _OLD_CONFIG_DIR.exists() and not CONFIG_DIR.exists():
    _OLD_CONFIG_DIR.rename(CONFIG_DIR)
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "spotify_token.json"
TRACK_CACHE_FILE = CONFIG_DIR / "track_cache.json"
PLAYLISTS_CACHE_FILE = CONFIG_DIR / "playlists_cache.json"


@dataclass
class Settings:
    client_id: str = ""
    output_dir: str = str(Path(user_music_dir()) / "FaixaMP3")
    workers: int = 4
    bitrate: int = 320
    normalize: bool = False
    ascii_names: bool = False
    cover_size: int = 500
    use_desktop: bool = True  # copiar playlists grandes de outras pessoas pelo app do Spotify

    @classmethod
    def load(cls) -> Settings:
        if not CONFIG_FILE.exists():
            return cls()
        data = json.loads(CONFIG_FILE.read_text("utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), "utf-8")


def _bundle_dir() -> Path | None:
    # Diretório de binários embutidos quando empacotado (PyInstaller).
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "bin" if base else None


def find_tool(name: str) -> str | None:
    """Procura ffmpeg/deno: variável FAIXAMP3_<NOME>, pacote embutido e depois PATH."""
    env = os.environ.get(f"FAIXAMP3_{name.upper()}")
    if env and Path(env).exists():
        return env
    bundle = _bundle_dir()
    if bundle:
        exe = name + (".exe" if sys.platform == "win32" else "")
        if (bundle / exe).exists():
            return str(bundle / exe)
    found = shutil.which(name)
    if found:
        return found
    # Apps abertos pelo Finder não herdam o PATH do terminal.
    extra = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".deno" / "bin")]
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        extra += [str(local / "Microsoft" / "WinGet" / "Links"), str(Path.home() / ".deno" / "bin")]
    return shutil.which(name, path=os.pathsep.join(extra))
