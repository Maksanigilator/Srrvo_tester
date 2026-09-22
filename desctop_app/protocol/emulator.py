"""Эмулятор платы с приводом для работы без железа (п. 8 ТЗ).

Подставляется вместо MySerialTransport: реализует тот же интерфейс Transport,
поэтому Client и весь интерфейс работают без единой правки.

Изображает плату целиком: отвечает на те же команды и тем же форматом, что
прошивка ESP32, и так же сам шлёт телеметрию и события. Когда в прошивке
появляется новая команда, её надо добавить и сюда, иначе демо-режим разойдётся
с реальным устройством и перестанет что-либо проверять.

Конфигурация по умолчанию здесь СВОЯ, а не взятая из интерфейса: у настоящей
платы свои заводские значения в прошивке, и приложение о них не знает,
пока не прочитает.

Физика намеренно грубая. Задача не смоделировать привод, а дать правдоподобно
меняющиеся числа: позиция едет к цели, нагрузка растёт с ошибкой слежения,
на механическом упоре подскакивает. Этого хватает, чтобы проверить графики,
автомат homing и обработку ошибок.
"""

import json
import queue
import random
import time

from .message import (
    CMD_GET_CFG,
    CMD_HOME,
    CMD_MOTOR,
    CMD_MOVE,
    CMD_PING,
    CMD_SET_CFG,
    CMD_STOP,
    ERR_BAD_JSON,
    ERR_BAD_PARAM,
    ERR_NO_CMD,
    ERR_UNKNOWN_CMD,
    EVT_HOMING,
    EVT_LIMIT,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TYPE_EVT,
    TYPE_RESP,
    TYPE_TLM,
)

DEMO_PORT = "__demo__"
FIRMWARE = "0.2.0-demo"
MODEL = 777

POSITION_MAX = 4095
TLM_FAST_HZ = 20          # в движении
TLM_IDLE_HZ = 5           # в покое
TLM_SLOW_EVERY = 10       # как часто повторять медленные поля

FACTORY_CONFIG = {
    "home_dir": "cw", "home_speed": 200, "home_load": 350,
    "home_current": 200, "home_timeout": 10000,
    "home_travel": POSITION_MAX, "home_zero": 0,
    "speed": 1000, "torque": 1000, "accel": 10,
    "min_pos": 0, "max_pos": POSITION_MAX,
    "prot_current": 500, "prot_torque": 20, "prot_time": 200,
    "overload_torque": 80, "max_temp": 70, "min_volt": 40, "max_volt": 126,
    "pos_p": 32, "pos_d": 32, "pos_i": 0, "speed_p": 10, "speed_i": 10,
}


class FakeServo:
    """Привод: позиция, нагрузка и механический упор."""

    def __init__(self) -> None:
        self.position = 2048.0
        self.target = 2048.0
        self.speed = 0.0               # шаг/с, знак задаёт направление
        self.mode = "idle"             # idle | position | motor
        self.load = 0.0
        self.temperature = 28.0
        # Механические упоры стоят в случайных точках: так видно, что homing
        # их ищет, а не знает заранее. Их два, как у настоящего механизма.
        self.stop_low = random.uniform(150, 700)
        self.stop_high = random.uniform(3400, 3950)
        self.zero: int | None = None

    def step(self, dt: float, limits: tuple[int, int],
             ignore_limits: bool = False) -> None:
        """Шаг модели.

        ignore_limits нужен для homing: программные min/max отсчитываются от
        нуля, а до успешного поиска нуля ещё нет. Ограничивают движение только
        механические упоры.

        В режиме непрерывного вращения пределы тоже не применяются: настоящая
        серва их в этом режиме не соблюдает, и эмулятор обязан врать так же,
        иначе демо-режим покажет безопасность, которой на железе нет.
        За диапазоном следит плата, см. DemoTransport._step.
        """
        # Пределы соблюдает только позиционный режим: в остальных
        # серва их не применяет, а без привода она никуда не прыгает.
        free = ignore_limits or self.mode != "position"
        low, high = (0.0, float(POSITION_MAX)) if free else limits
        wanted = 0.0
        if self.mode == "position":
            error = self.target - self.position
            wanted = max(-self.speed, min(self.speed, error / max(dt, 1e-3)))
        elif self.mode == "motor":
            wanted = self.speed

        target_pos = self.position + wanted * dt
        effort = abs(wanted) / 3400.0
        if target_pos <= self.stop_low or target_pos >= self.stop_high:
            # Упёрлись в железо: привод давит, нагрузка уходит в потолок.
            self.position = max(self.stop_low, min(self.stop_high, target_pos))
            self.load = 900 + random.uniform(-30, 30) if wanted else 20.0
        elif target_pos < low or target_pos > high:
            # Программный предел: движения нет, но и давить не во что.
            self.position = max(low, min(high, target_pos))
            self.load = 40 + random.uniform(-10, 10)
        else:
            self.position = target_pos
            self.load = 40 + 700 * effort + random.uniform(-15, 15)
        if self.mode == "idle":
            self.load = random.uniform(5, 25)
        self.temperature += (self.load / 1000.0) * dt * 0.4 - dt * 0.05
        self.temperature = max(24.0, min(85.0, self.temperature))

    @property
    def current(self) -> int:
        return int(self.load * 3.2)

    @property
    def voltage(self) -> int:
        return int(121 - self.load * 0.004)          # 0.1 В, просадка под нагрузкой


