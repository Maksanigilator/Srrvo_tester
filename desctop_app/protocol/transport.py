"""
общение с портом
"""

import threading
from typing import Protocol

import serial
from serial.tools import list_ports


MESSAGE_MAX_LINE = 4096


def get_ports() -> list:
    """возвращает список портов"""
    return list_ports.comports()



class Transport(Protocol):
    """Что клиент требует от канала связи.

    Структурный интерфейс: подходит любой объект с этими методами,
    наследоваться не нужно. Благодаря этому вместо настоящего порта
    можно подставить эмулятор (п. 8 ТЗ).
    """

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write_line(self, text: str) -> None: ...
    def read_line(self) -> str | None: ...


class MySerialTransport: 

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        timeout: float = 0.1,
        ) -> None:

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: serial.Serial | None = None
        # Писателей может быть несколько: каждая команда уходит своим потоком.
        # write() не атомарен, и без замка байты двух команд перемешались бы
        # в линии. Замок только на запись: чтению мешать нечему.
        self._write_lock = threading.Lock()
        self._buffer = bytearray() #список байтов
    
    def open(self) -> None:
        """открывает порт"""
        if self._serial:
            return
        if self.port == None:
            raise ValueError("порт не выбран")
        self._buffer = bytearray()
        self._serial = serial.Serial(
            port = self.port, 
            baudrate = self.baudrate, 
            timeout = self.timeout) #объект сериал часть нашего объекта (композиция)

    def close(self) -> None:
        """закрывает порт"""
        if self._serial == None:
            return
        try: #на случай если уже выдернули
            self._serial.close()
        except serial.SerialException:
            pass
        self._serial = None

    def write_line(self, text: str) -> None:
        """отправляем сообщение без проверки успеха"""
        if self._serial == None:
            raise ValueError("нет порта")
        with self._write_lock:
            self._serial.write((text + "\n").encode())  # str -> bytes


    def read_line(self) -> str | None:
        """Возвращает очередную полную строку или None.

        Сначала проверяется собственный буфер и только потом порт: за один
        read может прийти несколько строк, и если лезть в порт первым, каждая
        следующая будет ждать полный таймаут. На пачке из двадцати строк это
        давало почти две секунды задержки.
        """
        if self._serial == None:
            raise ValueError("нет порта")

        message = self._take_line()
        if message is not None:
            return message

        count = self._serial.in_waiting
        if count == 0:
            count = 1
        self._buffer += self._serial.read(count)
        return self._take_line()

    def _take_line(self) -> str | None:
        """Отрезает из буфера первую готовую строку, если она там есть."""
        i = self._buffer.find(b"\n")
        if i == -1:
            if len(self._buffer) > MESSAGE_MAX_LINE:
                self._buffer.clear()
            return None
        # errors="replace" оставляет мусор видимым, а не роняет поток чтения
        message = self._buffer[:i].decode(errors="replace").rstrip("\r")
        del self._buffer[:i + 1]
        return message
