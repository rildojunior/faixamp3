"""Importa playlists de CSV (ex.: exportadas pelo Exportify).

Útil para playlists de outras pessoas, que a API em modo de desenvolvimento não lê.
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

from .models import Playlist, Track

_ALIASES = {
    "title": ["track name", "name", "title", "música", "musica", "faixa"],
    "artists": ["artist name(s)", "artist names", "artists", "artist", "artista", "artistas"],
    "album": ["album name", "album", "álbum"],
    "album_artist": ["album artist name(s)", "album artist"],
    "duration_ms": ["duration (ms)", "duration_ms", "track duration (ms)"],
    "isrc": ["isrc"],
    "uri": ["track uri", "spotify uri", "uri"],
    "release_date": ["release date", "album release date"],
    "track_number": ["track number"],
    "disc_number": ["disc number"],
    "cover_url": ["album image url", "image url"],
    "explicit": ["explicit"],
}


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def load_csv(path: str | Path) -> Playlist:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = {h.strip().lower(): h for h in reader.fieldnames or []}
        col = {key: next((header[a] for a in names if a in header), None) for key, names in _ALIASES.items()}
        if not col["title"] or not col["artists"]:
            raise ValueError("CSV precisa ter ao menos colunas de nome da faixa e artista.")

        tracks = []
        for row in reader:
            get = lambda k: (row.get(col[k]) or "").strip() if col[k] else ""  # noqa: E731
            title = get("title")
            if not title:
                continue
            artists = tuple(a.strip() for a in re.split(r"[,;]", get("artists")) if a.strip())
            uri = get("uri")
            track_id = uri.rsplit(":", 1)[-1] if uri else hashlib.sha1(f"{artists}{title}".encode()).hexdigest()[:22]
            tracks.append(
                Track(
                    id=track_id,
                    title=title,
                    artists=artists,
                    album=get("album"),
                    album_artist=get("album_artist") or (artists[0] if artists else ""),
                    duration_s=(_int(get("duration_ms")) or 0) / 1000,
                    isrc=get("isrc") or None,
                    track_number=_int(get("track_number")),
                    disc_number=_int(get("disc_number")),
                    release_date=get("release_date") or None,
                    cover_url=get("cover_url") or None,
                    explicit=get("explicit").lower() == "true",
                )
            )
    return Playlist(id=f"csv:{path.stem}", name=path.stem, tracks=tracks)
