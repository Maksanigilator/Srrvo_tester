"""
общение с портом
"""
import serial
from serial.tools import list_ports


def get_ports() -> list:
    """возвращает список портов"""
    return list_ports.comports()



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
        if self._serial:
            return
        if self.port == None:
            raise ValueError("порт не выбран")
        self._buffer = bytearray()
        self._serial = serial.Serial(
            port = self.port, 
            baudrate = self.baudrate, 
            timeout = self.timeout)
