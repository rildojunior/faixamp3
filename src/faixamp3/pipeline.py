"""Sincroniza uma playlist com uma pasta de MP3s, baixando faixas em paralelo.

Cada pasta guarda um manifesto (.faixamp3.json) ligando o ID do Spotify ao
arquivo: rodar de novo só baixa o que é novo, renomeia o que mudou de posição e
reaproveita faixas que já existem em outra playlist.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from . import audio
from .matcher import Match, find_match
from .models import Playlist, Track

MANIFEST = ".faixamp3.json"
OLD_MANIFEST = ".mp3car.json"  # nome antigo do app
TAGS_VERSION = 2  # 2: sem emojis nas tags


class Status(str, Enum):
    QUEUED = "na fila"
    MATCHING = "procurando"
    DOWNLOADING = "baixando"
    CONVERTING = "convertendo"
    DONE = "pronta"
    COPIED = "copiada"
    SKIPPED = "já existia"
    NOT_FOUND = "não encontrada"
    FAILED = "erro"
    CANCELLED = "cancelada"

    @property
    def finished(self) -> bool:
        return self in {Status.DONE, Status.COPIED, Status.SKIPPED, Status.NOT_FOUND, Status.FAILED, Status.CANCELLED}


@dataclass
class TrackEvent:
    index: int
    track: Track
    status: Status
    message: str = ""
    match: Match | None = None


@dataclass
class Options:
    output_dir: Path
    workers: int = 4
    bitrate: int = 320
    normalize: bool = False
    ascii_names: bool = False
    cover_size: int = 500
    prune: bool = False


@dataclass
class Report:
    playlist: str
    folder: Path
    total: int = 0
    downloaded: int = 0
    copied: int = 0
    skipped: int = 0
    removed: int = 0
    not_found: list[Track] = field(default_factory=list)
    failed: list[tuple[Track, str]] = field(default_factory=list)
    cancelled: bool = False


class Cancelled(Exception):
    pass


class Syncer:
    def __init__(self, opts: Options, on_event: Callable[[TrackEvent], None] | None = None,
                 cancel: threading.Event | None = None):
        self.opts = opts
        self.on_event = on_event or (lambda e: None)
        self.cancel = cancel or threading.Event()
        self._lock = threading.Lock()

    # ---------- manifesto ----------

    @staticmethod
    def _read_manifest(folder: Path) -> dict:
        try:
            return json.loads((folder / MANIFEST).read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": 1, "tracks": {}}

    def _save_manifest(self, folder: Path, manifest: dict) -> None:
        tmp = folder / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(folder / MANIFEST)

    def _library_index(self, exclude: Path) -> dict[str, Path]:
        """ID do Spotify -> MP3 já baixado em outra pasta da biblioteca."""
        index: dict[str, Path] = {}
        if not self.opts.output_dir.exists():
            return index
        for mf in self.opts.output_dir.glob(f"*/{MANIFEST}"):
            if mf.parent == exclude:
                continue
            for tid, entry in self._read_manifest(mf.parent).get("tracks", {}).items():
                p = mf.parent / entry["file"]
                if p.exists():
                    index.setdefault(tid, p)
        return index

    # ---------- sincronização ----------

    def _filename(self, pos: int, width: int, track: Track) -> str:
        name = audio.safe_name(f"{track.main_artist} - {track.title}", self.opts.ascii_names) or track.id
        return f"{pos:0{width}d} - {name}.mp3"

    def _migrate_manifests(self) -> None:
        if self.opts.output_dir.exists():
            for old in self.opts.output_dir.glob(f"*/{OLD_MANIFEST}"):
                if not (old.parent / MANIFEST).exists():
                    old.rename(old.parent / MANIFEST)

    def sync(self, playlist: Playlist) -> Report:
        self._migrate_manifests()
        folder_name = audio.safe_name(playlist.name, self.opts.ascii_names) or f"Playlist {playlist.id[-6:]}"
        folder = self.opts.output_dir / folder_name
        self._migrate_folder(playlist.id, folder)
        folder.mkdir(parents=True, exist_ok=True)
        manifest = self._read_manifest(folder)
        known: dict = manifest.setdefault("tracks", {})
        manifest["playlist"] = {"id": playlist.id, "name": playlist.name}

        # Faixas repetidas na playlist entram uma vez só.
        seen: set[str] = set()
        tracks = [t for t in playlist.tracks if not (t.id in seen or seen.add(t.id))]
        width = max(2, len(str(len(tracks))))
        wanted = {t.id: self._filename(i + 1, width, t) for i, t in enumerate(tracks)}
        report = Report(playlist.name, folder, total=len(tracks))

        if self.opts.prune:
            for tid in [tid for tid in known if tid not in wanted]:
                (folder / known.pop(tid)["file"]).unlink(missing_ok=True)
                report.removed += 1

        self._apply_renames(folder, known, wanted)
        if manifest.get("tags_version", 0) < TAGS_VERSION:
            # Arquivos baixados antes da limpeza de emojis.
            for entry in known.values():
                audio.clean_tags(folder / entry["file"])
            manifest["tags_version"] = TAGS_VERSION
        self._save_manifest(folder, manifest)

        library = self._library_index(exclude=folder)
        pending: list[tuple[int, Track]] = []
        for i, t in enumerate(tracks):
            if t.id in known and (folder / known[t.id]["file"]).exists():
                report.skipped += 1
                self.on_event(TrackEvent(i, t, Status.SKIPPED))
            elif t.id in library:
                shutil.copy2(library[t.id], folder / wanted[t.id])
                known[t.id] = {"file": wanted[t.id], "copied_from": str(library[t.id].parent.name)}
                report.copied += 1
                self.on_event(TrackEvent(i, t, Status.COPIED))
            else:
                pending.append((i, t))
                self.on_event(TrackEvent(i, t, Status.QUEUED))
        self._save_manifest(folder, manifest)

        with ThreadPoolExecutor(max_workers=max(1, self.opts.workers)) as pool:
            album_name = audio.strip_emoji(playlist.name) or folder_name
            futures = {pool.submit(self._process, i, t, folder / wanted[t.id], album_name): (i, t)
                       for i, t in pending}
            try:
                for fut in as_completed(futures):
                    i, t = futures[fut]
                    try:
                        match = fut.result()
                    except Cancelled:
                        self.on_event(TrackEvent(i, t, Status.CANCELLED))
                        continue
                    except LookupError:
                        report.not_found.append(t)
                        self.on_event(TrackEvent(i, t, Status.NOT_FOUND, "nenhum resultado confiável"))
                        continue
                    except Exception as e:  # noqa: BLE001
                        report.failed.append((t, str(e)))
                        self.on_event(TrackEvent(i, t, Status.FAILED, str(e)))
                        continue
                    with self._lock:
                        manifest["tags_version"] = TAGS_VERSION
                        known[t.id] = {"file": wanted[t.id], "video_id": match.video_id, "score": match.score}
                        self._save_manifest(folder, manifest)
                    report.downloaded += 1
                    self.on_event(TrackEvent(i, t, Status.DONE, match=match))
            except KeyboardInterrupt:
                self.cancel.set()
                pool.shutdown(cancel_futures=True)
                raise

        report.cancelled = self.cancel.is_set()
        self._write_missing(folder, report)
        return report

    def _migrate_folder(self, playlist_id: str, target: Path) -> None:
        """Se a playlist já foi baixada com outro nome de pasta (ex.: com emoji), renomeia."""
        if target.exists() or not self.opts.output_dir.exists():
            return
        for mf in self.opts.output_dir.glob(f"*/{MANIFEST}"):
            if self._read_manifest(mf.parent).get("playlist", {}).get("id") == playlist_id:
                mf.parent.rename(target)
                return

    def _apply_renames(self, folder: Path, known: dict, wanted: dict[str, str]) -> None:
        moves = [(tid, known[tid]["file"], wanted[tid]) for tid in known
                 if tid in wanted and known[tid]["file"] != wanted[tid] and (folder / known[tid]["file"]).exists()]
        # Duas fases para não colidir quando faixas trocam de posição.
        for tid, old, _ in moves:
            (folder / old).rename(folder / f"{old}.mv")
        for tid, old, new in moves:
            (folder / f"{old}.mv").rename(folder / new)
            known[tid]["file"] = new

    def _process(self, index: int, track: Track, dest: Path, playlist_name: str) -> Match:
        def step(status: Status, **kw) -> None:
            if self.cancel.is_set():
                raise Cancelled
            self.on_event(TrackEvent(index, track, status, **kw))

        step(Status.MATCHING)
        match = find_match(track)
        if not match:
            raise LookupError
        step(Status.DOWNLOADING, match=match)
        with tempfile.TemporaryDirectory(prefix="faixamp3-") as tmp:
            tmp = Path(tmp)
            src = audio.download(match.url, tmp)
            step(Status.CONVERTING, match=match)
            out = tmp / "out.mp3"
            audio.to_mp3(src, out, self.opts.bitrate, self.opts.normalize)
            cover = audio.cover_jpeg(track.cover_url, self.opts.cover_size) if track.cover_url else None
            # Sem álbum conhecido (dados do player público), usa a playlist: o rádio agrupa por ela.
            tagged = track if track.album else replace(track, album=playlist_name)
            audio.write_tags(out, tagged, cover, match.url)
            shutil.move(out, dest)
        return match

    @staticmethod
    def _write_missing(folder: Path, report: Report) -> None:
        path = folder / "_faltando.txt"
        lines = [f"{t}  (não encontrada)" for t in report.not_found]
        lines += [f"{t}  (erro: {msg})" for t, msg in report.failed]
        if lines:
            path.write_text("\n".join(lines) + "\n", "utf-8")
        else:
            path.unlink(missing_ok=True)
