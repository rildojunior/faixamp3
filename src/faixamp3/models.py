from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artists: tuple[str, ...]
    album: str = ""
    album_artist: str = ""
    duration_s: float = 0.0
    isrc: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    release_date: str | None = None
    cover_url: str | None = None
    explicit: bool = False

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def main_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def year(self) -> str | None:
        return self.release_date[:4] if self.release_date else None

    def __str__(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class Playlist:
    id: str
    name: str
    tracks: list[Track] = field(default_factory=list)
    owner: str = ""
    note: str = ""  # aviso para o usuário (ex.: playlist incompleta)
