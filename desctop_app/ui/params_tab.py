"""Вкладка «Параметры»: редактирование конфигурации с индикацией расхождений.

Источник правды для текущих значений - сами поля. Отдельной рабочей копии
в словаре нет намеренно: два места, описывающих одно состояние, рано или
поздно разъезжаются.

Хранятся только два снимка для сравнения:
    _saved   что было сохранено или загружено на компьютере,
             на старте равен значениям по умолчанию;
    _device  что в последний раз вернула плата, пока не читали - None.

Две точки в конце строки показывают расхождение с этими снимками.
"""

import json
from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .params import (
    DEFAULT_CONFIG,
    PARAM_GROUPS,
    POSITION_KEYS,
    degrees_to_steps,
    steps_to_degrees,
)
from .theme import C_DIM, C_OK, C_WARN

C_UNKNOWN = "#5a5f65"

# Подробные описания параметров будут написаны позже, пока заглушка.
DESCRIPTION_STUB = "=== текстовое описание ==="


class ParamsTab(QWidget):
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_change = on_change
        self._fields: dict[str, QWidget] = {}
        self._marks: dict[str, QLabel] = {}
        self._row_widgets: dict[str, tuple] = {}
        self._degree_fields: dict[str, QDoubleSpinBox] = {}
        self._spec: dict[str, tuple] = {}
        self._saved = dict(DEFAULT_CONFIG)
        self._device: dict | None = None

        inner = QWidget()
        columns = QHBoxLayout(inner)
        left, right = QVBoxLayout(), QVBoxLayout()
        for index, (title, params) in enumerate(PARAM_GROUPS):
            (left if index % 2 == 0 else right).addWidget(
                self._build_group(title, params))
        left.addStretch(1)
        right.addStretch(1)
        columns.addLayout(left)
        columns.addLayout(right)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(area)
        self.refresh()

    # --- публичное ---------------------------------------------------------

    def collect(self) -> dict:
        """Снимает значения со всех полей."""
        return {key: (widget.currentData() if isinstance(widget, QComboBox)
                      else widget.value())
                for key, widget in self._fields.items()}

    def apply(self, values: dict) -> None:
        """Раскладывает значения по полям, незнакомые ключи пропускает."""
        for key, value in values.items():
            widget = self._fields.get(key)
            if widget is None:
                continue
            if isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            else:
                widget.setValue(int(value))
                degrees = self._degree_fields.get(key)
                if degrees is not None:
                    degrees.blockSignals(True)
                    degrees.setValue(steps_to_degrees(int(value)))
                    degrees.blockSignals(False)
        self.refresh()

    def mark_saved(self, values: dict | None = None) -> None:
        """Запоминает снимок «сохранено на компьютере»."""
        self._saved = dict(values if values is not None else self.collect())
        self.refresh()

    def mark_on_device(self, values: dict) -> None:
        """Запоминает снимок «записано на плату»."""
        self._device = dict(values)
        self.refresh()

    def forget_device(self) -> None:
        """При отключении состояние платы снова неизвестно."""
        self._device = None
        self.refresh()

    def refresh(self) -> None:
        current = self.collect()
        for key, mark in self._marks.items():
            value = current[key]
            saved = self._compare(value, self._saved.get(key))
            device = (self._compare(value, self._device.get(key))
                      if self._device is not None else None)
            mark.setText(f'<span style="color:{self._colour(saved)}">&#9679;</span> '
                         f'<span style="color:{self._colour(device)}">&#9679;</span>')
            tooltip = self._tooltip(key, value)
            for widget in self._row_widgets[key]:
                widget.setToolTip(tooltip)

    # --- внутреннее --------------------------------------------------------

    @staticmethod
    def _compare(value, other) -> bool | None:
        return None if other is None else value == other

    @staticmethod
    def _colour(state: bool | None) -> str:
        if state is None:
            return C_UNKNOWN
        return C_OK if state else C_WARN

    def _tooltip(self, key: str, value) -> str:
        """Подсказка по строке: описание и два значения для сравнения.

        Полные описания параметров появятся позже, сейчас берётся короткая
        строка из PARAM_GROUPS.
        """
        label, unit, _hint = self._spec[key]
        config = self._show(self._saved.get(key), unit)
        device = ("не прочитано" if self._device is None
                  else self._show(self._device.get(key), unit))
        return (f"<div style='max-width:320px'>"
                f"<b>{label}</b><br><br>"
                f"{DESCRIPTION_STUB}<br><br>"
                f"Значение в конфиге: <b>{config}</b><br>"
                f"Значение на плате: <b>{device}</b>"
                f"</div>")

    @staticmethod
    def _show(value, unit: str) -> str:
        if value is None:
            return "нет в ответе"
        return f"{value} {unit}".strip()

    def _build_group(self, title: str, params) -> QGroupBox:
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(6)
        for row, (key, label, low, high, unit, hint) in enumerate(params):
            extra = ()
            if key == "home_dir":
                widget = QComboBox()
                widget.addItem("CW, по часовой", "cw")
                widget.addItem("CCW, против часовой", "ccw")
                widget.currentIndexChanged.connect(self._changed)
                cell = widget
            elif key in POSITION_KEYS:
                widget, degrees, cell = self._build_position_cell(key, low, high)
                extra = (degrees,)
                unit = "шаг"
            else:
                widget = QSpinBox()
                widget.setRange(low, high)
                widget.setValue(DEFAULT_CONFIG[key])
                widget.valueChanged.connect(self._changed)
                cell = widget
            mark = QLabel()
            name = QLabel(label)
            unit_label = QLabel("" if key in POSITION_KEYS else unit)
            unit_label.setStyleSheet(f"color:{C_DIM};")
            grid.addWidget(name, row, 0)
            grid.addWidget(cell, row, 1)
            grid.addWidget(unit_label, row, 2)
            grid.addWidget(mark, row, 3)
            self._fields[key] = widget
            self._marks[key] = mark
            self._spec[key] = (label, unit, hint)
            # подсказка нужна на всей строке: наводят и на название, и на поле
            self._row_widgets[key] = (name, widget, unit_label, mark) + extra
        return box


    def _build_position_cell(self, key: str, low: int, high: int):
        """Пара связанных полей: шаги и градусы.

        Хранится всегда значение в шагах. Пока набирают градусы, шаги
        пересчитываются на каждое изменение, а само поле градусов
        подтягивается к достижимому значению только по окончании ввода,
        иначе оно переписывало бы набираемое число под пальцами.
        """
        steps = QSpinBox()
        steps.setRange(low, high)
        steps.setValue(DEFAULT_CONFIG[key])
        steps.setSuffix(" шаг")

        degrees = QDoubleSpinBox()
        degrees.setDecimals(2)
        degrees.setRange(steps_to_degrees(low), steps_to_degrees(high))
        degrees.setValue(steps_to_degrees(DEFAULT_CONFIG[key]))
        degrees.setSuffix(" °")

        def on_steps(value: int) -> None:
            degrees.blockSignals(True)
            degrees.setValue(steps_to_degrees(value))
            degrees.blockSignals(False)
            self._changed()

        def on_degrees(value: float) -> None:
            steps.blockSignals(True)
            steps.setValue(max(low, min(high, degrees_to_steps(value))))
            steps.blockSignals(False)
            self._changed()

        def snap() -> None:
            degrees.blockSignals(True)
            degrees.setValue(steps_to_degrees(steps.value()))
            degrees.blockSignals(False)

        steps.valueChanged.connect(on_steps)
        degrees.valueChanged.connect(on_degrees)
        degrees.editingFinished.connect(snap)

        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(steps, 1)
        layout.addWidget(degrees, 1)
        self._degree_fields[key] = degrees
        return steps, degrees, cell

    def _changed(self, *_) -> None:
        self.refresh()
        if self._on_change is not None:
            self._on_change()
