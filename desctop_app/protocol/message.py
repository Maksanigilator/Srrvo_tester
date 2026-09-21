"""
Формат сообщений между компом и ESP32.

Одно сообщение - одна строка JSON. Разделитель "\\n" добавляет транспорт

ПК -> ESP32:
    {"type": "cmd", "id": 1, "cmd": "ping"}

ESP32 -> ПК:
    {"type": "resp", "id": 1, "ok": true,  "data": {"fw": "0.1.0"}}
    {"type": "resp", "id": 1, "ok": false, "error": "BAD_JSON"}

Поля:
    type  - вид сообщения: cmd или resp
    id    - номер команды; ответ несёт тот же номер
    cmd   - что выполнить
    ok    - удалась ли команда
    data  - полезная нагрузка ответа
    error - код ошибки, когда ok = false

"""

import json

TYPE_CMD = "cmd"
TYPE_RESP = "resp"
CMD_PING = "ping"


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