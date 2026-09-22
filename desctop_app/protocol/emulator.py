"""Эмулятор платы для работы без железа (п. 8 ТЗ).

Подставляется вместо MySerialTransport: реализует тот же интерфейс
Transport, поэтому Client и интерфейс работают без единой правки.

Изображает не сервопривод, а плату целиком — отвечает на те же команды
и в том же формате, что прошивка ESP32. Когда в прошивке появляется новая
команда, её надо добавить и сюда, иначе демо-режим разойдётся с реальным
устройством и перестанет что-либо проверять.
"""

import json
import queue

from .message import CMD_PING, TYPE_RESP

# Интерфейс показывает это в списке портов вместо имени устройства.
DEMO_PORT = "__demo__"
FIRMWARE = "0.1.0-demo"


class DemoTransport:
    """Воображаемая плата на другом конце провода."""

    def __init__(self, timeout: float = 0.1) -> None:
        self._timeout = timeout
        self._opened = False
        self._outbox: queue.Queue[str] = queue.Queue()

    def open(self) -> None:
        self._opened = True
        while not self._outbox.empty():
            self._outbox.get_nowait()

    def close(self) -> None:
        self._opened = False

    def write_line(self, text: str) -> None:
        if not self._opened:
            raise ValueError("нет порта")
        self._handle(text)

    def read_line(self) -> str | None:
        """Ждёт до таймаута, как настоящий порт.

        Возвращать None сразу нельзя: поток-читатель крутился бы вхолостую
        на полной скорости и съел бы ядро процессора.
        """
        if not self._opened:
            raise ValueError("нет порта")
        try:
            return self._outbox.get(timeout=self._timeout)
        except queue.Empty:
            return None

    def _handle(self, text: str) -> None:
        """Повторяет логику handleLine() из прошивки."""
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self._reply(0, ok=False, error="BAD_JSON")
            return

        msg_id = message.get("id", 0)
        cmd = message.get("cmd")
        if cmd is None:
            self._reply(msg_id, ok=False, error="NO_CMD")
        elif cmd == CMD_PING:
            self._reply(msg_id, ok=True, data={"fw": FIRMWARE, "servo": True})
        else:
            self._reply(msg_id, ok=False, error="UNKNOWN_CMD")

    def _reply(self, msg_id: object, **fields: object) -> None:
        self._outbox.put(json.dumps({"type": TYPE_RESP, "id": msg_id, **fields}))
