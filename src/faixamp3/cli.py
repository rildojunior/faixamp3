from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import desktop
from .config import Settings, find_tool
from .csv_source import load_csv
from .matcher import find_match
from .models import Playlist, Track
from .pipeline import Options, Status, Syncer, TrackEvent
from .spotify import NotLoggedIn, SpotifyClient, SpotifyError

console = Console()

SETUP_HELP = """[bold]Configuração única do Spotify[/bold]
1. Acesse https://developer.spotify.com/dashboard e clique em [bold]Create app[/bold].
2. Redirect URI: [cyan]http://127.0.0.1:8888/callback[/cyan]  ·  API: marque [bold]Web API[/bold].
3. Copie o [bold]Client ID[/bold] e rode:  [cyan]faixamp3 configurar --client-id SEU_ID[/cyan]
4. Depois:  [cyan]faixamp3 login[/cyan]"""


def _client(settings: Settings) -> SpotifyClient:
    if not settings.client_id:
        console.print(SETUP_HELP)
        sys.exit(1)
    return SpotifyClient(settings.client_id)


def cmd_configurar(args, settings: Settings) -> None:
    for attr, value in [("client_id", args.client_id), ("output_dir", args.pasta), ("workers", args.paralelo),
                        ("normalize", args.normalizar), ("ascii_names", args.nomes_ascii), ("bitrate", args.bitrate)]:
        if value is not None:
            setattr(settings, attr, str(Path(value).expanduser()) if attr == "output_dir" else value)
    settings.save()
    table = Table(show_header=False, box=None)
    for k, v in vars(settings).items():
        table.add_row(f"[dim]{k}[/dim]", str(v) if k != "client_id" or not v else v[:6] + "…")
    console.print(table)
    if not settings.client_id:
        console.print(SETUP_HELP)


def cmd_login(args, settings: Settings) -> None:
    client = _client(settings)
    console.print("Abrindo o navegador para login no Spotify…")
    client.login(open_url=lambda url: (__import__("webbrowser").open(url),
                                        console.print(f"[dim]Se não abrir, acesse:[/dim] {url}")))
    console.print(f"[green]✓ Conectado como {client.me().get('display_name', '?')}[/green]")


def cmd_logout(args, settings: Settings) -> None:
    _client(settings).logout()
    console.print("Sessão do Spotify removida.")


def cmd_playlists(args, settings: Settings) -> None:
    table = Table("Nome", "Faixas", "Dono", "Acesso", "ID")
    for p in sorted(_client(settings).my_playlists(), key=lambda p: not p["readable"]):
        access = "completo" if p["readable"] else ("player público" if p["total"] <= 100 else "app do Spotify")
        table.add_row(p["name"], str(p["total"]), p["owner"], access, p["id"])
    console.print(table)
    console.print("[dim]Playlists de outras pessoas com mais de 100 músicas são copiadas pelo app do Spotify "
                  "para computador (ele abre sozinho).[/dim]")


def _load_sources(sources: list[str], settings: Settings) -> list[Playlist]:
    client = None
    playlists = []
    for src in sources:
        if src.lower().endswith(".csv") and Path(src).expanduser().exists():
            playlists.append(load_csv(Path(src).expanduser()))
            continue
        client = client or _client(settings)
        status = lambda msg: console.print(f"[dim]{msg}[/dim]")  # noqa: E731
        if src.lower() == "colar":
            text = desktop.get_clipboard()
            playlists.append(client.from_links(text, f"Colado {time.strftime('%Y-%m-%d %H%M')}", status))
            continue
        if src.lower().endswith(".txt") and Path(src).expanduser().exists():
            path = Path(src).expanduser()
            playlists.append(client.from_links(path.read_text("utf-8"), path.stem, status))
            continue
        if src.lower() in {"todas", "all"}:
            for p in client.my_playlists():
                try:
                    playlists.append(client.playlist(p["id"], p["total"], settings.use_desktop, status))
                except SpotifyError as e:
                    console.print(f"[yellow]Pulando '{p['name']}': {e}[/yellow]")
            continue
        playlists.append(client.load(src, 0, settings.use_desktop, status))
    return playlists


def cmd_baixar(args, settings: Settings) -> None:
    _check_tools()
    opts = Options(
        output_dir=Path(args.pasta or settings.output_dir).expanduser(),
        workers=args.paralelo or settings.workers,
        bitrate=settings.bitrate,
        normalize=settings.normalize if args.normalizar is None else args.normalizar,
        ascii_names=settings.ascii_names if args.nomes_ascii is None else args.nomes_ascii,
        cover_size=settings.cover_size,
        prune=args.remover_ausentes,
    )
    playlists = _load_sources(args.fontes, settings)
    for pl in playlists:
        _sync_one(pl, opts)


