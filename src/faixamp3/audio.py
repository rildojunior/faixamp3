"""Download (yt-dlp), conversão para MP3 (ffmpeg) e tags ID3 (mutagen)."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

import requests
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, ID3NoHeaderError

from .config import find_tool
from .models import Track

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_cover_lock = threading.Lock()
_cover_cache: dict[tuple[str, int], bytes | None] = {}


class AudioError(Exception):
    pass


_KEEP_SYMBOLS = set("©®°")
# Partes invisíveis de emojis compostos: seletor de variação, ZWJ, keycap, tags de bandeira.
_EMOJI_GLUE = re.compile("[\ufe0e\ufe0f\u200d\u20e3\U000e0020-\U000e007f]")


def strip_emoji(text: str) -> str:
    """Remove emojis e símbolos gráficos que rádios de carro não sabem mostrar."""
    text = _EMOJI_GLUE.sub("", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in _KEEP_SYMBOLS:
            out.append(ch)
        elif cat in ("So", "Cs", "Co", "Cn") or (cat == "Sk" and 0x1F3FB <= ord(ch) <= 0x1F3FF):
            out.append(" ")
        else:
            out.append(ch)
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    # Sobras de separadores no começo/fim ("| Favs", "Rock -").
    return re.sub(r"^[\s|\-–—•·,:;]+|[\s|\-–—•·,:;]+$", "", text)


def safe_name(text: str, ascii_only: bool = False, max_len: int = 90) -> str:
    """Nome válido em FAT32/exFAT/NTFS e legível em rádios de carro."""
    text = strip_emoji(text)
    if ascii_only:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if len(text) > max_len:
        text = text[:max_len].rstrip(". ")
    if text.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        text = f"_{text}"
    return text


def download(video_url: str, workdir: Path) -> Path:
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "overwrites": True,
    }
    deno = find_tool("deno")
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            return Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise AudioError(str(e).removeprefix("ERROR: ")[:200]) from e


def _run_ffmpeg(args: list[str]) -> bytes:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise AudioError("ffmpeg não encontrado.")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg falhou: {proc.stderr.decode(errors='replace')[-300:]}")
    return proc.stdout


def to_mp3(src: Path, dst: Path, bitrate: int = 320, normalize: bool = False) -> None:
    args = ["-i", str(src), "-vn", "-map_metadata", "-1",
            "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k", "-ar", "44100", "-ac", "2"]
    if normalize:
        # Volume parecido entre faixas: evita ficar mexendo no volume do carro.
        args += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    _run_ffmpeg([*args, "-f", "mp3", str(dst)])


def cover_jpeg(url: str, size: int) -> bytes | None:
    """Capa quadrada em JPEG baseline (muitos rádios não leem JPEG progressivo)."""
    key = (url, size)
    with _cover_lock:
        if key in _cover_cache:
            return _cover_cache[key]
    data = None
    try:
        raw = requests.get(url, timeout=30).content
        data = _jpeg_resize(raw, size)
    except Exception:
        data = None
    with _cover_lock:
        _cover_cache[key] = data
    return data


def _jpeg_resize(raw: bytes, size: int) -> bytes:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise AudioError("ffmpeg não encontrado.")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={size}:{size}",
         "-q:v", "3", "-f", "mjpeg", "pipe:1"],
        input=raw, capture_output=True, creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError("falha ao redimensionar capa")
    return proc.stdout


def clean_tags(path: Path) -> None:
    """Tira emojis das tags de texto de um MP3 já existente."""
    try:
        tags = ID3(path)
    except (ID3NoHeaderError, FileNotFoundError):
        return
    changed = False
    for key in ("TIT2", "TPE1", "TALB", "TPE2"):
        frame = tags.get(key)
        if frame:
            new = [strip_emoji(str(t)) or str(t) for t in frame.text]
            if new != [str(t) for t in frame.text]:
                frame.text = new
                changed = True
    if changed:
        tags.save(path, v2_version=3, v1=2)


def write_tags(path: Path, track: Track, cover: bytes | None, source_url: str) -> None:
    try:
        tags = ID3(path)
        tags.clear()
    except ID3NoHeaderError:
        tags = ID3()
    clean = lambda s: strip_emoji(s) or s  # noqa: E731 - se só tinha emoji, mantém o original
    tags.add(TIT2(encoding=3, text=clean(track.title)))
    tags.add(TPE1(encoding=3, text=clean(track.artist)))
    if track.album:
        tags.add(TALB(encoding=3, text=clean(track.album)))
    tags.add(TPE2(encoding=3, text=clean(track.album_artist or track.main_artist)))
    if track.year:
        tags.add(TDRC(encoding=3, text=track.year))
    if track.track_number:
        tags.add(TRCK(encoding=3, text=str(track.track_number)))
    if track.disc_number:
        tags.add(TPOS(encoding=3, text=str(track.disc_number)))
    if track.isrc:
        tags.add(TXXX(encoding=3, desc="ISRC", text=track.isrc))
    tags.add(TXXX(encoding=3, desc="SOURCE", text=source_url))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    # ID3v2.3 + ID3v1: o que a maioria dos rádios automotivos entende.
    tags.save(path, v2_version=3, v1=2)
