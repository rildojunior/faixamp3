"""Encontra no YouTube Music o áudio que corresponde a uma faixa do Spotify.

Diferente do Allavsoft (que aceita o primeiro vídeo que baixa), aqui cada
candidato recebe uma nota por título, artista e duração, e versões
"ao vivo", "cover", "remix" etc. são rejeitadas quando a original não é isso.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from ytmusicapi import YTMusic

from .models import Track

MIN_SCORE = 0.70
MAX_DURATION_DIFF = 12  # segundos

_VARIANT_WORDS = [
    "live", "ao vivo", "en vivo", "en directo", "cover", "karaoke", "instrumental", "remix",
    "sped up", "speed up", "slowed", "nightcore", "8d", "acapella", "a cappella", "reverb",
    "bass boosted", "acoustic", "acustico", "demo", "edit", "mashup", "piano version", "a capela",
    "capela", "ukulele", "versao", "violao", "guitar cover", "tribute", "tributo", "reaction",
]
_FEAT_RE = re.compile(r"[\(\[]\s*(feat\.?|ft\.?|featuring|with|com|part\.?)\s[^\)\]]*[\)\]]", re.I)
_SUFFIX_RE = re.compile(r"\s-\s.*(remaster|version|versão|mono|stereo|single|radio edit|edit).*$", re.I)
_TAG_RE = re.compile(r"[\(\[][^\)\]]*(official|oficial|video|vídeo|audio|áudio|lyric|letra|visualizer|hd|4k)[^\)\]]*[\)\]]", re.I)

_local = threading.local()


def _yt() -> YTMusic:
    # Um cliente por thread: as sessões HTTP não são compartilhadas entre workers.
    if not hasattr(_local, "yt"):
        _local.yt = YTMusic()
    return _local.yt


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("$", "s")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str) -> str:
    title = _FEAT_RE.sub("", title)
    title = _TAG_RE.sub("", title)
    title = _SUFFIX_RE.sub("", title)
    return normalize(title)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    containment = len(ta & tb) / min(len(ta), len(tb))
    return max(ratio, containment * 0.95)


def _variants(text: str) -> set[str]:
    # Separa hashtags em CamelCase (#TheCapela -> The Capela).
    norm = f" {normalize(re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', text))} "
    return {w for w in _VARIANT_WORDS if f" {w} " in norm}


@dataclass
class Match:
    video_id: str
    title: str
    artists: str
    duration_s: float
    score: float
    via: str

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"


def score(track: Track, cand: dict, via: str) -> float | None:
    dur = cand.get("duration_seconds") or 0
    if track.duration_s and dur:
        diff = abs(track.duration_s - dur)
        if diff > MAX_DURATION_DIFF:
            return None
        dur_score = 1.0 if diff <= 2 else max(0.0, 1 - (diff - 2) / (MAX_DURATION_DIFF - 2))
    else:
        dur_score = 0.5

    cand_title = cand.get("title") or ""
    cand_artists = [a.get("name", "") for a in cand.get("artists") or [] if a.get("name")]
    title_sim = _similarity(clean_title(track.title), clean_title(cand_title))

    # Vídeos comuns costumam ter "Artista - Música" no título e o canal como artista.
    haystack = [normalize(a) for a in cand_artists] + [normalize(cand_title)]
    artist_sim = max(
        (max(_similarity(normalize(sa), h), 1.0 if normalize(sa) in h else 0.0) for sa in track.artists for h in haystack),
        default=0.0,
    )

    if via != "isrc" and (title_sim < 0.6 or artist_sim < 0.6):
        return None

    # Versões alternativas que a faixa original não tem.
    extra = _variants(cand_title) - _variants(f"{track.title} {track.album}")
    if extra:
        return None

    total = 0.45 * title_sim + 0.25 * artist_sim + 0.30 * dur_score
    # Artista só no título do vídeo (canal de terceiros): menos confiável.
    channel_sim = max((_similarity(normalize(sa), normalize(ca)) for sa in track.artists for ca in cand_artists), default=0.0)
    if channel_sim < 0.6:
        total -= 0.10
    vtype = cand.get("videoType") or ""
    if vtype == "MUSIC_VIDEO_TYPE_ATV":  # áudio oficial de estúdio, sem intro de clipe
        total += 0.08
    elif cand.get("resultType") == "video" and vtype != "MUSIC_VIDEO_TYPE_OMV":
        total -= 0.05
    if via == "isrc":
        total += 0.15
    return min(total, 1.0)


def find_match(track: Track) -> Match | None:
    yt = _yt()
    query = f"{track.main_artist} {clean_title(track.title)}"
    searches = []
    if track.isrc:
        searches.append(("isrc", track.isrc, "songs"))
    searches += [("songs", query, "songs"), ("videos", query, "videos")]

    best: Match | None = None
    for via, q, flt in searches:
        try:
            results = yt.search(q, filter=flt, limit=10)
        except Exception:
            continue
        for cand in results[:8]:
            if not cand.get("videoId"):
                continue
            s = score(track, cand, via)
            if s is not None and (best is None or s > best.score):
                best = Match(
                    video_id=cand["videoId"],
                    title=cand.get("title", ""),
                    artists=", ".join(a.get("name", "") for a in cand.get("artists") or []),
                    duration_s=cand.get("duration_seconds") or 0,
                    score=round(s, 3),
                    via=via,
                )
        if best and best.score >= 0.9:
            break
    return best if best and best.score >= MIN_SCORE else None
