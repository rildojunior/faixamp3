"""FaixaMP3: baixa e organiza playlists do Spotify como MP3 pensados para o carro."""

__version__ = "0.3.1"


def main() -> None:
    from .cli import main as cli_main

    cli_main()
