"""Atualização automática do app empacotado a partir das Releases do GitHub.

Só age no app gerado pelo PyInstaller (FaixaMP3.app no Mac, pasta FaixaMP3 no Windows). Rodando pelo
código-fonte (`uv run`), a atualização é pelo `git pull`.

Como não dá para trocar os arquivos de um app aberto, a troca é feita por um pequeno script que espera o
app fechar, substitui a instalação inteira e, se pedido, abre a versão nova.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from platformdirs import user_cache_dir

from . import __version__
from .config import APP_NAME

REPO = "rildojunior/faixamp3"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL_MS = 2 * 60 * 60 * 1000  # com o app aberto, procura de novo a cada 2 horas
UPDATE_DIR = Path(user_cache_dir(APP_NAME, appauthor=False)) / "update"


class UpdateError(Exception):
    pass


@dataclass
class Update:
    version: str
    url: str
    page: str
    notes: str = ""


def _parse(version: str) -> tuple[int, ...]:
    parts = []
    for p in version.lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def release_url(version: str = __version__) -> str:
    """Página da release no GitHub, com as novidades daquela versão."""
    return f"https://github.com/{REPO}/releases/tag/v{version}"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_path() -> Path | None:
    """O que é substituído: o FaixaMP3.app no Mac, a pasta do .exe no Windows."""
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        app = exe.parents[2]
        return app if app.suffix == ".app" else None
    if sys.platform == "win32":
        return exe.parent
    return None


def supported() -> bool:
    """Atualiza sozinho só no app empacotado e quando dá para escrever onde ele está instalado."""
    target = install_path() if frozen() else None
    return bool(target) and os.access(target.parent, os.W_OK)


def _asset_name() -> str:
    return "FaixaMP3-macOS.zip" if sys.platform == "darwin" else "FaixaMP3-Windows.zip"


def check() -> Update | None:
    """Versão mais nova publicada, ou None se esta já é a última."""
    r = requests.get(LATEST_URL, timeout=15, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    data = r.json()
    tag = data.get("tag_name", "")
    if _parse(tag) <= _parse(__version__):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == _asset_name()), None)
    if not asset:
        return None  # release ainda sem o arquivo deste sistema (build em andamento)
    notes = (data.get("body") or "").split("\n---", 1)[0].strip()  # sem o rodapé de instalação
    return Update(tag.lstrip("vV"), asset["browser_download_url"], data.get("html_url", ""), notes)


def download(update: Update) -> Path:
    """Baixa e descompacta a versão nova; devolve o caminho do app/pasta pronto para instalar."""
    shutil.rmtree(UPDATE_DIR, ignore_errors=True)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    archive = UPDATE_DIR / _asset_name()
    with requests.get(update.url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(archive, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    extracted = UPDATE_DIR / "new"
    if sys.platform == "darwin":
        # ditto preserva os links simbólicos e permissões de dentro do .app (o zipfile não).
        subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        new = extracted / "FaixaMP3.app"
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(extracted)
        new = extracted / "FaixaMP3"
    archive.unlink(missing_ok=True)
    if not new.exists():
        raise UpdateError("O arquivo baixado não tem o formato esperado.")
    return new


def apply(new: Path, relaunch: bool) -> None:
    """Agenda a troca para quando este processo fechar. Quem chama deve fechar o app em seguida."""
    target = install_path()
    if not target:
        raise UpdateError("Não foi possível localizar a instalação do app.")
    pid = os.getpid()
    if sys.platform == "darwin":
        script = Path(tempfile.gettempdir()) / "faixamp3-update.sh"
        script.write_text(f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
OLD="{target}.old"
rm -rf "$OLD"
if mv "{target}" "$OLD" && mv "{new}" "{target}"; then
    rm -rf "$OLD"
    xattr -dr com.apple.quarantine "{target}" 2>/dev/null
else
    [ -e "{target}" ] || mv "$OLD" "{target}"
fi
rm -rf "{UPDATE_DIR}"
{f'open "{target}"' if relaunch else ""}
rm -f "$0"
""")
        script.chmod(0o755)
        subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        script = Path(tempfile.gettempdir()) / "faixamp3-update.cmd"
        exe = target / Path(sys.executable).name
        script.write_text(f"""@echo off
:wait
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)
robocopy "{new}" "{target}" /MIR /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >NUL
{f'start "" "{exe}"' if relaunch else ""}
rmdir /S /Q "{new.parent}"
(goto) 2>NUL & del "%~f0"
""", encoding="mbcs")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
