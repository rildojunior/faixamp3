"""Cliente mínimo da Web API oficial do Spotify (OAuth Authorization Code + PKCE).

Nenhuma senha passa pelo app: o login acontece no site do Spotify e só o token
fica salvo localmente.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Iterator

import requests

from . import desktop, embed
from .config import PLAYLISTS_CACHE_FILE, TOKEN_FILE, TRACK_CACHE_FILE
from .models import Playlist, Track

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"

LIKED = "liked"

COPY_HELP = (
    "Playlists de outras pessoas com mais de 100 músicas são copiadas automaticamente pelo app do Spotify "
    "para computador (no Mac, libere o FaixaMP3 em Ajustes → Privacidade e Segurança → Acessibilidade).\n\n"
    "Se preferir fazer à mão: abra a playlist no app do Spotify, clique numa música, aperte Cmd+A e Cmd+C "
    "(Ctrl+A e Ctrl+C no Windows) e use o botão “Colar músicas” do FaixaMP3."
)


class SpotifyError(Exception):
    pass


class NotLoggedIn(SpotifyError):
    pass


class NotFound(SpotifyError):
    pass


class QuotaExceeded(SpotifyError):
    def __init__(self, retry_after: int):
        hours = max(1, round(retry_after / 3600))
        super().__init__(f"A cota diária da API do seu app no Spotify acabou (volta em ~{hours} h). "
                         "Playlists de outras pessoas e links colados continuam funcionando.")


def parse_source(text: str) -> tuple[str, str]:
    """Converte URL/URI/ID em (tipo, id). Tipos: playlist, album, track, liked."""
    text = text.strip()
    if text.lower() in {"liked", "curtidas", "favoritas"}:
        return LIKED, ""
    m = re.search(r"(playlist|album|track)[/:]([A-Za-z0-9]{22})", text)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return "playlist", text
    raise SpotifyError(f"Não reconheci '{text}' como playlist, álbum ou faixa do Spotify.")


class SpotifyClient:
    def __init__(self, client_id: str, token_path: Path = TOKEN_FILE):
        if not client_id:
            raise SpotifyError("Client ID do Spotify não configurado.")
        self.client_id = client_id
        self.token_path = token_path
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._token: dict | None = None
        self._track_cache: dict[str, Track] | None = None
        if token_path.exists():
            self._token = json.loads(token_path.read_text("utf-8"))

    # ---------- autenticação ----------

    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def login(self, open_url: Callable[[str], object] = webbrowser.open, timeout: float = 300) -> None:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if not self.path.startswith("/callback"):
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in query.items()})
                ok = "code" in result and result.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = "Login concluído. Pode fechar esta aba." if ok else "Falha no login. Volte ao app."
                self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        server.timeout = 1
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "scope": SCOPES,
        }
        open_url(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")
        deadline = time.monotonic() + timeout
        try:
            while "code" not in result and "error" not in result and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()

        if "error" in result:
            raise SpotifyError(f"Spotify recusou o login: {result['error']}")
        if "code" not in result:
            raise SpotifyError("Tempo esgotado esperando o login no navegador.")
        if result.get("state") != state:
            raise SpotifyError("Resposta de login inválida (state não confere).")

        self._store_token(
            self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": result["code"],
                    "redirect_uri": REDIRECT_URI,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                }
            )
        )

    def logout(self) -> None:
        self._token = None
        self.token_path.unlink(missing_ok=True)

    def _token_request(self, data: dict) -> dict:
        r = self.session.post(TOKEN_URL, data=data, timeout=30)
        if r.status_code != 200:
            raise SpotifyError(f"Erro ao obter token ({r.status_code}): {r.text[:200]}")
        return r.json()

    def _store_token(self, tok: dict) -> None:
        if self._token and "refresh_token" not in tok:
            tok["refresh_token"] = self._token["refresh_token"]
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        self._token = tok
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(tok), "utf-8")

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not self.logged_in:
                raise NotLoggedIn("Faça login no Spotify primeiro.")
            if force_refresh or time.time() >= self._token.get("expires_at", 0):
                self._store_token(
                    self._token_request(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self._token["refresh_token"],
                            "client_id": self.client_id,
                        }
                    )
                )
            return self._token["access_token"]

    # ---------- HTTP ----------

    def _get(self, url: str, params: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = API + url
        refreshed = False
        for _ in range(6):
            r = self.session.get(
                url, params=params, timeout=30,
                headers={"Authorization": f"Bearer {self._access_token(force_refresh=refreshed)}"},
            )
            if r.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2") or 2)
                if wait > 60:
                    raise QuotaExceeded(wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2)
                continue
            if r.status_code == 404:
                raise NotFound("Não encontrado no Spotify (link errado ou conteúdo indisponível).")
            if r.status_code == 403:
                raise SpotifyError(
                    "O Spotify negou acesso (403). Em apps no modo de desenvolvimento só é possível "
                    "ler playlists que você criou ou em que colabora."
                )
            r.raise_for_status()
            return r.json()
        raise SpotifyError("Spotify não respondeu depois de várias tentativas.")

    def _paginate(self, url: str, params: dict | None = None) -> Iterator[dict]:
        page = self._get(url, params)
        while True:
            yield from page.get("items", [])
            if not page.get("next"):
                break
            page = self._get(page["next"])

    # ---------- conversão ----------

    @staticmethod
    def _track(t: dict, album: dict | None = None) -> Track | None:
        if not t or t.get("type", "track") != "track" or t.get("is_local") or not t.get("id"):
            return None
        album = t.get("album") or album or {}
        images = album.get("images") or []
        return Track(
            id=t["id"],
            title=t.get("name", ""),
            artists=tuple(a["name"] for a in t.get("artists", []) if a.get("name")),
            album=album.get("name", ""),
            album_artist=", ".join(a["name"] for a in album.get("artists", []) if a.get("name")),
            duration_s=(t.get("duration_ms") or 0) / 1000,
            isrc=(t.get("external_ids") or {}).get("isrc"),
            track_number=t.get("track_number"),
            disc_number=t.get("disc_number"),
            release_date=album.get("release_date"),
            cover_url=max(images, key=lambda i: i.get("width") or 0)["url"] if images else None,
            explicit=bool(t.get("explicit")),
        )

    # ---------- consultas ----------

    def me(self) -> dict:
        return self._get("/me")

    def my_playlists(self) -> list[dict]:
        """Suas playlists; se a cota acabou, devolve a última lista salva."""
        try:
            out = self._my_playlists()
        except QuotaExceeded:
            if PLAYLISTS_CACHE_FILE.exists():
                return json.loads(PLAYLISTS_CACHE_FILE.read_text("utf-8"))
            raise
        PLAYLISTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLAYLISTS_CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
        return out

    def _my_playlists(self) -> list[dict]:
        me = self.me().get("id")
        out = []
        for p in self._paginate("/me/playlists", {"limit": 50}):
            if not p:
                continue
            info = p.get("items") or p.get("tracks") or {}
            owner = p.get("owner") or {}
            out.append(
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "owner": owner.get("display_name") or owner.get("id") or "",
                    "total": info.get("total", 0),
                    # Só estas o Spotify deixa ler em apps no modo de desenvolvimento.
                    "readable": owner.get("id") == me or bool(p.get("collaborative")),
                }
            )
        return out

    def playlist(self, playlist_id: str, expected_total: int = 0, use_desktop: bool = True,
                 status: Callable[[str], None] | None = None) -> Playlist:
        status = status or (lambda s: None)
        try:
            meta = self._get(f"/playlists/{playlist_id}", {"fields": "id,name,owner(display_name)"})
        except (QuotaExceeded, NotFound):
            # Cota esgotada, ou playlist do próprio Spotify (a API esconde de apps pessoais).
            return self._playlist_other({"id": playlist_id}, expected_total, use_desktop, status)
        tracks = []
        try:
            for entry in self._paginate(f"/playlists/{playlist_id}/items", {"limit": 50, "additional_types": "track"}):
                tr = self._track(entry.get("item") or entry.get("track"))
                if tr:
                    tracks.append(tr)
        except QuotaExceeded:
            return self._playlist_other(meta, expected_total, use_desktop, status)
        except SpotifyError as e:
            if "403" not in str(e):
                raise
            return self._playlist_other(meta, expected_total, use_desktop, status)
        return Playlist(meta["id"], meta.get("name", playlist_id), tracks, (meta.get("owner") or {}).get("display_name", ""))

    def _playlist_other(self, meta: dict, expected_total: int, use_desktop: bool,
                        status: Callable[[str], None]) -> Playlist:
        """Playlist de outra pessoa (a API não entrega as faixas para apps pessoais).

        1. Player público: até 100 músicas, sem precisar de nada.
        2. Se for maior: copia os links pelo app do Spotify para computador.
        Em ambos os casos os dados completos de cada faixa vêm da API oficial.
        """
        owner = (meta.get("owner") or {}).get("display_name") or "outra pessoa"
        name = meta.get("name") or meta["id"]
        basic: list[Track] = []
        try:
            status(f"Lendo “{name}” pelo player público…")
            embed_name, basic = embed.fetch_playlist(meta["id"], self.session)
            name = meta.get("name") or embed_name
        except embed.EmbedError:
            pass

        truncated = len(basic) >= embed.EMBED_LIMIT or (expected_total and len(basic) < expected_total)
        note = ""
        ids = [t.id for t in basic]
        if truncated or not basic:
            if use_desktop and desktop.available():
                try:
                    status(f"Copiando “{name}” pelo app do Spotify…")
                    ids = desktop.copy_playlist(meta["id"], expected_total)
                except desktop.DesktopError as e:
                    note = f"{e} Por enquanto, baixando só as {len(basic)} primeiras."
            else:
                note = (f"Playlist de {owner}: o Spotify só mostra as {embed.EMBED_LIMIT} primeiras músicas. "
                        "Com o app do Spotify para computador instalado, o FaixaMP3 copia todas.")
        if not ids:
            raise SpotifyError(f"Não consegui ler “{name}” ({owner}).\n\n{COPY_HELP}")
        if expected_total and len(ids) < expected_total and not note:
            note = f"Vieram {len(ids)} de {expected_total} músicas (algumas podem estar indisponíveis no Brasil)."

        fallback = {t.id: t for t in basic}
        tracks = self.tracks_by_ids(ids, fallback, status)
        return Playlist(meta["id"], name, tracks, owner, note)

    def tracks_by_ids(self, ids: list[str], fallback: dict[str, Track] | None = None,
                      status: Callable[[str], None] | None = None) -> list[Track]:
        """Dados de cada faixa pelo player público (não gasta a cota da API), com cache em disco."""
        fallback = fallback or {}
        status = status or (lambda s: None)
        with self._lock:
            if self._track_cache is None:
                try:
                    raw = json.loads(TRACK_CACHE_FILE.read_text("utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    raw = {}
                self._track_cache = {k: Track(**{**v, "artists": tuple(v["artists"])}) for k, v in raw.items()}
        cache = self._track_cache
        missing = [i for i in ids if i not in cache]
        done = 0

        def fetch(tid: str) -> None:
            nonlocal done
            try:
                cache[tid] = embed.fetch_track(tid, self.session)
            except embed.EmbedError:
                pass  # usa os dados básicos, se houver
            with self._lock:
                done += 1
                if done % 10 == 0 or done == len(missing):
                    status(f"Buscando dados das músicas: {done} de {len(missing)}…")

        if missing:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(fetch, missing))
            with self._lock:
                TRACK_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                TRACK_CACHE_FILE.write_text(
                    json.dumps({k: asdict(v) for k, v in cache.items()}, ensure_ascii=False), "utf-8")
        return [t for i in ids if (t := cache.get(i) or fallback.get(i))]

    def from_links(self, text: str, name: str, status: Callable[[str], None] | None = None) -> Playlist:
        """Playlist a partir de links de músicas colados do app do Spotify."""
        try:
            ids, _ = desktop.parse_pasted(text)
        except desktop.PasteError as e:
            raise SpotifyError(str(e)) from e
        return Playlist(f"links:{name}", name, self.tracks_by_ids(ids, status=status))

    def liked(self) -> Playlist:
        tracks = [tr for e in self._paginate("/me/tracks", {"limit": 50}) if (tr := self._track(e.get("track")))]
        return Playlist(LIKED, "Músicas Curtidas", tracks)

    def album(self, album_id: str) -> Playlist:
        alb = self._get(f"/albums/{album_id}")
        tracks = []
        for t in self._paginate(f"/albums/{album_id}/tracks", {"limit": 50}):
            if tr := self._track(t, alb):
                tracks.append(tr)
        artist = ", ".join(a["name"] for a in alb.get("artists", []))
        return Playlist(alb["id"], f"{artist} - {alb.get('name', '')}", tracks)

    def track(self, track_id: str) -> Playlist:
        tr = self._track(self._get(f"/tracks/{track_id}"))
        if not tr:
            raise SpotifyError("Faixa indisponível.")
        return Playlist(tr.id, "Faixas avulsas", [tr])

    def load(self, source: str, expected_total: int = 0, use_desktop: bool = True,
             status: Callable[[str], None] | None = None) -> Playlist:
        kind, ident = parse_source(source)
        if kind == LIKED:
            return self.liked()
        if kind == "playlist":
            return self.playlist(ident, expected_total, use_desktop, status)
        return getattr(self, kind)(ident)
