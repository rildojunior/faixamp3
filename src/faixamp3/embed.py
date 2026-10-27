"""Lista de faixas pelo player de incorporação público (open.spotify.com/embed).

É a mesma página que sites usam para embutir uma playlist: não precisa de login
nem de chave. Limite do próprio Spotify: só as 100 primeiras faixas.
"""

from __future__ import annotations

import json
import re
import time

import requests

from .models import Track

EMBED_LIMIT = 100
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130 Safari/537.36")
_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


class EmbedError(Exception):
    pass


def fetch_playlist(playlist_id: str, session: requests.Session | None = None) -> tuple[str, list[Track]]:
    """Retorna (nome, faixas básicas: id, título, artistas e duração)."""
    http = session or requests
    r = http.get(f"https://open.spotify.com/embed/playlist/{playlist_id}", headers={"User-Agent": _UA}, timeout=30)
    m = _DATA_RE.search(r.text) if r.ok else None
    if not m:
        raise EmbedError(f"Player público indisponível para esta playlist (HTTP {r.status_code}).")
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise EmbedError("O formato do player público mudou; não foi possível ler as faixas.") from e

    tracks = []
    for item in entity.get("trackList") or []:
        uri = item.get("uri") or ""
        if item.get("entityType", "track") != "track" or not uri.startswith("spotify:track:"):
            continue
        artists = tuple(a.strip() for a in (item.get("subtitle") or "").split(",") if a.strip())
        tracks.append(Track(
            id=uri.rsplit(":", 1)[-1],
            title=item.get("title", ""),
            artists=artists,
            duration_s=(item.get("duration") or 0) / 1000,
            explicit=bool(item.get("isExplicit")),
        ))
    return entity.get("name") or playlist_id, tracks


def fetch_track(track_id: str, session: requests.Session | None = None) -> Track:
    """Dados de uma faixa pelo player público (título, artistas, duração, data e capa)."""
    http = session or requests
    for attempt in range(3):
        try:
            r = http.get(f"https://open.spotify.com/embed/track/{track_id}", headers={"User-Agent": _UA}, timeout=20)
        except requests.RequestException:
            r = None
        if r is not None and r.ok:
            break
        time.sleep(1 + attempt * 2)
    m = _DATA_RE.search(r.text) if r is not None and r.ok else None
    if not m:
        raise EmbedError(f"faixa {track_id} indisponível no player público")
    try:
        e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EmbedError("formato do player público mudou") from exc
    images = (e.get("visualIdentity") or {}).get("image") or []
    cover = max(images, key=lambda i: i.get("maxWidth") or 0)["url"] if images else None
    release = ((e.get("releaseDate") or {}).get("isoString") or "")[:10] or None
    return Track(
        id=track_id,
        title=e.get("title") or e.get("name") or "",
        artists=tuple(a["name"] for a in e.get("artists") or [] if a.get("name")),
        duration_s=(e.get("duration") or 0) / 1000,
        release_date=release,
        cover_url=cover,
        explicit=bool(e.get("isExplicit")),
    )


def fetch_title(kind: str, ident: str, session: requests.Session | None = None) -> str:
    """Nome de uma playlist, álbum ou faixa pelo oEmbed público (sem login)."""
    http = session or requests
    r = http.get("https://open.spotify.com/oembed", params={"url": f"https://open.spotify.com/{kind}/{ident}"},
                 headers={"User-Agent": _UA}, timeout=15)
    r.raise_for_status()
    return (r.json().get("title") or "").strip()
