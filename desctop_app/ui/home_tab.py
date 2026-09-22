"""Вкладка «Дом»: ручное управление, графики и текущее состояние привода.

Три колонки отвечают на три разных вопроса: что сделать, что было, что сейчас.

Вкладка ничего не знает про транспорт и клиента. Команды она отдаёт через
функцию send, переданную при создании, а данные получает вызовами
update_telemetry и set_homing_status из главного окна.
"""

from collections.abc import Callable

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..protocol.message import CMD_MOTOR, CMD_STOP
from .params import DEFAULT_CONFIG, POSITION_MAX, SPEED_MAX
from .theme import C_ACCENT, C_DIM, C_ERR, C_OK, C_WARN
from .widgets import HomingControl, PositionControl, ServoDial, TelemetryPanel

CHART_POINTS = 600                 # 30 секунд при 20 Гц

class HomeTab(QWidget):
    def __init__(self, send: Callable[..., None]) -> None:
        super().__init__()
        self._send = send
        self._dial = ServoDial()
        self._chart_data: dict[str, list[float]] = {
            "t": [], "pos": [], "spd": [], "load": []}

        page = QSplitter(Qt.Orientation.Horizontal)
        page.addWidget(self._build_controls())
        page.addWidget(self._build_charts())
        page.addWidget(self._build_state())
        # растягивается только середина с графиками: боковые колонки
        # держат содержимое, а не пустоту
        page.setStretchFactor(0, 0)
        page.setStretchFactor(1, 1)
        page.setStretchFactor(2, 0)
        page.setSizes([320, 560, 320])
        page.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page)

    # --- публичное ---------------------------------------------------------

    def set_active(self, active: bool) -> None:
        for block in self._blocks:
            block.setEnabled(active)

    def set_default_speed(self, value: int) -> None:
        """Ползунок скорости отталкивается от рабочей скорости конфигурации."""
        self._speed.setValue(int(value))

    def set_homing_status(self, text: str) -> None:
        self._homing.set_status(text)

    def update_telemetry(self, message: dict) -> None:
        self._telemetry.update_values(message)
        if "pos" in message:
            self._dial.update_values(position=int(message["pos"]))
        if "tgt" in message:
            # Цель берём с устройства, а не с ползунка: во время homing её
            # задаёт прошивка, и ползунок о ней ничего не знает.
            self._dial.update_values(target=int(message["tgt"]))
        self._chart_data["t"].append(float(message.get("t", 0)) / 1000.0)
        for key in ("pos", "spd", "load"):
            self._chart_data[key].append(float(message.get(key, 0)))
        for series in self._chart_data.values():
            del series[:-CHART_POINTS]
        for key, curve in self._curves.items():
            curve.setData(self._chart_data["t"], self._chart_data[key])

    def set_range(self, min_pos: int, max_pos: int) -> None:
        self._dial.update_values(min_pos=min_pos, max_pos=max_pos)

    def set_zero(self, steps: int) -> None:
        """Отметка нуля на диаграмме после успешного homing."""
        self._dial.update_values(zero=steps)

    # --- построение --------------------------------------------------------

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(270)
        panel.setMaximumWidth(420)
        layout = QVBoxLayout(panel)

        self._position = PositionControl(self._send, self._on_target_changed)
        movement = self._position

        motor = QGroupBox("Непрерывное вращение")
        mbox = QVBoxLayout(motor)
        speed_row = QHBoxLayout()
        self._speed = QSpinBox()
        self._speed.setRange(0, SPEED_MAX)
        self._speed.setValue(DEFAULT_CONFIG["speed"])
        self._speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._speed_slider.setRange(0, SPEED_MAX)
        self._speed_slider.setValue(DEFAULT_CONFIG["speed"])
        self._speed_slider.valueChanged.connect(self._speed.setValue)
        self._speed.valueChanged.connect(self._speed_slider.setValue)
        speed_row.addWidget(QLabel("Скорость"))
        speed_row.addWidget(self._speed)
        speed_row.addWidget(QLabel("шаг/с"))
        speed_row.addStretch(1)
        mbox.addLayout(speed_row)
        mbox.addWidget(self._speed_slider)
        hint = QLabel("временная, не сохраняется в конфигурацию")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_DIM}; font-size:11px;")
        mbox.addWidget(hint)
        buttons = QHBoxLayout()
        cw = QPushButton("◀  CW")
        cw.clicked.connect(
            lambda: self._send(CMD_MOTOR, dir="cw", speed=self._speed.value()))
        ccw = QPushButton("CCW  ▶")
        ccw.clicked.connect(
            lambda: self._send(CMD_MOTOR, dir="ccw", speed=self._speed.value()))
        # Остановка рядом с кнопками вращения: та же команда, что у общего
        # STOP, но не надо переводить взгляд на верхнюю панель.
        stop = QPushButton("STOP")
        stop.setStyleSheet(
            f"background:{C_ERR}; color:#101010; font-weight:bold;")
        stop.clicked.connect(lambda: self._send(CMD_STOP))
        buttons.addWidget(cw)
        buttons.addWidget(stop, stretch=1)
        buttons.addWidget(ccw)
        mbox.addLayout(buttons)

        self._homing = HomingControl(self._send)
        homing = self._homing

        self._blocks = [movement, motor, homing]
        for block in self._blocks:
            layout.addWidget(block)
        layout.addStretch(1)
        return panel

    def _build_charts(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self._curves = {}
        for key, title, unit, color, limits in (
                ("pos", "Позиция", "шаг", C_ACCENT, (0, POSITION_MAX)),
                ("spd", "Скорость", "шаг/с", C_OK, (-SPEED_MAX, SPEED_MAX)),
                ("load", "Нагрузка", "1/1000", C_WARN, (0, 1000))):
            plot = pg.PlotWidget(title=title)
            plot.setYRange(*limits)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setLabel("left", unit)
            plot.setLabel("bottom", "с")
            plot.setMinimumHeight(110)
            self._curves[key] = plot.plot(pen=pg.mkPen(color, width=2))
            layout.addWidget(plot)
        return panel

    def _build_state(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(240)
        panel.setMaximumWidth(400)
        layout = QVBoxLayout(panel)
        layout.addWidget(self._dial)

        self._telemetry = TelemetryPanel()
        layout.addWidget(self._telemetry)
        layout.addStretch(1)
        return panel

    # --- действия ----------------------------------------------------------

    def _on_target_changed(self, steps: int) -> None:
        """Серая качалка на диаграмме едет за ползунком, до нажатия Move."""
        self._dial.update_values(target=steps)

