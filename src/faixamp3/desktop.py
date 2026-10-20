"""Copia os links das músicas pelo app do Spotify para computador.

No app oficial, selecionar todas as músicas (Cmd/Ctrl+A) e copiar (Cmd/Ctrl+C)
coloca o link de cada faixa na área de transferência, sem limite de quantidade.
Este módulo automatiza esses atalhos e devolve a área de transferência como estava.

macOS: o app que roda isto precisa de permissão em Ajustes do Sistema →
Privacidade e Segurança → Acessibilidade.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

TRACK_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?track/|spotify:track:)([A-Za-z0-9]{22})")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class DesktopError(Exception):
    pass


_LINE_TRACK = re.compile(
    r"^(?:https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]{22})(?:\?\S*)?|spotify:track:([A-Za-z0-9]{22}))$")
# Itens que o Spotify também copia de playlists e que não são músicas baixáveis.
_LINE_SKIP = re.compile(r"^(?:https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(?:local|episode)/|spotify:(?:local|episode):)\S*$")
_LINE_OTHER_SPOTIFY = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|artist)/|spotify:(playlist|album|artist):")


class PasteError(ValueError):
    pass


def parse_pasted(text: str) -> tuple[list[str], int]:
    """Aceita só músicas copiadas do Spotify: uma por linha, nada além disso.

    Retorna (IDs sem repetir, quantos itens foram ignorados por serem arquivos
    locais ou episódios de podcast).
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        raise PasteError("A área de transferência está vazia.")
    ids: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for n, line in enumerate(lines, 1):
        m = _LINE_TRACK.match(line)
        if m:
            tid = m.group(1) or m.group(2)
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
        elif _LINE_SKIP.match(line):
            skipped += 1
        else:
            other = _LINE_OTHER_SPOTIFY.search(line)
            if other and len(lines) == 1:
                raise PasteError("Isso é um link de playlist/álbum/artista, não músicas copiadas. "
                                 "Cole-o no campo de link e clique em Adicionar.")
            # Não mostra o conteúdo: pode ser algo sensível copiado por engano.
            where = f" (a linha {n} não é uma música)" if len(lines) > 1 else ""
            raise PasteError(f"A área de transferência não contém só músicas do Spotify{where}.")
    if not ids:
        raise PasteError("Nenhuma música encontrada (só arquivos locais ou podcasts).")
    return ids, skipped


def track_ids(text: str) -> list[str]:
    """IDs de faixa em ordem, sem repetir."""
    seen: set[str] = set()
    return [i for i in TRACK_RE.findall(text) if not (i in seen or seen.add(i))]


def available() -> bool:
    if sys.platform == "darwin":
        return Path("/Applications/Spotify.app").exists() or (Path.home() / "Applications/Spotify.app").exists()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", ""))
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        return (appdata / "Spotify" / "Spotify.exe").exists() or (local / "Microsoft" / "WindowsApps" / "Spotify.exe").exists()
    return False


def _run(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input_text, capture_output=True, text=True, encoding="utf-8",
                          creationflags=_NO_WINDOW, timeout=60)


def _ps(script: str) -> subprocess.CompletedProcess:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def get_clipboard() -> str:
    if sys.platform == "darwin":
        return _run(["pbpaste"]).stdout
    return _ps("Get-Clipboard -Raw").stdout or ""


def set_clipboard(text: str) -> None:
    if sys.platform == "darwin":
        _run(["pbcopy"], text)
    else:
        # Vazio vira um espaço: Set-Clipboard não aceita texto vazio.
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"], text or " ")


def _is_running() -> bool:
    if sys.platform == "darwin":
        return _run(["pgrep", "-x", "Spotify"]).returncode == 0
    return "Spotify.exe" in _run(["tasklist", "/FI", "IMAGENAME eq Spotify.exe"]).stdout


def _open_uri(uri: str) -> None:
    if sys.platform == "darwin":
        _run(["open", uri])
    else:
        os.startfile(uri)  # noqa: S606 - abre o protocolo spotify: no app oficial


def _select_all_and_copy() -> None:
    if sys.platform == "darwin":
        proc = _run([
            "osascript",
            "-e", 'tell application "Spotify" to activate',
            "-e", "delay 0.8",
            "-e", 'tell application "System Events" to keystroke "a" using command down',
            "-e", "delay 0.8",
            "-e", 'tell application "System Events" to keystroke "c" using command down',
        ])
        if proc.returncode != 0:
            if "1002" in proc.stderr or "permiss" in proc.stderr.lower() or "not allowed" in proc.stderr.lower():
                raise DesktopError(
                    "O macOS bloqueou o atalho de teclado. Libere o FaixaMP3 em Ajustes do Sistema → "
                    "Privacidade e Segurança → Acessibilidade e tente de novo."
                )
            raise DesktopError(f"Não consegui controlar o Spotify: {proc.stderr.strip()[:200]}")
    else:
        proc = _ps(
            "$w = New-Object -ComObject WScript.Shell; "
            "$p = Get-Process Spotify -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle } | Select-Object -First 1; "
            "if (-not $p) { exit 2 }; "
            "[void]$w.AppActivate($p.Id); Start-Sleep -Milliseconds 800; "
            "$w.SendKeys('^a'); Start-Sleep -Milliseconds 800; $w.SendKeys('^c')"
        )
        if proc.returncode != 0:
            raise DesktopError("Não encontrei a janela do Spotify aberta.")


def copy_playlist(playlist_id: str, expected: int = 0, load_wait: float | None = None) -> list[str]:
    """Abre a playlist no app do Spotify, copia todas as músicas e devolve os IDs."""
    if not available():
        raise DesktopError("App do Spotify para computador não encontrado.")
    backup = get_clipboard()
    try:
        was_running = _is_running()
        _open_uri(f"spotify:playlist:{playlist_id}")
        time.sleep(load_wait if load_wait is not None else (5 if was_running else 12))
        best: list[str] = []
        for attempt in range(3):
            set_clipboard("")
            _select_all_and_copy()
            deadline = time.monotonic() + 5
            ids: list[str] = []
            while time.monotonic() < deadline:
                time.sleep(0.5)
                ids = track_ids(get_clipboard())
                if ids:
                    break
            if len(ids) > len(best):
                best = ids
            if best and (not expected or len(best) >= expected * 0.95):
                break
            time.sleep(3)  # playlist ainda carregando: espera e tenta de novo
        if not best:
            raise DesktopError("O Spotify não copiou nenhuma música (a lista de faixas não estava em foco).")
        return best
    finally:
        set_clipboard(backup)
