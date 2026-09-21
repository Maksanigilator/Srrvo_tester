"""Клиент: отправка команд, ожидание ответов, фоновое чтение.

два потока:

1) Цикл событий Qt, весь интерфейс. Из него вызываются
   connect(), disconnect() и send_command().
2) Читатель создаётся в connect(), живёт до disconnect().
   transport.read_line() по кругу и отдает ответы тому, кто их ждёт,остальное
   в очередь incoming.

Не асинхрон а многопоток
pyserial блокирующий, его read() остановил бы цикл событий целиком.
Обёртка pyserial-asyncio (версия 0.6 последний релиз 2021) не заявляет
поддержку Windows

"""

import itertools
import queue
import threading
from collections.abc import Callable

from .message import TYPE_RESP, decode, encode_command
from .transport import Transport


class Client:
    """Связывает транспорт с протоколом.

    Держит фоновый поток, который читает строки из порта. Ответы отдаёт
    тому, кто ждёт команду с таким же id. Телеметрию и события кладёт
    в очередь incoming.
    """

    def __init__(self, transport: Transport, timeout: float = 1.0) -> None:
        self._transport = transport
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue[dict]] = {}
        self.incoming: queue.Queue[dict] = queue.Queue()
        self.raw: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=2000)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.on_lost: Callable[[Exception], None] | None = None

    def connect(self) -> None:
        self._transport.open()
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._transport.close()

    def send_command(self, cmd: str, **params: object) -> dict:
        """Отправляет команду и ждёт ответ с тем же id."""
        msg_id = next(self._ids)
        box: queue.Queue = queue.Queue(maxsize=1)
        self._pending[msg_id] = box
        try:
            line = encode_command(msg_id, cmd, **params)
            self._log_raw(">", line)
            self._transport.write_line(line)
            return box.get(timeout=self._timeout)
        except queue.Empty:
            raise TimeoutError(f"нет ответа на {cmd} (id={msg_id})") from None
        finally:
            self._pending.pop(msg_id, None)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._transport.read_line()
            except OSError as error:
                if self.on_lost is not None:
                    self.on_lost(error)
                return
            if line is None:
                continue
            self._log_raw("<", line)
            message = decode(line)
            if message is None:
                continue
            if message.get("type") == TYPE_RESP:
                box = self._pending.get(message.get("id"))
                if box is not None:
                    box.put(message)
            else:
                self.incoming.put(message)

    def _log_raw(self, direction: str, line: str) -> None:
        """Складывает строку в очередь для консоли.

        Очередь ограничена: если её никто не разгребает, старые строки
        выбрасываются, иначе она съест память за час работы.
        """
        try:
            self.raw.put_nowait((direction, line))
        except queue.Full:
            try:
                self.raw.get_nowait()
                self.raw.put_nowait((direction, line))
            except (queue.Empty, queue.Full):
                pass
