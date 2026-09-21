"""
общение с портом
"""

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
        self._serial.write((text + "\n").encode()) #encode str -> bytes


    def read_line(self) -> str | None:
        """"""
        if self._serial == None:
            raise ValueError("нет порта")
        count = self._serial.in_waiting
        if count == 0:
            count = 1
        data = self._serial.read(count)
        #беру байты из буфера чтения и добавляю в буфер для сообщения
        self._buffer += data
        i = self._buffer.find(b"\n")#индекс разделителя

        if i == -1:
            if len(self._buffer) > MESSAGE_MAX_LINE:
                self._buffer.clear()
            return None

        message = self._buffer[:i].decode(errors="replace").rstrip("\r")#обрезаю до i. errors="replace" превращает ошибку в символ ? . .rstrip("\r") обрезает /r
        del self._buffer[:i+1]
        return message

