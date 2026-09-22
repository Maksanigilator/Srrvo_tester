"""Самостоятельные виджеты: диаграмма сервопривода и консоль."""

import html
import time
from math import cos, radians, sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..protocol.message import CMD_HOME, CMD_MOVE, CMD_STOP
from .params import POSITION_MAX
from .theme import CONSOLE_COLORS, C_ACCENT, C_DIM, C_TEXT, C_WARN, MARKERS

CONSOLE_MAX_LINES = 1000


class ServoDial(QWidget):
    """Вид сервопривода сверху: качалка, рабочий сектор, ноль и цель.

    Одна картинка отвечает на три вопроса сразу - где привод, куда едет
    и где границы диапазона.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(170, 170)
        self.setMaximumHeight(300)
        self.position = 0
        self.target = 2048
        self.min_pos = 0
        self.max_pos = POSITION_MAX
        self.zero: int | None = None

    def update_values(self, **values) -> None:
        for key, value in values.items():
            setattr(self, key, value)
        self.update()

    @staticmethod
    def _angle(steps: int) -> float:
        """Шаги в градусы Qt: ноль сверху, по часовой стрелке."""
        return 90.0 - steps * 360.0 / (POSITION_MAX + 1)

    def paintEvent(self, event) -> None:
        side = min(self.width(), self.height()) - 16
        box = QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                     side, side)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setBrush(QColor("#2b3035"))
        painter.setPen(QPen(QColor("#454b52"), 2))
        painter.drawEllipse(box)

        # сектор рабочего диапазона поверх корпуса, иначе заливка его закроет
        span = (self.max_pos - self.min_pos) * 360.0 / (POSITION_MAX + 1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(78, 161, 255, 45))
        painter.drawPie(box, int(self._angle(self.min_pos) * 16),
                        int(-span * 16))

        centre = box.center()
        radius = side / 2

        if self.zero is not None:
            self._draw_arm(painter, centre, radius * 0.94, self.zero,
                           QPen(QColor(C_WARN), 2, Qt.PenStyle.DashLine))
        self._draw_arm(painter, centre, radius * 0.82, self.target,
                       QPen(QColor(138, 145, 153, 150), 6, Qt.PenStyle.SolidLine))
        self._draw_arm(painter, centre, radius * 0.82, self.position,
                       QPen(QColor(C_ACCENT), 8, Qt.PenStyle.SolidLine))

        painter.setBrush(QColor("#454b52"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, 7, 7)

        painter.setPen(QColor(C_TEXT))
        painter.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        degrees = self.position * 360.0 / (POSITION_MAX + 1)
        painter.drawText(
            QRectF(box.left(), box.bottom() - 30, box.width(), 26),
            Qt.AlignmentFlag.AlignCenter,
            f"{self.position} шаг · {degrees:.1f}°")
        painter.end()

    def _draw_arm(self, painter: QPainter, centre: QPointF, length: float,
                  steps: int, pen: QPen) -> None:
        angle = radians(self._angle(steps))
        painter.setPen(pen)
        painter.drawLine(centre, QPointF(centre.x() + length * cos(angle),
                                         centre.y() - length * sin(angle)))


class Console(QWidget):
    """Текстовая панель со сворачиванием."""

    def __init__(self, title: str, extra: QWidget | None = None) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        self._toggle = QPushButton("▼")
        self._toggle.setFixedWidth(26)
        self._toggle.setFlat(True)
        self._toggle.clicked.connect(self._flip)
        header.addWidget(self._toggle)
        header.addWidget(QLabel(f"<b>{title}</b>"))
        if extra is not None:
            header.addWidget(extra)
        header.addStretch(1)
        clear = QPushButton("Очистить")
        clear.clicked.connect(lambda: self.view.clear())
        header.addWidget(clear)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(CONSOLE_MAX_LINES)
        self.view.setFont(QFont("monospace", 9))
        self.view.setMinimumHeight(70)

        layout.addLayout(header)
        layout.addWidget(self.view)

    def _flip(self) -> None:
        visible = not self.view.isVisible()
        self.view.setVisible(visible)
        self._toggle.setText("▼" if visible else "▶")

    def append(self, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        color = CONSOLE_COLORS.get(kind, C_TEXT)
        self.view.appendHtml(
            f'<span style="color:{C_DIM}">{stamp}</span> '
            f'<span style="color:{color}">{MARKERS.get(kind, " ")} '
            f'{html.escape(text)}</span>')
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())


# ключ, подпись, единица, делитель и знаков после запятой.
# Напряжение приходит в десятых долях вольта, как лежит в регистре 0x3E.
TELEMETRY_FIELDS = (
    ("pos", "Позиция", "шаг", 1, 0),
    ("spd", "Скорость", "шаг/с", 1, 0),
    ("load", "Нагрузка", "1/1000", 1, 0),
    ("cur", "Ток", "мА", 1, 0),
    ("volt", "Напряжение", "В", 10, 1),
    ("temp", "Температура", "°C", 1, 0),
    ("mode", "Режим", "", 1, 0),
    ("err", "Ошибки", "", 1, 0),
)


class TelemetryPanel(QGroupBox):
    """Текущие значения с устройства. Один экземпляр на вкладку."""

    def __init__(self) -> None:
        super().__init__("Телеметрия")
        grid = QGridLayout(self)
        self._values: dict[str, QLabel] = {}
        for row, (key, title, unit, _, _) in enumerate(TELEMETRY_FIELDS):
            value = QLabel("?")
            value.setFont(QFont("monospace", 11, QFont.Weight.Bold))
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            unit_label = QLabel(unit)
            unit_label.setStyleSheet(f"color:{C_DIM};")
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(value, row, 1)
            grid.addWidget(unit_label, row, 2)
            self._values[key] = value
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(8)

    def update_values(self, message: dict) -> None:
        for key, _, _, divider, digits in TELEMETRY_FIELDS:
            if key not in message:
                continue
            value = message[key]
            if divider != 1 and isinstance(value, (int, float)):
                value = f"{value / divider:.{digits}f}"
            self._values[key].setText(str(value))


class PositionControl(QGroupBox):
    """Целевая позиция и переход в неё. Используется на нескольких вкладках."""

    def __init__(self, send, on_target_changed=None) -> None:
        super().__init__("Позиционный режим")
        self._send = send
        self._on_target_changed = on_target_changed
        box = QVBoxLayout(self)

        row = QHBoxLayout()
        self._target = QSpinBox()
        self._target.setRange(0, POSITION_MAX)
        self._target.setValue(2048)
        self._degrees = QLabel()
        move = QPushButton("Move")
        move.clicked.connect(lambda: self._send(CMD_MOVE, pos=self._target.value()))
        row.addWidget(QLabel("Цель"))
        row.addWidget(self._target)
        row.addWidget(self._degrees)
        row.addStretch(1)
        row.addWidget(move)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, POSITION_MAX)
        self._slider.setValue(2048)
        self._slider.valueChanged.connect(self._target.setValue)
        self._target.valueChanged.connect(self._slider.setValue)
        self._target.valueChanged.connect(self._changed)

        box.addLayout(row)
        box.addWidget(self._slider)
        self._changed(self._target.value())

    def target(self) -> int:
        return self._target.value()

    def set_range(self, low: int, high: int) -> None:
        """Цель нельзя задать вне разрешённого диапазона."""
        self._target.setRange(low, high)
        self._slider.setRange(low, high)

    def _changed(self, steps: int) -> None:
        self._degrees.setText(f"{steps * 360 / (POSITION_MAX + 1):.1f}°")
        if self._on_target_changed is not None:
            self._on_target_changed(steps)


class HomingControl(QGroupBox):
    """Запуск и прерывание поиска механического нуля."""

    def __init__(self, send) -> None:
        super().__init__("Homing, поиск механического нуля")
        self._send = send
        row = QHBoxLayout(self)
        start = QPushButton("Start Homing")
        start.clicked.connect(self._start)
        # Отдельная кнопка рядом со Start: прервать поиск, не переводя взгляд
        # на верхнюю панель. Команда та же, что у общего STOP.
        stop = QPushButton("Stop")
        stop.clicked.connect(self._stop)
        self._status = QLabel("Idle")
        row.addWidget(start)
        row.addWidget(stop)
        row.addWidget(self._status)
        row.addStretch(1)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def _start(self) -> None:
        self._status.setText("Running")
        self._send(CMD_HOME)

    def _stop(self) -> None:
        """Отдельной команды прерывания нет: STOP останавливает любое
        движение, включая homing."""
        self._status.setText("Stopped")
        self._send(CMD_STOP)
