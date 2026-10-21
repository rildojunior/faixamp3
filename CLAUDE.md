# FaixaMP3

App Python (PySide6) que baixa playlists do Spotify como MP3. Código em `src/faixamp3/`, empacotado com
PyInstaller (`packaging/build.py`) e publicado pelo workflow `.github/workflows/build.yml` ao enviar uma tag `v*`.

## Versões e releases

**Só envie para o GitHub (push, pull request, merge, tag) quando o dono do projeto pedir.** Nunca por conta
própria: ele junta várias mudanças antes de publicar. Até lá, faça commits locais numa branch e vá
acrescentando as novidades no `CHANGELOG.md`, numa seção `## Próxima versão` no topo.

Quando ele pedir para enviar, esse envio gera uma versão nova, sem precisar perguntar:

1. Suba a versão em `pyproject.toml` e `src/faixamp3/__init__.py` (e rode `uv lock` para atualizar o
   `uv.lock`). Funcionalidade nova → sobe o segundo número (0.1.1 → 0.2.0); correção ou ajuste pequeno → sobe
   o terceiro (0.2.0 → 0.2.1). Uma versão só por envio, cobrindo todas as mudanças juntadas.
2. No topo do `CHANGELOG.md`, troque `## Próxima versão` por `## <versão>` (ou crie a seção) com o resumo de
   tudo o que mudou desde a última versão, escrito para o usuário do app (o que ele vai notar), não para quem
   programa. Esse texto vira a descrição da release e aparece no aviso de atualização do app; sem ele, o
   workflow falha.
3. Commit, envie a `main` e depois a tag `v<versão>` (ex.: `git tag -a v0.2.0 -m "FaixaMP3 v0.2.0"`).
4. Acompanhe o workflow `build` até a release ter os dois arquivos (`FaixaMP3-macOS.zip` e
   `FaixaMP3-Windows.zip`).

O app empacotado se atualiza sozinho a partir da release mais recente (`src/faixamp3/updater.py`), então uma
release quebrada chega a todos os usuários: teste antes de enviar.
