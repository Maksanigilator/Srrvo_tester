"""Главное окно. MVP: подключение к порту, ping и консоль обмена.

Виджеты трогает только главный поток. Данные из фоновых потоков
попадают сюда через очереди, которые разгребает таймер:

  client.raw  — строки обмена, для консоли
  self._events — результаты команд и обрывы связи

Поэтому on_lost, который вызывается из потока-читателя, ничего не рисует,
а только кладёт событие в очередь.
"""

import html
import queue
import threading
import time

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QTimer

from ..protocol.client import Client
from ..protocol.emulator import DEMO_PORT, DemoTransport
from ..protocol.message import CMD_PING
from ..protocol.transport import MySerialTransport, get_ports

POLL_INTERVAL_MS = 30
CONSOLE_MAX_LINES = 1000

COLORS = {">": "#2e7d32", "<": "#1565c0", "error": "#c62828", "info": "#616161"}
MARKERS = {">": "→", "<": "←", "error": "!", "info": "·"}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._client: Client | None = None
        self._fault = False
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()

        self.setWindowTitle("Servo Tester — конфигуратор STS3215")
        self.resize(900, 560)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(self._build_connection_bar())
        layout.addWidget(self._build_console(), stretch=1)
        self.setCentralWidget(root)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_INTERVAL_MS)

        self._refresh_ports()
        self._update_state()

    # --- построение интерфейса -------------------------------------------

    def _build_connection_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self._ports = QComboBox()
        self._ports.setMinimumWidth(320)

        self._refresh_button = QPushButton("Обновить")
        self._refresh_button.clicked.connect(self._refresh_ports)

        self._connect_button = QPushButton("Подключиться")
        self._connect_button.clicked.connect(self._toggle_connection)

        self._ping_button = QPushButton("Ping")
        self._ping_button.clicked.connect(self._ping)

        self._status = QLabel()

        bar.addWidget(QLabel("Порт:"))
        bar.addWidget(self._ports)
        bar.addWidget(self._refresh_button)
        bar.addWidget(self._connect_button)
        bar.addWidget(self._ping_button)
        bar.addStretch(1)
        bar.addWidget(self._status)
        return bar

    def _build_console(self) -> QGroupBox:
        box = QGroupBox("Обмен с устройством")
        layout = QVBoxLayout(box)

        controls = QHBoxLayout()
        self._autoscroll = QCheckBox("Прокручивать")
        self._autoscroll.setChecked(True)
        clear_button = QPushButton("Очистить")
        clear_button.clicked.connect(lambda: self._console.clear())
        controls.addWidget(self._autoscroll)
        controls.addWidget(clear_button)
        controls.addStretch(1)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(CONSOLE_MAX_LINES)
        self._console.setFont(QFont("monospace", 9))

        layout.addLayout(controls)
        layout.addWidget(self._console)
        return box

    # --- действия ---------------------------------------------------------

    def _refresh_ports(self) -> None:
        """USB-устройства идут первыми: у них есть vid, у ttyS* его нет."""
        current = self._ports.currentData()
        self._ports.clear()
        self._ports.addItem("Демо — работа без платы", DEMO_PORT)
        for port in sorted(get_ports(), key=lambda p: (p.vid is None, p.device)):
            self._ports.addItem(f"{port.device} — {port.description}", port.device)
        index = self._ports.findData(current)
        if index >= 0:
            self._ports.setCurrentIndex(index)

    def _toggle_connection(self) -> None:
        if self._client is not None:
            self._disconnect()
        else:
            self._connect()
        self._update_state()

    def _connect(self) -> None:
        port = self._ports.currentData()
        if port is None:
            QMessageBox.warning(self, "Порт не выбран",
                                "Сначала выберите порт в списке.")
            return
        demo = port == DEMO_PORT
        client = Client(DemoTransport() if demo else MySerialTransport(port))
        client.on_lost = self._on_lost
        try:
            client.connect()
        except OSError as error:
            self._log("error", f"не удалось открыть {port}: {error}")
            QMessageBox.critical(self, "Не удалось подключиться", str(error))
            return
        self._client = client
        self._fault = False
        self._log("info", "демо-режим: платы нет, отвечает эмулятор"
                  if demo else f"порт {port} открыт")

    def _disconnect(self) -> None:
        self._client.disconnect()
        self._client = None
        self._fault = False
        self._log("info", "отключено")

    def _ping(self) -> None:
        """Команда уходит в отдельный поток: send_command блокирует до таймаута."""
        client = self._client
        if client is None:
            return
        threading.Thread(target=self._ping_worker, args=(client,),
                         daemon=True).start()

    def _ping_worker(self, client: Client) -> None:
        try:
            answer = client.send_command(CMD_PING)
        except (TimeoutError, OSError) as error:
            self._events.put(("error", str(error)))
        else:
            self._events.put(("info", f"ответ: {answer}"))

    def _on_lost(self, error: Exception) -> None:
        """Вызывается ИЗ ПОТОКА-ЧИТАТЕЛЯ — только кладём событие в очередь."""
        self._fault = True
        self._events.put(("error", f"связь потеряна: {error}"))

    # --- опрос ------------------------------------------------------------

    def _poll(self) -> None:
        if self._client is not None:
            while True:
                try:
                    direction, line = self._client.raw.get_nowait()
                except queue.Empty:
                    break
                self._log(direction, line)
        while True:
            try:
                kind, text = self._events.get_nowait()
            except queue.Empty:
                break
            self._log(kind, text)
            if kind == "error":
                self._update_state()

    def _log(self, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        color = COLORS.get(kind, "#000000")
        self._console.appendHtml(
            f'<span style="color:#9e9e9e">{stamp}</span> '
            f'<span style="color:{color}">{MARKERS.get(kind, " ")} '
            f'{html.escape(text)}</span>'
        )
        if self._autoscroll.isChecked():
            bar = self._console.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _update_state(self) -> None:
        connected = self._client is not None
        if self._fault:
            text, color = "Обрыв связи", "#c62828"
        elif connected:
            text, color = "Подключено", "#2e7d32"
        else:
            text, color = "Не подключено", "#9e9e9e"
        self._connect_button.setText("Отключиться" if connected else "Подключиться")
        self._ping_button.setEnabled(connected and not self._fault)
        self._ports.setEnabled(not connected)
        self._refresh_button.setEnabled(not connected)
        self._status.setText(f'<span style="color:{color}">● {text}</span>')

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._client is not None:
            self._disconnect()
        super().closeEvent(event)