class Homing:
    """Автомат поиска упора. Повторяет логику, задуманную для прошивки."""

    def __init__(self, servo: FakeServo, config: dict, emit, done) -> None:
        self._servo = servo
        self._done = done
        self._config = config
        self._emit = emit
        self._started = time.monotonic()
        self._from = servo.position
        self._hits = 0
        servo.mode = "motor"
        direction = -1 if config["home_dir"] == "cw" else 1
        servo.speed = direction * config["home_speed"]
        self._emit(EVT_HOMING, status=STATUS_RUNNING)

    def step(self) -> bool:
        """Возвращает True, пока поиск продолжается."""
        elapsed = (time.monotonic() - self._started) * 1000
        if elapsed > self._config["home_timeout"]:
            return self._fail("timeout")
        if abs(self._servo.position - self._from) > self._config["home_travel"]:
            return self._fail("travel_exceeded")

        over = (self._servo.load >= self._config["home_load"]
                or self._servo.current >= self._config["home_current"] * 6.5)
        # Упором считаем только НЕСКОЛЬКО замеров подряд: на разгоне нагрузка
        # скачет, и одиночное превышение это ещё не упор.
        self._hits = self._hits + 1 if over else 0
        if self._hits >= 3 and elapsed > 200:
            self._servo.speed = 0.0
            self._servo.mode = "idle"
            self._servo.zero = int(self._servo.position)
            # Найденный упор становится нулём: дальше позиции протокола
            # отсчитываются от него.
            self._done(self._servo.position)
            self._emit(EVT_HOMING, status=STATUS_COMPLETED,
                       zero=self._config["home_zero"],
                       zero_raw=int(self._servo.position))
            return False
        return True

    def _fail(self, reason: str) -> bool:
        self._servo.speed = 0.0
        self._servo.mode = "idle"
        self._emit(EVT_HOMING, status=STATUS_ERROR, reason=reason)
        return False

    def abort(self) -> None:
        self._servo.speed = 0.0
        self._servo.mode = "idle"
        self._emit(EVT_HOMING, status=STATUS_STOPPED)


