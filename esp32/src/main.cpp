/*
 * Прошивка платы Waveshare Servo Driver with ESP32.
 *
 * Обмен с ПК: строки JSON через USB (UART0, 115200), разделитель '\n'.
 *   ПК  -> плата: {"type":"cmd","id":1,"cmd":"ping"}
 *   плата -> ПК : {"type":"resp","id":1,"ok":true,"data":{"fw":"0.1.0","servo":true}}
 *   ошибка     : {"type":"resp","id":1,"ok":false,"error":"BAD_JSON"}
 *
 * servo в ответе на ping - отвечает ли сам сервопривод. Линий связи две
 * (ПК-плата и плата-серва), и одна может работать без другой.
 *
 * loop() не должен блокироваться: пока он занят, плата не примет STOP
 * и не пришлёт телеметрию. Поэтому никаких delay() и никаких ожиданий.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <SCServo.h>
#include <string.h>

// UART до шины сервоприводов. Пины и скорость - из мануала платы,
// раздел ESP32 Pin Function.
const uint8_t SERVO_RX = 18;
const uint8_t SERVO_TX = 19;
const uint32_t SERVO_BAUD = 1000000;

const uint8_t SERVO_ID = 1;          // заводской ID STS3215
const char FIRMWARE[] = "0.1.0";

// Длиннее этого команд не бывает; всё, что длиннее, считаем мусором.
const size_t HOST_LINE_MAX = 256;

SMS_STS st;

static char line[HOST_LINE_MAX];
static size_t lineLen = 0;
static bool lineOverflow = false;

static void sendResponse(long id, bool ok, JsonDocument &doc);
static void sendError(long id, const char *code);
static void handleLine(const char *text);
static void readFromHost();

void setup() {
  Serial.begin(115200);
  Serial1.begin(SERVO_BAUD, SERIAL_8N1, SERVO_RX, SERVO_TX);
  st.pSerial = &Serial1;
  // По умолчанию 100 мс. Это время loop() простоял бы в ожидании молчащей
  // сервы. На 1 Мбод ответ приходит меньше чем за миллисекунду.
  st.IOTimeOut = 10;
  delay(1000);                       // серве нужно время на загрузку
}

void loop() {
  readFromHost();
}

// Накапливает байты из USB до '\n' и отдаёт готовую строку в разбор.
// Строку длиннее HOST_LINE_MAX выбрасывает целиком, вместе с хвостом:
// иначе её остаток был бы разобран как отдельное сообщение.
static void readFromHost() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (!lineOverflow && lineLen > 0) {
        line[lineLen] = '\0';
        handleLine(line);
      }
      lineLen = 0;
      lineOverflow = false;
      continue;
    }
    if (c == '\r') {
      continue;
    }
    if (lineOverflow) {
      continue;                      // дочитываем испорченную строку в никуда
    }
    if (lineLen + 1 >= HOST_LINE_MAX) {
      lineOverflow = true;
      continue;
    }
    line[lineLen++] = c;
  }
}

static void handleLine(const char *text) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, text);
  if (error) {
    sendError(0, "BAD_JSON");        // id неизвестен: 0, наши начинаются с 1
    return;
  }

  long id = doc["id"] | 0L;
  const char *cmd = doc["cmd"];
  if (cmd == nullptr) {
    sendError(id, "NO_CMD");
    return;
  }

  if (strcmp(cmd, "ping") == 0) {
    JsonDocument data;
    data["fw"] = FIRMWARE;
    data["servo"] = st.Ping(SERVO_ID) != -1;
    sendResponse(id, true, data);
    return;
  }

  sendError(id, "UNKNOWN_CMD");
}

static void sendResponse(long id, bool ok, JsonDocument &data) {
  JsonDocument doc;
  doc["type"] = "resp";
  doc["id"] = id;
  doc["ok"] = ok;
  doc["data"] = data;
  serializeJson(doc, Serial);
  Serial.println();
}

static void sendError(long id, const char *code) {
  JsonDocument doc;
  doc["type"] = "resp";
  doc["id"] = id;
  doc["ok"] = false;
  doc["error"] = code;
  serializeJson(doc, Serial);
  Serial.println();
}
