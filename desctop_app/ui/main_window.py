"""Главное окно: постоянная панель, вкладки, консоли и связь с устройством.

Постоянные части, не зависящие от вкладок:
    верхняя панель - подключение, Ping, STOP, состояние и работа с конфигурацией;
    нижняя панель - две консоли: сырой обмен и его расшифровка, обе сворачиваются.

STOP вынесен в постоянную панель намеренно: по п. 7 ТЗ опасное движение должно
останавливаться, а кнопка на вкладке недоступна, пока открыта другая вкладка.

Виджеты трогает только главный поток. Данные из фоновых потоков попадают сюда
через очереди, которые разгребает таймер:
    client.raw      строки обмена, для консолей
    client.incoming телеметрия и события устройства
    self._events    результаты команд и обрывы связи
"""

import json
import queue
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..protocol.client import Client
from ..protocol.emulator import DEMO_PORT, DemoTransport
from ..protocol.message import (
    CMD_GET_CFG,
    CMD_PING,
    CMD_SET_CFG,
    CMD_STOP,
    EVT_HOMING,
    STATUS_COMPLETED,
    STATUS_ERROR,
    TYPE_EVT,
    TYPE_TLM,
)
from ..protocol.transport import MySerialTransport, get_ports
from .home_tab import HomeTab
from .manual_tab import ManualTab
from .params import DEFAULT_CONFIG
from .params_tab import ParamsTab
from .theme import C_DIM, C_ERR, C_OK
from .widgets import Console

POLL_INTERVAL_MS = 30
DEFAULT_PROFILE = "default.json"


