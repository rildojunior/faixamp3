"""Interface gráfica (Qt 6): mesma base de código para macOS e Windows."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import requests

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QInputDialog, QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .config import Settings, find_tool
from .csv_source import load_csv
from .pipeline import Options, Report, Status, Syncer, TrackEvent
from . import desktop, updater
from .embed import EMBED_LIMIT, fetch_title
from .spotify import COPY_HELP, QuotaExceeded, LIKED, REDIRECT_URI, SpotifyClient, SpotifyError, parse_source

STATUS_COLORS = {
    Status.DONE: "#2e9d4f", Status.COPIED: "#2a7fd4", Status.SKIPPED: "#8a8a8a",
    Status.NOT_FOUND: "#d9822b", Status.FAILED: "#d64545", Status.CANCELLED: "#8a8a8a",
}
SOURCE_ROLE = Qt.UserRole
ADDED_HEADER = "Adicionadas por você"


def friendly_error(e: BaseException) -> str:
    """Explica o erro em palavras simples; as mensagens do próprio app já vêm prontas."""
    if isinstance(e, SpotifyError):
        return str(e)
    chain = []
    while e is not None and e not in chain:
        chain.append(e)
        e = e.__cause__ or e.__context__
    if any(isinstance(x, requests.Timeout) for x in chain):
        return ("O Spotify demorou demais para responder. Pode ser a internet lenta ou instável. "
                "Tente de novo daqui a pouco.")
    if any(isinstance(x, (requests.ConnectionError, ConnectionError)) for x in chain):
        return "A conexão com a internet caiu ou está instável. Confira se você está conectado e tente de novo."
    http = next((x for x in chain if isinstance(x, requests.HTTPError)), None)
    if http is not None and http.response is not None:
        return (f"O Spotify não atendeu o pedido agora (erro {http.response.status_code}). "
                "Tente de novo daqui a pouco.")
    return f"Algo deu errado: {chain[0]}"


class Bridge(QObject):
    """Leva eventos das threads de trabalho para a thread da interface."""

    playlists_loaded = Signal(list)
    logged_in = Signal(str)
    playlist_started = Signal(int, str, list)
    note = Signal(str)
    status = Signal(str)
    track_event = Signal(int, object)
    playlist_done = Signal(int, object)
    all_done = Signal()
    error = Signal(str)
    update_ready = Signal(str, str, str)  # versão, caminho do app novo, novidades
    update_message = Signal(str)  # resposta da busca manual por atualizações
    link_named = Signal(str, str)  # link adicionado, nome dele no Spotify


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configurações")
        self.settings = settings
        form = QFormLayout()

        help_label = QLabel(
            "Para ler suas playlists, crie um app gratuito em "
            "<a href='https://developer.spotify.com/dashboard'>developer.spotify.com/dashboard</a>.<br>"
            f"Redirect URI: <b>{REDIRECT_URI}</b> · API: <b>Web API</b>. Depois cole o Client ID abaixo."
        )
        help_label.setOpenExternalLinks(True)
        help_label.setWordWrap(True)
        help_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        form.addRow(help_label)

        self.client_id = QLineEdit(settings.client_id)
        self.client_id.setPlaceholderText("Client ID do seu app no Spotify")
        form.addRow("Client ID:", self.client_id)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings.output_dir)
        browse = QPushButton("Escolher…")
        browse.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder)
        folder_row.addWidget(browse)
        form.addRow("Pasta das músicas:", folder_row)

        self.workers = QSpinBox(minimum=1, maximum=12, value=settings.workers)
        form.addRow("Downloads simultâneos:", self.workers)

        self.bitrate = QComboBox()
        for b in (192, 256, 320):
            self.bitrate.addItem(f"{b} kbps", b)
        self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(settings.bitrate)))
        form.addRow("Qualidade do MP3:", self.bitrate)

        self.normalize = QCheckBox("Igualar o volume entre as músicas")
        self.normalize.setChecked(settings.normalize)
        form.addRow("", self.normalize)
        self.ascii = QCheckBox("Tirar acentos dos nomes de arquivo (rádios antigos)")
        self.ascii.setChecked(settings.ascii_names)
        form.addRow("", self.ascii)
        self.use_desktop = QCheckBox("Copiar playlists grandes de outras pessoas pelo app do Spotify")
        self.use_desktop.setToolTip("O player público mostra só 100 músicas. Com isto, o FaixaMP3 abre a playlist "
                                    "no app do Spotify e copia todas (no Mac exige permissão de Acessibilidade).")
        self.use_desktop.setChecked(settings.use_desktop)
        form.addRow("", self.use_desktop)

        version_row = QHBoxLayout()
        version_row.addWidget(QLabel(f"Versão {__version__}"))
        version_row.addStretch()
        check_btn = QPushButton("Procurar atualizações")
        news_btn = QPushButton("Novidades")
        news_btn.setToolTip("Abre no navegador as novidades desta versão.")
        if parent is not None:
            check_btn.clicked.connect(lambda: parent._check_update(manual=True))
        else:
            check_btn.setEnabled(False)
        news_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updater.release_url())))
        version_row.addWidget(check_btn)
        version_row.addWidget(news_btn)
        form.addRow("Atualizações:", version_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(560, 0)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Pasta das músicas", self.folder.text())
        if path:
            self.folder.setText(path)

    def accept(self):
        s = self.settings
        s.client_id = self.client_id.text().strip()
        s.output_dir = self.folder.text().strip() or s.output_dir
        s.workers = self.workers.value()
        s.bitrate = self.bitrate.currentData()
        s.normalize = self.normalize.isChecked()
        s.ascii_names = self.ascii.isChecked()
        s.use_desktop = self.use_desktop.isChecked()
        s.save()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FaixaMP3")
        self.resize(1100, 680)
        self.settings = Settings.load()
        self.client: SpotifyClient | None = None
        self.bridge = Bridge()
        self.cancel = threading.Event()
        self.running = False
        self.rows: dict[tuple[int, int], int] = {}
        self.done_count = 0
        self.reports: list[Report] = []
        self.notes: list[str] = []
        self.pasted: dict[str, str] = {}
        self.pasted_for: dict[str, str] = {}  # ID da playlist -> links colados dela
        self._last_playlists: list[dict] = []
        self.pending_update: tuple[str, str, str] | None = None
        self.checking_update = False

        self._build_ui()
        self._connect_signals()
        self._init_client()
        self._check_update()
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._check_update)
        self.update_timer.start(updater.CHECK_INTERVAL_MS)

    # ---------- interface ----------

    def _build_ui(self):
        # Barra superior: conta e configurações.
        self.account_label = QLabel()
        self.login_btn = QPushButton("Conectar ao Spotify")
        self.login_btn.clicked.connect(self._login)
        settings_btn = QPushButton("Configurações")
        settings_btn.clicked.connect(self._open_settings)
        top = QHBoxLayout()
        top.addWidget(self.account_label, 1)
        top.addWidget(self.login_btn)
        top.addWidget(settings_btn)

        # Esquerda: fontes.
        self.sources = QListWidget()
        self.sources.setAlternatingRowColors(True)
        self.sources.itemDoubleClicked.connect(self._on_source_double_clicked)
        self.sources.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sources.customContextMenuRequested.connect(self._source_menu)
        self.sources.installEventFilter(self)
        self.sources.currentItemChanged.connect(lambda *_: self._update_remove_btn())
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("Cole um link de playlist, álbum ou faixa do Spotify")
        self.link_input.returnPressed.connect(self._add_link)
        add_btn = QPushButton("Adicionar")
        add_btn.clicked.connect(self._add_link)
        csv_btn = QPushButton("Abrir CSV…")
        csv_btn.setToolTip("Para playlists de outras pessoas: exporte em exportify.app")
        csv_btn.clicked.connect(self._add_csv)
        paste_btn = QPushButton("Colar músicas")
        paste_btn.setToolTip("No app do Spotify: abra a playlist, clique numa música, Cmd/Ctrl+A e Cmd/Ctrl+C. "
                             "Depois clique aqui.")
        paste_btn.clicked.connect(self._add_pasted)
        refresh_btn = QPushButton("Recarregar")
        refresh_btn.clicked.connect(self._load_playlists)
        link_row = QHBoxLayout()
        link_row.addWidget(self.link_input, 1)
        link_row.addWidget(add_btn)
        btn_row = QHBoxLayout()
        btn_row.addWidget(paste_btn)
        btn_row.addWidget(csv_btn)
        btn_row.addWidget(refresh_btn)
        self.remove_btn = QPushButton("Remover")
        self.remove_btn.setToolTip("Remove da lista o item adicionado por você (atalho: Delete)")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(lambda: self._remove_source(self.sources.currentItem()))
        btn_row.addWidget(self.remove_btn)
        btn_row.addStretch()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>Playlists</b> — marque as que quer baixar"))
        left_layout.addWidget(self.sources, 1)
        left_layout.addLayout(link_row)
        left_layout.addLayout(btn_row)

        # Direita: fila.
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Playlist", "#", "Música", "Situação", "Detalhe"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(4, 220)
        self.progress = QProgressBar()
        self.progress.setFormat("%v de %m")
        self.summary = QLabel()
        self.start_btn = QPushButton("Baixar selecionadas")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        open_btn = QPushButton("Abrir pasta")
        open_btn.clicked.connect(self._open_folder)
        self.clear_btn = QPushButton("Limpar lista")
        self.clear_btn.setToolTip("Tira da lista as músicas do último download (os arquivos continuam na pasta)")
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self._clear_queue)
        actions = QHBoxLayout()
        actions.addWidget(self.summary, 1)
        actions.addWidget(self.clear_btn)
        actions.addWidget(open_btn)
        actions.addWidget(self.cancel_btn)
        actions.addWidget(self.start_btn)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.table, 1)
        right_layout.addWidget(self.progress)
        right_layout.addLayout(actions)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([340, 760])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _connect_signals(self):
        b = self.bridge
        b.playlists_loaded.connect(self._on_playlists)
        b.logged_in.connect(self._on_logged_in)
        b.playlist_started.connect(self._on_playlist_started)
        b.note.connect(self.notes.append)
        b.status.connect(self.summary.setText)
        b.track_event.connect(self._on_track_event)
        b.playlist_done.connect(self._on_playlist_done)
        b.all_done.connect(self._on_all_done)
        b.error.connect(self._on_error)
        b.update_ready.connect(self._on_update_ready)
        b.update_message.connect(lambda text: QMessageBox.information(self, "Atualização", text))
        b.link_named.connect(self._on_link_named)

    # ---------- Spotify ----------

    def _init_client(self):
        self.client = SpotifyClient(self.settings.client_id) if self.settings.client_id else None
        if not self.client:
            self.account_label.setText("Spotify não configurado")
            self.login_btn.setEnabled(False)
            self._fill_sources([])
        elif self.client.logged_in:
            self.account_label.setText("Conectando…")
            self.login_btn.setText("Trocar conta")
            self._load_playlists()
        else:
            self.account_label.setText("Desconectado")
            self.login_btn.setEnabled(True)
            self._fill_sources([])

    def _run_bg(self, fn):
        def wrapper():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.bridge.error.emit(friendly_error(e))
        threading.Thread(target=wrapper, daemon=True).start()

    def _login(self):
        if not self.client:
            return
        self.login_btn.setEnabled(False)
        self.account_label.setText("Conclua o login no navegador…")

        def work():
            self.client.login()
            self.bridge.logged_in.emit(self.client.me().get("display_name") or "")
        self._run_bg(work)

    def _on_logged_in(self, name: str):
        self.login_btn.setEnabled(True)
        self.login_btn.setText("Trocar conta")
        self._load_playlists()

    def _load_playlists(self):
        if not self.client or not self.client.logged_in:
            return

        def work():
            try:
                name = self.client.me().get("display_name") or ""
            except QuotaExceeded as e:
                name = "(cota da API esgotada; usando a última lista salva)"
                self.bridge.status.emit(str(e))
            pls = self.client.my_playlists()
            self.bridge.playlists_loaded.emit([name, pls])
        self._run_bg(work)

    def _on_playlists(self, payload: list):
        name, playlists = payload
        self._last_playlists = playlists
        self.account_label.setText(f"Conectado como <b>{name}</b>")
        self._fill_sources(playlists)

    def _fill_sources(self, playlists: list[dict]):
        extra = [self.sources.item(i) for i in range(self.sources.count())
                 if self.sources.item(i).data(SOURCE_ROLE + 1) == "extra"]
        extras = [(it.text(), it.data(SOURCE_ROLE), it.checkState(), it.toolTip()) for it in extra]
        self.sources.clear()
        for text, src, state, tip in extras:
            self._add_source_item(text, src, tooltip=tip, extra=True, checked=state == Qt.Checked)
        if self.client and self.client.logged_in:
            self._add_header("Suas playlists")
            self._add_source_item("♥ Músicas Curtidas", LIKED)
        for p in playlists:
            if p.get("readable", True):
                self._add_source_item(f"{p['name']}  ({p['total']})", p["id"], total=p["total"], name=p["name"])
        others = [p for p in playlists if not p.get("readable", True)]
        if others:
            self._add_header("De outras pessoas (pelo player público)")
            for p in others:
                limited = p["total"] > EMBED_LIMIT and not (self.settings.use_desktop and desktop.available())
                text = f"{p['name']}  ({p['total']}) · {p['owner']}"
                if limited:
                    text = f"⚠︎ {text} · só {EMBED_LIMIT}"
                tip = f"Dono: {p['owner']}."
                if limited:
                    tip += f" O Spotify mostra só as {EMBED_LIMIT} primeiras músicas; dê dois cliques para ver como baixar todas."
                if p["total"] > EMBED_LIMIT and not limited:
                    tip += " Mais de 100 músicas: será copiada pelo app do Spotify (ele vai abrir sozinho)."
                self._add_source_item(text, p["id"], tooltip=tip, total=p["total"], name=p["name"])
                if limited:
                    self.sources.item(self.sources.count() - 1).setData(SOURCE_ROLE + 1, "limited")

    def _add_header(self, text: str, row: int | None = None) -> QListWidgetItem:
        item = QListWidgetItem(text)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.NoItemFlags)
        item.setData(SOURCE_ROLE + 1, "header")
        if row is None:
            self.sources.addItem(item)
        else:
            self.sources.insertItem(row, item)
        return item

    def _extras_insert_row(self) -> int:
        """Linha para um item adicionado pelo usuário: seção "Adicionadas por você", no topo."""
        first = self.sources.item(0)
        if not (first and first.data(SOURCE_ROLE + 1) == "header" and first.text() == ADDED_HEADER):
            self._add_header(ADDED_HEADER, row=0)
        row = 1
        while row < self.sources.count() and self.sources.item(row).data(SOURCE_ROLE + 1) == "extra":
            row += 1
        return row

    def _show_added(self, item: QListWidgetItem, message: str):
        self.sources.setCurrentItem(item)
        self.sources.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self.summary.setText(message)

    def _removable(self, item: QListWidgetItem | None) -> bool:
        return bool(item) and not self.running and (
            item.data(SOURCE_ROLE + 1) == "extra" or item.data(SOURCE_ROLE) in self.pasted_for)

    def _update_remove_btn(self):
        item = self.sources.currentItem()
        self.remove_btn.setEnabled(self._removable(item))
        self.remove_btn.setText("Desfazer colagem" if item and item.data(SOURCE_ROLE) in self.pasted_for else "Remover")

    def _source_menu(self, pos):
        item = self.sources.itemAt(pos)
        if not self._removable(item):
            return
        menu = QMenu(self)
        label = "Desfazer músicas coladas" if item.data(SOURCE_ROLE) in self.pasted_for else "Remover da lista"
        menu.addAction(label, lambda: self._remove_source(item))
        menu.exec(self.sources.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, event):
        if obj is self.sources and event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._remove_source(self.sources.currentItem())
            return True
        return super().eventFilter(obj, event)

    def _remove_source(self, item: QListWidgetItem | None):
        if not self._removable(item):
            return
        source = item.data(SOURCE_ROLE)
        if source in self.pasted_for:
            # Volta a ser a playlist normal (sem as músicas coladas).
            self.pasted_for.pop(source)
            self._fill_sources(self._last_playlists)
            self.summary.setText("Músicas coladas descartadas.")
            return
        if str(source).startswith("links:"):
            self.pasted.pop(source[6:], None)
        name = item.text()
        self.sources.takeItem(self.sources.row(item))
        # Some com o título da seção se ela ficou vazia.
        first, second = self.sources.item(0), self.sources.item(1)
        if first and first.text() == ADDED_HEADER and not (second and second.data(SOURCE_ROLE + 1) == "extra"):
            self.sources.takeItem(0)
        self.summary.setText(f"Removido: {name}")
        self._update_remove_btn()

    def _on_source_double_clicked(self, item: QListWidgetItem):
        if item.data(SOURCE_ROLE + 1) == "limited":
            QMessageBox.information(self, "Playlist de outra pessoa", COPY_HELP)

    def _add_source_item(self, text: str, source: str, tooltip: str = "", extra: bool = False, checked: bool = False,
                         total: int = 0, name: str = ""):
        if source in self.pasted_for:
            text, checked = f"📋 {text.lstrip('⚠︎ ')}", True
            tooltip = f"Usando as {len(desktop.track_ids(self.pasted_for[source]))} músicas coladas."
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        item.setData(SOURCE_ROLE, source)
        item.setData(SOURCE_ROLE + 1, "extra" if extra else "")
        item.setData(SOURCE_ROLE + 2, total)
        item.setData(SOURCE_ROLE + 3, name or text)
        if tooltip:
            item.setToolTip(tooltip)
        if extra:
            self.sources.insertItem(self._extras_insert_row(), item)
        else:
            self.sources.addItem(item)
        return item

    def _add_link(self):
        text = self.link_input.text().strip()
        if not text:
            return
        try:
            kind, ident = parse_source(text)
        except SpotifyError as e:
            QMessageBox.warning(self, "Link inválido", str(e))
            return
        labels = {"playlist": "Playlist", "album": "Álbum", "track": "Faixa"}
        item = self._add_source_item(f"🔗 {labels.get(kind, kind)} {ident[:8]}…", text, tooltip=text, extra=True,
                                     checked=True)
        self.link_input.clear()
        self._show_added(item, "Link adicionado no topo da lista. Clique em Baixar selecionadas.")

        def work():
            # Troca o código do link pelo nome de verdade; se falhar, fica o código mesmo.
            try:
                title = fetch_title(kind, ident)
            except Exception:  # noqa: BLE001
                return
            if title:
                self.bridge.link_named.emit(text, f"🔗 {labels.get(kind, kind)}: {title}")
        threading.Thread(target=work, daemon=True).start()

    def _on_link_named(self, source: str, label: str):
        for i in range(self.sources.count()):
            it = self.sources.item(i)
            if it.data(SOURCE_ROLE + 1) == "extra" and it.data(SOURCE_ROLE) == source:
                it.setText(label)
                it.setData(SOURCE_ROLE + 3, label)

    def _add_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir playlist em CSV", str(Path.home()), "CSV (*.csv)")
        if path:
            item = self._add_source_item(f"📄 {Path(path).stem}", path, tooltip=path, extra=True, checked=True)
            self._show_added(item, f"“{Path(path).stem}” adicionada no topo da lista. Clique em Baixar selecionadas.")

    def _paste_preview(self, text: str) -> str:
        """Nomes das primeiras músicas coladas, para conferir antes de confirmar."""
        if not self.client:
            return ""
        ids = desktop.track_ids(text)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            first = self.client.tracks_by_ids(ids[:3])
        except Exception:  # noqa: BLE001 - a prévia é só uma ajuda
            first = []
        finally:
            QApplication.restoreOverrideCursor()
        if not first:
            return ""
        lines = "<br>".join(f"• {t}" for t in first)
        more = f"<br><i>…e mais {len(ids) - len(first)}</i>" if len(ids) > len(first) else ""
        return f"<span style='color:gray'>{lines}{more}</span>"

    def _add_pasted(self):
        text = QApplication.clipboard().text()
        try:
            ids, skipped = desktop.parse_pasted(text)
        except desktop.PasteError as e:
            QMessageBox.warning(self, "Não dá para colar", f"{e}\n\n{COPY_HELP.split(chr(10) * 2)[-1]}")
            return
        count = len(ids)
        items = [self.sources.item(i) for i in range(self.sources.count())]
        playlists = [it for it in items if it.flags() & Qt.ItemIsUserCheckable
                     and it.data(SOURCE_ROLE) not in (LIKED, None)
                     and not str(it.data(SOURCE_ROLE)).startswith("links:")
                     and not str(it.data(SOURCE_ROLE)).lower().endswith(".csv")]
        # Palpite: mesma quantidade de músicas; senão, a playlist grande marcada.
        guess = next((it for it in playlists if it.data(SOURCE_ROLE + 2) == count), None) or next(
            (it for it in playlists if it.checkState() == Qt.Checked and (it.data(SOURCE_ROLE + 2) or 0) > EMBED_LIMIT),
            None)

        dlg = QDialog(self)
        dlg.setWindowTitle("Colar músicas")
        combo = QComboBox()
        combo.addItem("Nova playlist…", None)
        for it in playlists:
            combo.addItem(f"{it.data(SOURCE_ROLE + 3)}  ({it.data(SOURCE_ROLE + 2)})", it.data(SOURCE_ROLE))
        if guess:
            combo.setCurrentIndex(combo.findData(guess.data(SOURCE_ROLE)))
        new_name = QLineEdit()
        new_name.setPlaceholderText("Nome da pasta")
        new_name.setEnabled(combo.currentData() is None)
        combo.currentIndexChanged.connect(lambda _: new_name.setEnabled(combo.currentData() is None))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"<b>{count} músicas</b> copiadas. De qual playlist são?"))
        if skipped:
            lay.addWidget(QLabel(f"<span style='color:gray'>{skipped} itens ignorados "
                                 "(arquivos locais ou podcasts).</span>"))
        preview = self._paste_preview(text)
        if preview:
            lay.addWidget(QLabel(preview))
        lay.addWidget(combo)
        lay.addWidget(new_name)
        lay.addWidget(buttons)
        dlg.resize(460, 0)
        if not dlg.exec():
            return

        source = combo.currentData()
        if source is None:
            name = new_name.text().strip()
            if not name:
                return
            self.pasted[name] = text
            item = self._add_source_item(f"📋 {name}  ({count})", f"links:{name}", extra=True, checked=True)
            self._show_added(item, f"“{name}” adicionada no topo da lista ({count} músicas). Clique em Baixar selecionadas.")
            return
        # As músicas coladas viram o conteúdo da playlist: mesma pasta, sem abrir o Spotify.
        self.pasted_for[source] = text
        it = next(i for i in playlists if i.data(SOURCE_ROLE) == source)
        it.setText(f"📋 {it.data(SOURCE_ROLE + 3)}  ({count})")
        it.setToolTip(f"Usando as {count} músicas coladas.")
        it.setCheckState(Qt.Checked)
        self._show_added(it, f"“{it.data(SOURCE_ROLE + 3)}” vai usar as {count} músicas coladas. Clique em Baixar selecionadas.")

    def _open_settings(self):
        if SettingsDialog(self.settings, self).exec():
            self._init_client()

    # ---------- atualização ----------

    def _check_update(self, manual: bool = False):
        """Procura versão nova. Sozinho (ao abrir e a cada 2 h) fica quieto; pelo botão, sempre responde."""
        if self.pending_update:
            if manual:
                self._offer_update()
            return
        if self.checking_update or not (manual or updater.supported()):
            return
        self.checking_update = True

        def work():
            try:
                update = updater.check()
                if not update:
                    if manual:
                        self.bridge.update_message.emit(f"Você já está na versão mais nova ({__version__}).")
                elif updater.supported():
                    if manual:
                        self.bridge.status.emit(f"Baixando a versão {update.version}…")
                    self.bridge.update_ready.emit(update.version, str(updater.download(update)), update.notes)
                elif manual:
                    self.bridge.update_message.emit(
                        f"A versão {update.version} está disponível, mas este app não consegue se atualizar "
                        f"sozinho. Baixe em {update.page}")
            except Exception:  # noqa: BLE001 - sem internet ou GitHub fora: tenta na próxima vez
                if manual:
                    self.bridge.update_message.emit("Não foi possível procurar atualizações. Confira a internet "
                                                    "e tente de novo.")
            finally:
                self.checking_update = False
        threading.Thread(target=work, daemon=True).start()

    def _on_update_ready(self, version: str, path: str, notes: str):
        self.pending_update = (version, path, notes)
        if not self.running:
            self._offer_update()

    def _offer_update(self):
        version, _, notes = self.pending_update
        box = QMessageBox(self)
        box.setWindowTitle("Atualização")
        box.setTextFormat(Qt.MarkdownText)
        box.setText(f"**A versão {version} do FaixaMP3 está pronta.**"
                    + (f"\n\n**Novidades:**\n\n{notes}" if notes else ""))
        box.setInformativeText("Reiniciar agora para usar? Se preferir, ela é instalada quando você fechar o app.")
        now = box.addButton("Reiniciar agora", QMessageBox.AcceptRole)
        box.addButton("Depois", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is now:
            self._install_update(relaunch=True)
            self.close()

    def _install_update(self, relaunch: bool):
        if not self.pending_update:
            return
        try:
            updater.apply(Path(self.pending_update[1]), relaunch)
        except Exception:  # noqa: BLE001 - a versão atual continua funcionando
            pass
        self.pending_update = None

    # ---------- download ----------

    def _selected_sources(self) -> list[tuple[str, int, str]]:
        items = [self.sources.item(i) for i in range(self.sources.count())]
        return [(it.data(SOURCE_ROLE), it.data(SOURCE_ROLE + 2) or 0, it.data(SOURCE_ROLE + 3) or "") for it in items
                if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked]

    def _start(self):
        sources = self._selected_sources()
        if not sources:
            QMessageBox.information(self, "Nada selecionado", "Marque ao menos uma playlist.")
            return
        if not find_tool("ffmpeg"):
            QMessageBox.critical(self, "ffmpeg não encontrado",
                                 "Instale o ffmpeg (macOS: brew install ffmpeg · Windows: winget install ffmpeg).")
            return
        opts = Options(
            output_dir=Path(self.settings.output_dir).expanduser(),
            workers=self.settings.workers, bitrate=self.settings.bitrate,
            normalize=self.settings.normalize, ascii_names=self.settings.ascii_names,
            cover_size=self.settings.cover_size,
        )
        self.table.setRowCount(0)
        self.rows.clear()
        self.reports.clear()
        self.notes.clear()
        self.done_count = 0
        self.progress.setRange(0, 0)
        self.summary.setText("Lendo playlists…")
        self.cancel.clear()
        self._set_running(True)

        pasted_for = dict(self.pasted_for)

        def work():
            try:
                for pi, (src, total, item_name) in enumerate(sources):
                    if self.cancel.is_set():
                        break
                    try:
                        if src.lower().endswith(".csv"):
                            pl = load_csv(src)
                        elif not self.client:
                            raise SpotifyError("Conecte sua conta Spotify primeiro.")
                        elif src in pasted_for:
                            pl = self.client.from_links(pasted_for[src], item_name, self.bridge.status.emit)
                            pl.id = src  # mesma playlist: mesma pasta e manifesto nas próximas vezes
                        elif src.startswith("links:"):
                            pl = self.client.from_links(self.pasted[src[6:]], src[6:], self.bridge.status.emit)
                        elif self.client:
                            pl = self.client.load(src, total, self.settings.use_desktop, self.bridge.status.emit)
                        else:
                            raise SpotifyError("Conecte sua conta Spotify primeiro.")
                    except Exception as e:  # noqa: BLE001
                        self.bridge.error.emit(f"Não deu para baixar “{item_name or src}”.\n\n{friendly_error(e)}\n\n"
                                              "Ao tentar de novo, as músicas que já baixaram são puladas.")
                        continue
                    seen: set[str] = set()
                    pl.tracks = [t for t in pl.tracks if not (t.id in seen or seen.add(t.id))]
                    if pl.note:
                        self.bridge.note.emit(f"• {pl.name}: {pl.note}")
                    self.bridge.playlist_started.emit(pi, pl.name, [str(t) for t in pl.tracks])
                    syncer = Syncer(opts, lambda ev, pi=pi: self.bridge.track_event.emit(pi, ev), self.cancel)
                    self.bridge.playlist_done.emit(pi, syncer.sync(pl))
            finally:
                self.bridge.all_done.emit()
        threading.Thread(target=work, daemon=True).start()

    def _set_running(self, running: bool):
        self.running = running
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.sources.setEnabled(not running)
        self.clear_btn.setEnabled(not running and self.table.rowCount() > 0)
        self._update_remove_btn()

    def _clear_queue(self):
        if self.running:
            return
        self.table.setRowCount(0)
        self.rows.clear()
        self.done_count = 0
        self.progress.reset()
        self.summary.clear()
        self.summary.setToolTip("")
        self.clear_btn.setEnabled(False)

    def _cancel(self):
        self.cancel.set()
        self.cancel_btn.setEnabled(False)
        self.summary.setText("Cancelando… (terminando as faixas em andamento)")

    def _on_playlist_started(self, pi: int, name: str, titles: list[str]):
        total = len(self.rows) + len(titles)
        for ti, title in enumerate(titles):
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, text in enumerate([name, str(ti + 1), title, "", ""]):
                self.table.setItem(row, col, QTableWidgetItem(text))
            self.rows[(pi, ti)] = row
        self.progress.setRange(0, total)
        self.progress.setValue(self.done_count)
        self.summary.setText(f"Baixando “{name}”…")

    def _on_track_event(self, pi: int, ev: TrackEvent):
        row = self.rows.get((pi, ev.index))
        if row is None:
            return
        status_item = self.table.item(row, 3)
        status_item.setText(ev.status.value)
        color = STATUS_COLORS.get(ev.status)
        status_item.setForeground(QColor(color) if color else self.palette().text().color())
        detail = ev.message
        if ev.match and ev.status in (Status.DONE, Status.DOWNLOADING, Status.CONVERTING):
            detail = f"nota {ev.match.score:.2f} · {ev.match.via}"
            self.table.item(row, 4).setToolTip(ev.match.url)
        self.table.item(row, 4).setText(detail)
        if ev.status.finished:
            self.done_count += 1
            self.progress.setValue(self.done_count)
        elif ev.status not in (Status.QUEUED,):
            self.table.scrollToItem(status_item, QAbstractItemView.EnsureVisible)

    def _on_playlist_done(self, pi: int, report: Report):
        self.reports.append(report)

    def _on_all_done(self):
        self._set_running(False)
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1)
        r = self.reports
        problems = sum(len(x.not_found) + len(x.failed) for x in r)
        text = (f"{sum(x.downloaded for x in r)} baixadas · {sum(x.copied for x in r)} copiadas · "
                f"{sum(x.skipped for x in r)} já existiam · {problems} com problema")
        if self.cancel.is_set():
            text = "Cancelado. " + text
        self.summary.setText(text)
        if problems:
            self.summary.setToolTip("Veja o arquivo _faltando.txt dentro de cada pasta.")
        if self.notes:
            QMessageBox.information(self, "Playlists incompletas", "\n\n".join(self.notes))
        if self.pending_update:
            self._offer_update()

    def _on_error(self, message: str):
        if not self.running:
            self.login_btn.setEnabled(bool(self.client))
            if not (self.client and self.client.logged_in):
                self.account_label.setText("Desconectado")
        QMessageBox.warning(self, "Aviso", message)

    def _open_folder(self):
        folder = Path(self.settings.output_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        if self.running:
            if QMessageBox.question(self, "Sair", "Há downloads em andamento. Cancelar e sair?") != QMessageBox.Yes:
                event.ignore()
                return
            self.cancel.set()
        self._install_update(relaunch=False)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("FaixaMP3")
    app.setApplicationVersion(__version__)
    icon = Path(__file__).with_name("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    if not win.settings.client_id:
        win._open_settings()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