class DemoTransport:
    """Воображаемая плата на другом конце провода."""

    def __init__(self, timeout: float = 0.1) -> None:
        self._timeout = timeout
        self._opened = False
        self._outbox: queue.Queue[str] = queue.Queue()
        self._config = dict(FACTORY_CONFIG)
        self._servo = FakeServo()
        self._homing: Homing | None = None
        # Система координат, как в прошивке: zero_raw это сырая позиция,
        # соответствующая config["home_zero"]. До homing преобразование
        # тождественно.
        self._zero_raw = float(FACTORY_CONFIG["home_zero"])
        self._boot = time.monotonic()
        self._last_step = time.monotonic()
        self._next_tlm = 0.0
        self._frame = 0

    # --- интерфейс Transport ----------------------------------------------

    def open(self) -> None:
        self._opened = True
        while not self._outbox.empty():
            self._outbox.get_nowait()
        self._boot = time.monotonic()
        self._last_step = time.monotonic()

    def close(self) -> None:
        self._opened = False

    def write_line(self, text: str) -> None:
        if not self._opened:
            raise ValueError("нет порта")
        self._handle(text)

    def read_line(self) -> str | None:
        """Ждёт до таймаута, как настоящий порт.

        Порядок тот же, что в loop() прошивки: сначала отдаём готовые ответы
        и события, и только потом телеметрию. Кадр телеметрии расходный.
        """
        if not self._opened:
            raise ValueError("нет порта")
        self._step()
        try:
            return self._outbox.get_nowait()
        except queue.Empty:
            pass
        now = time.monotonic()
        if now >= self._next_tlm:
            moving = self._servo.mode != "idle"
            self._next_tlm = now + 1.0 / (TLM_FAST_HZ if moving else TLM_IDLE_HZ)
            return self._telemetry()
        time.sleep(min(self._timeout, max(0.0, self._next_tlm - now)))
        return None

    # --- внутреннее --------------------------------------------------------

    def _step(self) -> None:
        now = time.monotonic()
        dt = min(now - self._last_step, 0.2)
        self._last_step = now
        limits = (self._to_raw(self._config["min_pos"]),
                  self._to_raw(self._config["max_pos"]))
        self._servo.step(dt, limits, ignore_limits=self._homing is not None)
        if self._homing is not None and not self._homing.step():
            self._homing = None
        self._watch_limits((self._config["min_pos"], self._config["max_pos"]))

    def _watch_limits(self, limits: tuple[int, int]) -> None:
        """Останавливает непрерывное вращение на границе диапазона.

        Повторяет сторожа из прошивки: в этом режиме серва пределы не
        соблюдает, поэтому за ними следит плата по телеметрии.
        """
        if self._servo.mode != "motor" or self._homing is not None:
            return
        low, high = limits
        here = self._to_user(self._servo.position)
        if here < low:
            reason = "min_pos"
        elif here > high:
            reason = "max_pos"
        else:
            return
        self._servo.speed = 0.0
        self._servo.mode = "idle"
        self._servo.target = self._servo.position
        self._emit(EVT_LIMIT, reason=reason, pos=here)

    def _to_user(self, raw: float) -> int:
        return int(raw - self._zero_raw + self._config["home_zero"])

    def _to_raw(self, user: float) -> float:
        return user + self._zero_raw - self._config["home_zero"]

    def _telemetry(self) -> str:
        self._frame += 1
        servo = self._servo
        frame = {
            "type": TYPE_TLM,
            "t": int((time.monotonic() - self._boot) * 1000),
            "pos": self._to_user(servo.position),
            "tgt": self._to_user(servo.target),
            "spd": int(servo.speed if servo.mode == "motor"
                       else (servo.target - servo.position) * 2),
            "load": int(servo.load),
            "cur": servo.current,
        }
        # Медленные поля повторяем редко: экономит треть кадра.
        if self._frame % TLM_SLOW_EVERY == 1:
            frame.update(volt=servo.voltage, temp=int(servo.temperature),
                         mode=servo.mode, err=0)
        return json.dumps(frame, separators=(",", ":"))

    def _emit(self, event: str, **fields: object) -> None:
        self._outbox.put(json.dumps(
            {"type": TYPE_EVT, "event": event, **fields}, separators=(",", ":")))

    def _reply(self, msg_id: object, **fields: object) -> None:
        self._outbox.put(json.dumps(
            {"type": TYPE_RESP, "id": msg_id, **fields}, separators=(",", ":")))

    def _handle(self, text: str) -> None:
        """Повторяет логику handleLine() из прошивки."""
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self._reply(0, ok=False, error=ERR_BAD_JSON)
            return
        msg_id = message.get("id", 0)
        cmd = message.get("cmd")
        if cmd is None:
            self._reply(msg_id, ok=False, error=ERR_NO_CMD)
        elif cmd == CMD_PING:
            self._reply(msg_id, ok=True,
                        data={"fw": FIRMWARE, "servo": True, "model": MODEL})
        elif cmd == CMD_GET_CFG:
            self._reply(msg_id, ok=True, data=dict(self._config))
        elif cmd == CMD_SET_CFG:
            self._set_config(msg_id, message)
        elif cmd == CMD_MOVE:
            self._move(msg_id, message)
        elif cmd == CMD_MOTOR:
            self._motor(msg_id, message)
        elif cmd == CMD_STOP:
            self._stop(msg_id)
        elif cmd == CMD_HOME:
            self._home(msg_id)
        else:
            self._reply(msg_id, ok=False, error=ERR_UNKNOWN_CMD)

    def _set_config(self, msg_id, message: dict) -> None:
        values = {k: v for k, v in message.items()
                  if k not in ("type", "id", "cmd")}
        unknown = set(values) - set(FACTORY_CONFIG)
        if unknown:
            self._reply(msg_id, ok=False, error=ERR_BAD_PARAM,
                        data={"field": sorted(unknown)[0], "value": 0})
            return
        if values.get("min_pos", 0) >= values.get("max_pos", POSITION_MAX):
            self._reply(msg_id, ok=False, error=ERR_BAD_PARAM,
                        data={"field": "min_pos", "value": values.get("min_pos", 0)})
            return
        self._config.update(values)
        self._reply(msg_id, ok=True, data={"written": len(values)})

    def _move(self, msg_id, message: dict) -> None:
        if self._homing is not None:
            self._reply(msg_id, ok=False, error="BUSY")
            return
        pos = message.get("pos")
        low, high = self._config["min_pos"], self._config["max_pos"]
        if not isinstance(pos, int) or not low <= pos <= high:
            self._reply(msg_id, ok=False, error=ERR_BAD_PARAM,
                        data={"field": "pos", "value": pos})
            return
        self._servo.mode = "position"
        self._servo.target = self._to_raw(pos)
        self._servo.speed = float(self._config["speed"])
        self._reply(msg_id, ok=True, data={"pos": pos})

    def _motor(self, msg_id, message: dict) -> None:
        if self._homing is not None:
            self._reply(msg_id, ok=False, error="BUSY")
            return
        direction = message.get("dir")
        speed = message.get("speed", self._config["speed"])
        if direction not in ("cw", "ccw") or not isinstance(speed, int):
            self._reply(msg_id, ok=False, error=ERR_BAD_PARAM,
                        data={"field": "dir", "value": 0})
            return
        # Если уже за границей, наружу не пускаем, а внутрь разрешаем.
        low, high = self._config["min_pos"], self._config["max_pos"]
        here = self._to_user(self._servo.position)
        outward = ((here <= low and direction == "cw")
                   or (here >= high and direction == "ccw"))
        if outward:
            self._reply(msg_id, ok=False, error=ERR_BAD_PARAM,
                        data={"field": "pos", "value": here})
            return
        self._servo.mode = "motor"
        self._servo.speed = speed * (-1 if direction == "cw" else 1)
        self._reply(msg_id, ok=True, data={"dir": direction, "speed": speed})

    def _stop(self, msg_id) -> None:
        """STOP работает всегда, в том числе посреди homing."""
        if self._homing is not None:
            self._homing.abort()
            self._homing = None
        self._servo.speed = 0.0
        self._servo.mode = "idle"
        self._servo.target = self._servo.position
        self._reply(msg_id, ok=True)

    def _set_zero(self, raw: float) -> None:
        self._zero_raw = raw

    def _home(self, msg_id) -> None:
        if self._homing is not None:
            self._reply(msg_id, ok=False, error="BUSY")
            return
        self._reply(msg_id, ok=True)        # результат придёт событием
        self._homing = Homing(self._servo, self._config, self._emit,
                              self._set_zero)