def _sync_one(pl: Playlist, opts: Options) -> None:
    console.rule(f"[bold]{pl.name}[/bold] · {len(pl.tracks)} faixas")
    if pl.note:
        console.print(f"[yellow]{pl.note}[/yellow]")
    cancel = threading.Event()
    lock = threading.Lock()
    active: dict[int, str] = {}

    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as progress:
        task = progress.add_task("", total=len(pl.tracks))

        def on_event(ev: TrackEvent) -> None:
            with lock:
                if ev.status.finished:
                    active.pop(ev.index, None)
                    progress.advance(task)
                    icon = {Status.DONE: "[green]✓[/green]", Status.COPIED: "[cyan]⇉[/cyan]",
                            Status.SKIPPED: "[dim]·[/dim]", Status.CANCELLED: "[dim]×[/dim]"}.get(ev.status, "[red]✗[/red]")
                    if ev.status is not Status.SKIPPED:
                        extra = ""
                        if ev.match:
                            extra = f" [dim](nota {ev.match.score:.2f} via {ev.match.via})[/dim]"
                        elif ev.message:
                            extra = f" [red]{ev.message}[/red]"
                        progress.console.print(f"{icon} {ev.track}{extra}")
                elif ev.status is not Status.QUEUED:
                    active[ev.index] = f"{ev.status.value}: {ev.track.title}"
                shown = list(active.values())[:3]
                progress.update(task, description=" | ".join(s[:40] for s in shown) or "…")

        try:
            report = Syncer(opts, on_event, cancel).sync(pl)
        except KeyboardInterrupt:
            console.print("[yellow]Cancelado. O que já foi baixado fica salvo; rode de novo para continuar.[/yellow]")
            sys.exit(130)

    console.print(
        f"[bold]{report.downloaded}[/bold] baixadas · {report.copied} copiadas de outras playlists · "
        f"{report.skipped} já existiam · [red]{len(report.not_found) + len(report.failed)}[/red] com problema"
        + (f" · {report.removed} removidas" if report.removed else "")
    )
    if report.not_found or report.failed:
        console.print(f"[dim]Lista em {report.folder / '_faltando.txt'}[/dim]")
    console.print(f"[dim]Pasta: {report.folder}[/dim]")


def cmd_testar(args, settings: Settings) -> None:
    artist, _, title = args.busca.partition(" - ")
    if not title:
        console.print("Use o formato \"Artista - Música\".")
        sys.exit(1)
    track = Track(id="teste", title=title.strip(), artists=(artist.strip(),), duration_s=args.duracao or 0)
    with console.status("Procurando…"):
        m = find_match(track)
    if not m:
        console.print("[red]Nada confiável encontrado.[/red]")
        return
    console.print(f"[green]{m.artists} - {m.title}[/green] ({int(m.duration_s)}s) nota {m.score} via {m.via}\n{m.url}")


def _check_tools() -> None:
    if not find_tool("ffmpeg"):
        console.print("[red]ffmpeg não encontrado. Instale (macOS: brew install ffmpeg · Windows: winget install ffmpeg).[/red]")
        sys.exit(1)
    if not find_tool("deno"):
        console.print("[yellow]Aviso: deno não encontrado; o YouTube pode falhar. "
                      "Instale (macOS: brew install deno · Windows: winget install DenoLand.Deno).[/yellow]")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="faixamp3", description="Playlists do Spotify em MP3 para o pendrive do carro.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("configurar", help="salva Client ID e preferências")
    p.add_argument("--client-id")
    p.add_argument("--pasta", help="pasta de saída padrão")
    p.add_argument("--paralelo", type=int, help="downloads simultâneos (padrão 7)")
    p.add_argument("--bitrate", type=int, choices=[128, 192, 256, 320])
    p.add_argument("--normalizar", action=argparse.BooleanOptionalAction, default=None,
                   help="igualar volume entre faixas")
    p.add_argument("--nomes-ascii", action=argparse.BooleanOptionalAction, default=None,
                   help="tirar acentos dos nomes de arquivo (rádios antigos)")
    p.set_defaults(func=cmd_configurar)

    sub.add_parser("login", help="conectar à sua conta Spotify").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="esquecer a conta Spotify").set_defaults(func=cmd_logout)
    sub.add_parser("playlists", help="listar suas playlists").set_defaults(func=cmd_playlists)

    p = sub.add_parser("baixar", help="baixar playlists/álbuns/faixas")
    p.add_argument("fontes", nargs="+",
                   help="link do Spotify, 'curtidas', 'todas', 'colar' (links copiados do app do Spotify), .csv ou .txt")
    p.add_argument("-o", "--pasta")
    p.add_argument("-j", "--paralelo", type=int)
    p.add_argument("--normalizar", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--nomes-ascii", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--remover-ausentes", action="store_true",
                   help="apagar da pasta as faixas que saíram da playlist")
    p.set_defaults(func=cmd_baixar)

    p = sub.add_parser("testar", help="testar a busca: \"Artista - Música\"")
    p.add_argument("busca")
    p.add_argument("--duracao", type=float, help="duração esperada em segundos")
    p.set_defaults(func=cmd_testar)

    args = parser.parse_args(argv)
    try:
        args.func(args, Settings.load())
    except NotLoggedIn:
        console.print("[red]Você ainda não fez login. Rode: faixamp3 login[/red]")
        sys.exit(1)
    except (SpotifyError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
