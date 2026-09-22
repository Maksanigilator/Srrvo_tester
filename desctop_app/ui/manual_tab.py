"""Вкладка «Ручное управление»: настройка регулятора и подробные графики.

ЗАГЛУШКА. Виджеты расставлены, обработчиков нет: ползунки никуда не шлют,
графики не наполняются. Смысл вкладки описан ниже, чтобы при наполнении
не пришлось вспоминать замысел.

Коэффициенты регулятора считает сам сервопривод, регистры 0x15, 0x16, 0x17
для позиционного контура и 0x25, 0x27 для скоростного. Прошивка только
передаёт их на шину.

Эти регистры лежат в EPROM, поэтому гнать в них каждое движение ползунка
нельзя: ресурс ячеек конечен. Замысел такой:

    замок 0x37 = 1   коэффициенты применяются, но не сохраняются,
                     в этом режиме и крутятся ползунки;
    замок 0x37 = 0   одна запись по кнопке «Сохранить в серву».

Формулировку про замок надо проверить на живой плате: документация говорит,
что при поднятом замке значение применяется и просто не переживает
выключение питания, но возможно и то, что запись игнорируется целиком.

Графики: слева что происходит, справа насколько плохо и чем регулятор за
это платит. «Управляющий сигнал» это регистр 0x3C, он же «нагрузка»:
по документации это скважность управляющего напряжения на мотор.
Ошибки считаются на стороне ПК как разность задания и факта.
"""

import pyqtgraph as pg
from PySide6.QtCore import Qt
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

from .params import DEFAULT_CONFIG, POSITION_MAX, SPEED_MAX
from .theme import C_ACCENT, C_DIM, C_ERR, C_OK, C_WARN
from .widgets import HomingControl, PositionControl, TelemetryPanel

# ключ, подпись, максимум, значение по умолчанию
REGULATOR_SLIDERS = (
    ("Регулятор положения", (
        ("pos_p", "P", 254, "пропорциональный, регистр 0x15"),
        ("pos_d", "D", 254, "дифференциальный, регистр 0x16"),
        ("pos_i", "I", 254, "интегральный, регистр 0x17"),
    )),
    ("Регулятор скорости", (
        ("speed_p", "P", 100, "пропорциональный, регистр 0x25"),
        ("speed_i", "I", 254, "интегральный, регистр 0x27"),
    )),
)

# ключ, заголовок, единица, пределы по Y, список кривых (подпись, цвет, пунктир)
CHART_SPECS = (
    ("position", "Позиция и цель", "шаг", (0, POSITION_MAX),
     (("позиция", C_ACCENT, False), ("цель", C_DIM, True))),
    ("pos_error", "Ошибка позиции", "шаг", (-POSITION_MAX, POSITION_MAX),
     (("ошибка", C_ERR, False),)),
    ("speed", "Скорость и задание", "шаг/с", (-SPEED_MAX, SPEED_MAX),
     (("скорость", C_OK, False), ("задание", C_DIM, True))),
    ("speed_error", "Ошибка скорости", "шаг/с", (-SPEED_MAX, SPEED_MAX),
     (("ошибка", C_ERR, False),)),
    ("control", "Управляющий сигнал", "1/1000", (0, 1000),
     (("скважность", C_WARN, False),)),
    ("current", "Ток", "мА", (0, 3300),
     (("ток", C_ACCENT, False),)),
)


class ManualTab(QWidget):
    def __init__(self, send) -> None:
        super().__init__()
        self._send = send
        self._sliders: dict[str, QSlider] = {}
        self._boxes: dict[str, QSpinBox] = {}
        self._curves: dict[str, list] = {}

        page = QSplitter(Qt.Orientation.Horizontal)
        page.addWidget(self._build_controls())
        page.addWidget(self._build_charts())
        page.addWidget(self._build_state())
        # растягивается только середина с графиками
        page.setStretchFactor(0, 0)
        page.setStretchFactor(1, 1)
        page.setStretchFactor(2, 0)
        page.setSizes([310, 690, 270])
        page.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page)

    def set_active(self, active: bool) -> None:
        for block in self._blocks:
            block.setEnabled(active)

    def update_telemetry(self, message: dict) -> None:
        self._telemetry.update_values(message)

    def set_homing_status(self, text: str) -> None:
        self._homing.set_status(text)

    # --- построение --------------------------------------------------------

    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(400)
        layout = QVBoxLayout(panel)

        self._blocks = []
        self._position = PositionControl(self._send)
        self._homing = HomingControl(self._send)
        for block in (self._position, self._homing):
            self._blocks.append(block)
            layout.addWidget(block)

        for title, sliders in REGULATOR_SLIDERS:
            box = QGroupBox(title)
            grid = QGridLayout(box)
            grid.setVerticalSpacing(4)
            # тянется только ползунок: имя и поле значения фиксированы,
            # иначе подпись зажимает поле до нечитаемого
            grid.setColumnStretch(1, 1)
            for row, (key, label, maximum, hint) in enumerate(sliders):
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(0, maximum)
                slider.setValue(DEFAULT_CONFIG[key])
                value = QSpinBox()
                value.setRange(0, maximum)
                value.setValue(DEFAULT_CONFIG[key])
                value.setFixedWidth(70)
                slider.valueChanged.connect(value.setValue)
                value.valueChanged.connect(slider.setValue)
                name = QLabel(label)
                name.setFixedWidth(16)
                for widget in (name, slider, value):
                    widget.setToolTip(hint)
                grid.addWidget(name, row, 0)
                grid.addWidget(slider, row, 1)
                grid.addWidget(value, row, 2)
                self._sliders[key] = slider
                self._boxes[key] = value
            self._blocks.append(box)
            layout.addWidget(box)

        note = QGroupBox("Режим настройки")
        nbox = QVBoxLayout(note)
        hint = QLabel(
            "Пока идёт настройка, коэффициенты применяются, но не сохраняются "
            "в серве: запись в EPROM изнашивает ячейки. Кнопка ниже сохраняет "
            "текущие значения один раз.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_DIM}; font-size:11px;")
        save = QPushButton("Сохранить в серву")
        save.setEnabled(False)
        save.setToolTip("появится вместе с командами настройки регулятора")
        nbox.addWidget(hint)
        nbox.addWidget(save)
        self._blocks.append(note)
        layout.addWidget(note)

        stub = QLabel("=== заглушка: ползунки ничего не отправляют ===")
        stub.setWordWrap(True)
        stub.setStyleSheet(f"color:{C_WARN}; font-size:11px;")
        layout.addWidget(stub)
        layout.addStretch(1)
        return panel

    def _build_state(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(240)
        panel.setMaximumWidth(360)
        layout = QVBoxLayout(panel)
        self._telemetry = TelemetryPanel()
        layout.addWidget(self._telemetry)
        layout.addStretch(1)
        return panel

    def _build_charts(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(360)
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for index, (key, title, unit, limits, curves) in enumerate(CHART_SPECS):
            plot = pg.PlotWidget(title=title)
            plot.setYRange(*limits)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setLabel("left", unit)
            plot.setLabel("bottom", "с")
            plot.setMinimumHeight(130)
            plot.setMinimumWidth(170)
            if len(curves) > 1:
                plot.addLegend(offset=(-10, 10))
            self._curves[key] = [
                plot.plot(pen=pg.mkPen(
                    colour, width=2,
                    style=Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine),
                    name=name)
                for name, colour, dashed in curves]
            grid.addWidget(plot, index // 2, index % 2)
        return panel