def profiles_dir() -> Path:
    """Папка профилей: создаётся при первом запуске вместе с профилем
    значений по умолчанию.

    Лежит в самом проекте, рядом с исходниками: профиль по умолчанию
    хранится в репозитории, поэтому после клонирования он уже на месте
    и не зависит от того, на какой машине запускают.

    Диалоги открываются здесь, но сохранить и загрузить можно куда угодно:
    папка задаёт стартовую точку, а не границу.

    Путь считается от файла модуля, а не от рабочего каталога: иначе он
    съезжал бы в зависимости от того, откуда запустили приложение.
    """
    path = Path(__file__).resolve().parents[2] / "profiles"
    path.mkdir(parents=True, exist_ok=True)
    default = path / DEFAULT_PROFILE
    if not default.exists():
        default.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return path


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._client: Client | None = None
        self._fault = False
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()

        self.setWindowTitle("Servo Tester, конфигуратор Feetech STS3215")
        self.setMinimumSize(900, 580)
        self._fit_to_screen()

        self._home = HomeTab(self._send)
        self._manual = ManualTab(self._send)
        self._params = ParamsTab()
        tabs = QTabWidget()
        tabs.addTab(self._home, "Дом")
        tabs.addTab(self._params, "Параметры")
        tabs.addTab(self._manual, "Ручное управление")

        self.addToolBar(self._build_top_bar())

        self._show_tlm = QCheckBox("телеметрия")
        self._raw = Console("Сырой обмен", self._show_tlm)
        self._readable = Console("Расшифровка")
        consoles = QSplitter(Qt.Orientation.Horizontal)
        consoles.addWidget(self._raw)
        consoles.addWidget(self._readable)
        consoles.setSizes([700, 500])

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(tabs)
        outer.addWidget(consoles)
        outer.setStretchFactor(0, 4)
        outer.setStretchFactor(1, 1)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(8)
        layout.addWidget(outer)
        self.setCentralWidget(root)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_INTERVAL_MS)

        self._refresh_ports()
        self._update_state()

    def _fit_to_screen(self) -> None:
        """Подбирает стартовый размер под экран.

        Фиксированные размеры ломаются при системном масштабировании:
        окно 1360x860 не влезает в рабочую область 1480x837.
        """
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        width = min(1280, int(area.width() * 0.92)) if area else 1100
        height = min(820, int(area.height() * 0.92)) if area else 700
        self.resize(width, height)
        if area:
            self.move(area.x() + (area.width() - width) // 2,
                      area.y() + (area.height() - height) // 3)

    # --- верхняя панель ----------------------------------------------------

    def _build_top_bar(self) -> QToolBar:
        """Панель на QToolBar: при нехватке ширины Qt сам убирает лишнее
        в выпадающее меню, а не обрезает кнопки."""
        bar = QToolBar("Управление")
        bar.setMovable(False)
        bar.setFloatable(False)

        self._ports = QComboBox()
        self._ports.setMinimumWidth(190)
        self._refresh_button = QPushButton("Обновить")
        self._refresh_button.clicked.connect(self._refresh_ports)
        self._connect_button = QPushButton("Подключиться")
        self._connect_button.clicked.connect(self._toggle_connection)
        self._ping_button = QPushButton("Ping")
        self._ping_button.clicked.connect(lambda: self._send(CMD_PING))
        self._stop_button = QPushButton("STOP")
        self._stop_button.setStyleSheet(
            f"background:{C_ERR}; color:#101010; font-weight:bold; padding:6px 18px;")
        self._stop_button.clicked.connect(lambda: self._send(CMD_STOP))
        self._status = QLabel()

        for widget in (QLabel(" Порт: "), self._ports, self._refresh_button,
                       self._connect_button, self._ping_button,
                       self._stop_button, self._status):
            bar.addWidget(widget)
        # распорка прижимает кнопки конфигурации к правому краю
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        bar.addSeparator()
        bar.addWidget(QLabel(" Конфигурация: "))

        self._config_buttons = []
        for title, slot in (("Read", lambda: self._send(CMD_GET_CFG)),
                            ("Write", self._write_config),
                            ("Из файла", self._load_config),
                            ("В файл", self._save_config),
                            ("По умолчанию", self._reset_config)):
            button = QPushButton(title)
            button.clicked.connect(slot)
            bar.addWidget(button)
            self._config_buttons.append(button)
        return bar

    # --- подключение -------------------------------------------------------

    def _refresh_ports(self) -> None:
        """USB-устройства идут первыми: у них есть vid, у ttyS* его нет."""
        current = self._ports.currentData()
        self._ports.clear()
        self._ports.addItem("Демо, работа без платы", DEMO_PORT)
        for port in sorted(get_ports(), key=lambda p: (p.vid is None, p.device)):
            self._ports.addItem(f"{port.device} - {port.description}", port.device)
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
            self._note("error", f"не удалось открыть {port}: {error}")
            QMessageBox.critical(self, "Не удалось подключиться", str(error))
            return
        self._client = client
        self._fault = False
        self._note("info", "демо-режим: платы нет, отвечает эмулятор"
                   if demo else f"порт {port} открыт")

    def _disconnect(self) -> None:
        self._client.disconnect()
        self._client = None
        self._fault = False
        self._params.forget_device()
        self._note("info", "отключено")

    # --- команды -----------------------------------------------------------

    def _send(self, cmd: str, **params: object) -> None:
        """Команда уходит в отдельный поток: send_command блокирует до таймаута."""
        client = self._client
        if client is None:
            return
        threading.Thread(target=self._worker, args=(client, cmd, params),
                         daemon=True).start()

    def _worker(self, client: Client, cmd: str, params: dict) -> None:
        try:
            answer = client.send_command(cmd, **params)
        except (TimeoutError, OSError, ValueError) as error:
            self._events.put(("error", f"{cmd}: {error}"))
            return
        if answer.get("ok"):
            if cmd == CMD_GET_CFG and isinstance(answer.get("data"), dict):
                self._events.put(("config", json.dumps(answer["data"])))
            elif cmd == CMD_SET_CFG:
                self._events.put(("written", json.dumps(params)))

    def _write_config(self) -> None:
        self._send(CMD_SET_CFG, **self._params.collect())

    def _reset_config(self) -> None:
        self._params.apply(DEFAULT_CONFIG)
        self._params.mark_saved(DEFAULT_CONFIG)
        self._home.set_default_speed(DEFAULT_CONFIG["speed"])
        self._note("info", "подставлены значения по умолчанию, "
                           "для записи на устройство нажмите Write")

    def _save_config(self) -> None:
        dialog = QFileDialog(self, "Сохранить конфигурацию",
                             str(profiles_dir()), "JSON (*.json)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        # иначе имя без расширения сохранится файлом без расширения,
        # и при загрузке он не попадёт под фильтр
        dialog.setDefaultSuffix("json")
        dialog.selectFile("servo-config.json")
        if not dialog.exec():
            return
        self.write_profile(Path(dialog.selectedFiles()[0]))

    def write_profile(self, path: Path) -> None:
        values = self._params.collect()
        try:
            path.write_text(json.dumps(values, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as error:
            self._note("error", f"не удалось сохранить {path}: {error}")
            return
        self._params.mark_saved(values)
        self._note("info", f"конфигурация сохранена в {path}")

    def _load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить конфигурацию", str(profiles_dir()), "JSON (*.json)")
        if path:
            self.read_profile(Path(path))

    def read_profile(self, path: Path) -> None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._note("error", f"не удалось прочитать {path}: {error}")
            return
        if not isinstance(loaded, dict):
            self._note("error", f"{path}: ожидался объект с параметрами")
            return
        unknown = set(loaded) - set(DEFAULT_CONFIG)
        if unknown:
            self._note("error", f"в файле неизвестные поля: {sorted(unknown)}")
            return
        self._params.apply(loaded)
        self._params.mark_saved(self._params.collect())
        self._home.set_default_speed(self._params.collect()["speed"])
        self._note("info", f"конфигурация загружена из {path}")

    def _on_lost(self, error: Exception) -> None:
        """Вызывается ИЗ ПОТОКА-ЧИТАТЕЛЯ, только кладём событие в очередь."""
        self._fault = True
        self._events.put(("error", f"связь потеряна: {error}"))

    # --- опрос -------------------------------------------------------------

    def _poll(self) -> None:
        if self._client is not None:
            self._drain(self._client.raw, lambda item: self._on_raw(*item))
            self._drain(self._client.incoming, self._on_message)
        self._drain(self._events, lambda item: self._on_event(*item))

    @staticmethod
    def _drain(source: queue.Queue, handler) -> None:
        while True:
            try:
                item = source.get_nowait()
            except queue.Empty:
                return
            handler(item)

    def _on_raw(self, direction: str, line: str) -> None:
        if not self._show_tlm.isChecked() and '"tlm"' in line:
            return
        self._raw.append(direction, line)
        self._readable.append(direction, describe(line))

    def _on_event(self, kind: str, text: str) -> None:
        if kind == "config":
            values = json.loads(text)
            self._params.apply(values)
            self._params.mark_on_device(values)
            self._home.set_default_speed(self._params.collect()["speed"])
            self._note("info", "конфигурация прочитана с устройства")
            return
        if kind == "written":
            values = json.loads(text)
            self._params.mark_on_device(values)
            self._home.set_range(int(values.get("min_pos", 0)),
                                 int(values.get("max_pos", 4095)))
            self._note("info", "конфигурация записана на устройство")
            return
        self._note(kind, text)
        if kind == "error":
            self._update_state()

    def _on_message(self, message: dict) -> None:
        """Телеметрия и события устройства, всё что не ответ на команду."""
        kind = message.get("type")
        if kind == TYPE_TLM:
            self._home.update_telemetry(message)
            self._manual.update_telemetry(message)
        elif kind == TYPE_EVT and message.get("event") == EVT_HOMING:
            text = homing_text(message)
            self._home.set_homing_status(text)
            self._manual.set_homing_status(text)
            if message.get("status") == STATUS_COMPLETED and "zero" in message:
                self._home.set_zero(int(message["zero"]))

    def _note(self, kind: str, text: str) -> None:
        """Служебное сообщение самого приложения, только в расшифровку."""
        self._readable.append(kind, text)

    # --- состояние ---------------------------------------------------------

    def _update_state(self) -> None:
        connected = self._client is not None
        active = connected and not self._fault
        if self._fault:
            text, colour = "Обрыв связи", C_ERR
        elif connected:
            text, colour = "Подключено", C_OK
        else:
            text, colour = "Не подключено", C_DIM
        self._connect_button.setText("Отключиться" if connected else "Подключиться")
        self._ping_button.setEnabled(active)
        self._stop_button.setEnabled(active)
        self._ports.setEnabled(not connected)
        self._refresh_button.setEnabled(not connected)
        self._home.set_active(active)
        self._manual.set_active(active)
        for button in self._config_buttons[:2]:
            button.setEnabled(active)
        self._status.setText(f'<span style="color:{colour}">●  {text}</span>')

    def closeEvent(self, event) -> None:
        self._timer.stop()
        if self._client is not None:
            self._disconnect()
        super().closeEvent(event)


def homing_text(message: dict) -> str:
    """Статус homing для интерфейса.

    В протоколе статусов ровно четыре и они строчные, а причина отказа идёт
    отдельным полем. Интерфейс принимает решение по статусу и просто
    дописывает причину текстом, поэтому новая причина в прошивке не требует
    правок здесь.
    """
    status = str(message.get("status", "?"))
    text = status.capitalize()
    if status == STATUS_ERROR and message.get("reason"):
        text += f": {message['reason']}"
    return text


def describe(line: str) -> str:
    """Переводит строку протокола на человеческий язык."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return "нераспознанная строка (мусор на линии или загрузчик платы)"
    if not isinstance(message, dict):
        return "не сообщение протокола"
    kind = message.get("type")
    number = message.get("id", "?")
    if kind == "cmd":
        params = {k: v for k, v in message.items()
                  if k not in ("type", "id", "cmd")}
        tail = f", параметры: {params}" if params else ""
        return f"#{number} отправлена команда {message.get('cmd')}{tail}"
    if kind == "resp":
        if message.get("ok"):
            return f"#{number} успех: {message.get('data', 'без данных')}"
        return f"#{number} отказ: {message.get('error', 'причина не указана')}"
    if kind == "tlm":
        return (f"телеметрия: позиция {message.get('pos')}, "
                f"скорость {message.get('spd')}, нагрузка {message.get('load')}")
    if kind == "evt":
        event = message.get("event")
        if event == "limit":
            return (f"привод вышел за диапазон ({message.get('reason')}), "
                    f"остановлен на {message.get('pos')} шаг")
        parts = [str(message.get(k)) for k in ("event", "status", "reason")
                 if message.get(k) is not None]
        return "событие: " + " ".join(parts)
    return f"неизвестный тип сообщения: {kind}"
