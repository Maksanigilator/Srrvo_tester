"""Протокол обмена между ПК и платой ESP32.

Транспорт: USB, 115200, 8N1, полный дуплекс. Одно сообщение это одна строка
JSON, разделитель "\\n" добавляет транспортный слой, здесь его нет.

ВИДЫ СООБЩЕНИЙ
--------------
    cmd   ПК -> плата, что сделать. Несёт id.
    resp  плата -> ПК, результат команды. Несёт тот же id.
    tlm   плата -> ПК, телеметрия. Без id: ни на что не отвечает.
    evt   плата -> ПК, событие. Без id по той же причине.

КОМАНДЫ
-------
    ping                    проверка связи
    get_config              вернуть конфигурацию
    set_config              записать конфигурацию, 24 поля параметрами
    move        pos         перейти в позицию, шаг
    motor       dir speed   непрерывное вращение, dir = cw | ccw
    stop                    немедленно остановить движение
    home                    запустить поиск механического нуля

ОТВЕТЫ
------
    {"type":"resp","id":1,"ok":true,"data":{...}}
    {"type":"resp","id":1,"ok":false,"error":"UNKNOWN_CMD"}

    data у ping: {"fw":"0.1.0","servo":true,"model":777}
    servo показывает, отвечает ли сам привод: линий связи две, и одна
    может работать без другой.

    Коды ошибок: BAD_JSON, NO_CMD, UNKNOWN_CMD, BAD_PARAM, BUSY, SERVO.
    Если разобрать строку не удалось и id неизвестен, ответ идёт с id 0:
    настоящие номера начинаются с единицы.

ТЕЛЕМЕТРИЯ
----------
    {"type":"tlm","t":123456,"pos":2048,"tgt":3000,"spd":850,"load":320,
     "cur":95,"volt":121,"temp":34,"mode":"position","err":0}

    t     миллисекунды с момента загрузки платы (millis).
          Не абсолютное время: часов на плате нет. Служит осью X графиков
          и показывает НАСТОЯЩИЕ интервалы между измерениями, а не моменты
          доставки кадров.
    pos   позиция, шаг        tgt   цель, шаг
    spd   скорость, шаг/с     load  нагрузка, 1/1000, она же скважность
    cur   ток, мА             volt  напряжение, 0.1 В
    temp  температура, °C     mode  position | motor | idle
    err   байт ошибок сервы, регистр 0x41

    Медленные поля (volt, temp, mode, err) идут не в каждом кадре, а при
    изменении и раз в TLM_SLOW_EVERY кадров. Получатель обязан считать
    отсутствующее поле неизменившимся, а не обнулившимся.

    Частота: 20 Гц в движении, 5 Гц в покое. Кадр телеметрии РАСХОДНЫЙ:
    если буфер передачи полон, плата его пропускает. Ответы на команды
    не пропускаются никогда.

СОБЫТИЯ
-------
    {"type":"evt","event":"homing","status":"running"}
    {"type":"evt","event":"homing","status":"completed","zero":1024}
    {"type":"evt","event":"homing","status":"error","reason":"timeout"}
    {"type":"evt","event":"limit","reason":"max_pos","pos":2501}
    {"type":"evt","event":"fault","reason":"servo_silent"}

    Событие limit: привод в режиме непрерывного вращения вышел за
    разрешённый диапазон, и плата его остановила. Сама серва в этом режиме
    пределы 0x09 и 0x0B НЕ соблюдает, это смысл режима, поэтому следит
    прошивка по телеметрии. Отсюда точность: при 20 Гц опроса и скорости
    3400 шаг/с привод успевает проскочить до 170 шагов, это около 15
    градусов.

    Статусов ровно четыре и список закрыт: running, completed, error,
    stopped. Причина отказа идёт ОТДЕЛЬНЫМ полем reason, а не вшивается
    в статус. Благодаря этому интерфейс принимает решение по статусу,
    а причину просто показывает текстом, и новая причина в прошивке
    не требует правок в интерфейсе.

ПОРЯДОК И ОДНОВРЕМЕННОСТЬ
-------------------------
    Плата однопоточная, писатель один, каждое сообщение уходит одним
    куском. Ответ и телеметрия не могут перемешаться, даже если созрели
    в одном проходе loop: сначала ответ, потом телеметрия.

    На стороне ПК писателей несколько (каждая команда уходит своим
    потоком), поэтому запись в порт защищена замком в транспорте.
"""

import json

TYPE_CMD = "cmd"
TYPE_RESP = "resp"
TYPE_TLM = "tlm"
TYPE_EVT = "evt"

CMD_PING = "ping"
CMD_GET_CFG = "get_config"
CMD_SET_CFG = "set_config"
CMD_MOVE = "move"
CMD_MOTOR = "motor"
CMD_STOP = "stop"
CMD_HOME = "home"

ERR_BAD_JSON = "BAD_JSON"
ERR_NO_CMD = "NO_CMD"
ERR_UNKNOWN_CMD = "UNKNOWN_CMD"
ERR_BAD_PARAM = "BAD_PARAM"
ERR_BUSY = "BUSY"
ERR_SERVO = "SERVO"

EVT_HOMING = "homing"
EVT_LIMIT = "limit"
EVT_FAULT = "fault"

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"


def encode_command(msg_id: int, cmd: str, **params: object) -> str:
    """Собирает команду в строку JSON.

    Args:
        msg_id: номер команды, ответ придёт с тем же номером.
        cmd: что выполнить, одна из констант CMD_*.
        **params: дополнительные поля конкретной команды.
    """
    message = {"type": TYPE_CMD, "id": msg_id, "cmd": cmd}
    message.update(params)
    return json.dumps(message)


def decode(line: str) -> dict[str, object] | None:
    """Разбирает строку, пришедшую из порта.

    Возвращает словарь либо None, если строка непригодна
    """
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    if "type" not in message:
        return None
    return message