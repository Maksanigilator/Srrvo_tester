/*
 * Прошивка платы Waveshare Servo Driver with ESP32 (SKU 21593)
 * для сервопривода Feetech STS3215.
 *
 * Протокол обмена с ПК описан в desktop-части, файл protocol/message.py.
 * Кратко: одна строка JSON на сообщение, разделитель '\n', четыре вида
 * сообщений (cmd, resp, tlm, evt), семь команд.
 *
 * ГЛАВНОЕ ПРАВИЛО: loop() не блокируется. Пока он занят, плата не примет
 * STOP и не пришлёт телеметрию, поэтому никаких delay() и никаких ожиданий
 * длиннее миллисекунд. Homing поэтому сделан конечным автоматом, а не циклом.
 *
 * Две вещи всё же ждут, и обе ограничены сверху:
 *   обмен с сервой  ограничен st.IOTimeOut, снижен со 100 мс до 10;
 *   запись в USB    блокируется при полном буфере, поэтому перед телеметрией
 *                   проверяется availableForWrite, и кадр пропускается.
 *                   Ответы на команды не пропускаются никогда.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SCServo.h>
#include <string.h>

// Пины и скорость шины сервоприводов: мануал платы, раздел ESP32 Pin Function.
const uint8_t SERVO_RX = 18;
const uint8_t SERVO_TX = 19;
const uint32_t SERVO_BAUD = 1000000;
const uint8_t SERVO_ID = 1;

const char FIRMWARE[] = "0.3.0";
const size_t HOST_LINE_MAX = 512;          // длиннее команд не бывает
const uint32_t TLM_FAST_MS = 50;           // 20 Гц в движении
const uint32_t TLM_IDLE_MS = 200;          // 5 Гц в покое
const uint8_t TLM_SLOW_EVERY = 10;         // как часто повторять медленные поля
const size_t TLM_RESERVE = 200;            // сколько места нужно под кадр
const uint8_t SERVO_FAIL_LIMIT = 5;        // после стольких неответов считаем молчащей
/* Плата питается отдельно, и выдернутый USB её не останавливает. Если во
 * время движения от ПК нет команд дольше этого времени, привод стоп.
 * Приложение, пока движение идёт, шлёт ping как подтверждение присутствия. */
const uint32_t HOST_TIMEOUT_MS = 3000;

SMS_STS st;
Preferences storage;

// ---------------------------------------------------------------- конфигурация

enum ConfigIndex {
  HOME_DIR, HOME_SPEED, HOME_LOAD, HOME_CURRENT, HOME_TIMEOUT,
  HOME_TRAVEL, HOME_ZERO,
  SPEED, TORQUE, ACCEL, MIN_POS, MAX_POS,
  PROT_CURRENT, PROT_TORQUE, PROT_TIME, OVERLOAD_TORQUE,
  MAX_TEMP, MIN_VOLT, MAX_VOLT,
  POS_P, POS_D, POS_I, SPEED_P, SPEED_I,
  CONFIG_COUNT
};

struct ConfigItem {
  const char *name;      // имя поля в JSON и ключ в NVS
  int32_t def;           // заводское значение
  uint8_t reg;           // адрес регистра сервы, 0 если параметр наш
  bool word;             // регистр двухбайтовый
};

// Направление хранится числом (0 = cw), в JSON переводится в строку:
// в NVS и в таблице удобнее однородные целые.
const ConfigItem CONFIG[CONFIG_COUNT] = {
  {"home_dir",        0,    0,    false},
  {"home_speed",      200,  0,    false},
  {"home_load",       350,  0,    false},
  {"home_current",    200,  0,    false},
  {"home_timeout",    10000,0,    false},
  {"home_travel",     4095, 0,    false},
  {"home_zero",       0,    0,    false},
  {"speed",           1000, 0,    false},   // передаётся с каждой командой движения
  {"torque",          1000, 0x30, true},
  {"accel",           10,   0,    false},   // тоже передаётся с командой
  {"min_pos",         0,    0x09, true},
  {"max_pos",         4095, 0x0B, true},
  {"prot_current",    500,  0x1C, true},
  {"prot_torque",     20,   0x22, false},
  {"prot_time",       200,  0x23, false},
  {"overload_torque", 80,   0x24, false},
  {"max_temp",        70,   0x0D, false},
  {"min_volt",        40,   0x0F, false},
  {"max_volt",        126,  0x0E, false},
  {"pos_p",           32,   0x15, false},
  {"pos_d",           32,   0x16, false},
  {"pos_i",           0,    0x17, false},
  {"speed_p",         10,   0x25, false},
  {"speed_i",         10,   0x27, false},
};

const uint32_t CONFIG_VERSION = 2;
int32_t config[CONFIG_COUNT];

// -------------------------------------------------------------------- состояние

enum Mode { MODE_IDLE, MODE_POSITION, MODE_MOTOR };
Mode mode = MODE_IDLE;
// Режим работы это регистр 0x21 в EPROM. Помним его здесь и пишем в серву
// только при реальной смене, иначе каждое нажатие CW жгло бы ячейку.
int servoMode = -1;
int32_t targetPos = 2048;

/* Система координат.
 *
 * zeroRaw это сырая позиция сервы, которая соответствует config[HOME_ZERO].
 * До успешного homing преобразование тождественно. После него нулём
 * становится найденный упор, и все позиции протокола отсчитываются от него.
 *
 * Запись 128 в 0x28 сознательно НЕ используется: она жёстко назначает
 * текущей точке значение 2048, что бы ни стояло в home_zero, и пишет
 * смещение в EPROM. Считать сдвиг на плате дешевле и точнее.
 */
int32_t zeroRaw = 0;

static int32_t toUser(int32_t raw) { return raw - zeroRaw + config[HOME_ZERO]; }
static int32_t toRaw(int32_t user) { return user + zeroRaw - config[HOME_ZERO]; }

enum HomingState { HOMING_OFF, HOMING_RUN };
HomingState homing = HOMING_OFF;
uint32_t homingStarted = 0;
int32_t homingFrom = 0;
uint8_t homingHits = 0;

static char line[HOST_LINE_MAX];
static size_t lineLen = 0;
static bool lineOverflow = false;

uint32_t lastTlm = 0;
uint32_t lastHostCmd = 0;
uint32_t tlmFrame = 0;
uint8_t servoFails = 0;
bool servoSilentReported = false;

struct Feedback {
  int pos, spd, load, cur, volt, temp, err;
  bool valid;
};
Feedback last = {0, 0, 0, 0, 0, 0, 0, false};

// --------------------------------------------------------------------- отправка

static void sendLine(JsonDocument &doc) {
  serializeJson(doc, Serial);
  Serial.println();
}

static void reply(long id, bool ok, JsonDocument &data, const char *error) {
  JsonDocument doc;
  doc["type"] = "resp";
  doc["id"] = id;
  doc["ok"] = ok;
  if (error) {
    doc["error"] = error;
  }
  if (!data.isNull()) {
    doc["data"] = data;
  }
  sendLine(doc);
}

static void replyOk(long id) {
  JsonDocument empty;
  reply(id, true, empty, nullptr);
}

static void replyError(long id, const char *code) {
  JsonDocument empty;
  reply(id, false, empty, code);
}

/* Отказ с указанием поля: иначе по одному BAD_PARAM непонятно,
 * что именно не понравилось. Эмулятор подробности передаёт, прошивка
 * должна вести себя так же. */
static void replyBadParam(long id, const char *field, int32_t value) {
  JsonDocument data;
  data["field"] = field;
  data["value"] = value;
  reply(id, false, data, "BAD_PARAM");
}

/* Останавливает непрерывное вращение на границе диапазона.
 *
 * В этом режиме серва пределы 0x09 и 0x0B НЕ соблюдает, это смысл режима,
 * поэтому следит прошивка по телеметрии. Точность ограничена периодом
 * опроса: при 20 Гц и 3400 шаг/с привод успевает проскочить до 170 шагов.
 */
static void emitFault(const char *reason) {
  JsonDocument doc;
  doc["type"] = "evt";
  doc["event"] = "fault";
  doc["reason"] = reason;
  sendLine(doc);
}

static void emitLimit(const char *reason, int pos) {
  JsonDocument doc;
  doc["type"] = "evt";
  doc["event"] = "limit";
  doc["reason"] = reason;
  doc["pos"] = pos;
  sendLine(doc);
}

static void emitHoming(const char *status, const char *reason, bool withZero) {
  JsonDocument doc;
  doc["type"] = "evt";
  doc["event"] = "homing";
  doc["status"] = status;
  if (reason) {
    doc["reason"] = reason;
  }
  if (withZero) {
    doc["zero"] = config[HOME_ZERO];      // в пользовательских координатах
    doc["zero_raw"] = zeroRaw;            // сырая позиция найденного упора
  }
  sendLine(doc);
}

// ------------------------------------------------------------------- работа с NVS

static void configDefaults() {
  for (int i = 0; i < CONFIG_COUNT; i++) {
    config[i] = CONFIG[i].def;
  }
}

static void configLoad() {
  storage.begin("servo", true);
  uint32_t version = storage.getUInt("ver", 0);
  if (version != CONFIG_VERSION) {
    // Состав параметров поменялся: старые данные прочитались бы как мусор.
    storage.end();
    configDefaults();
    return;
  }
  for (int i = 0; i < CONFIG_COUNT; i++) {
    config[i] = storage.getInt(CONFIG[i].name, CONFIG[i].def);
  }
  storage.end();
}

static void configSave() {
  storage.begin("servo", false);
  storage.putUInt("ver", CONFIG_VERSION);
  for (int i = 0; i < CONFIG_COUNT; i++) {
    storage.putInt(CONFIG[i].name, config[i]);
  }
  storage.end();
}

// ------------------------------------------------------------------ работа с сервой

/* Пишет в серву те параметры, которые изменились.
 * Большинство этих регистров в EPROM, и переписывать все семнадцать при
 * каждом нажатии Write означало бы изнашивать ячейки впустую.
 */
static int applyToServo(const int32_t *previous) {
  int written = 0;
  bool unlocked = false;
  for (int i = 0; i < CONFIG_COUNT; i++) {
    if (CONFIG[i].reg == 0) {
      continue;
    }
    if (previous && previous[i] == config[i]) {
      continue;
    }
    if (!unlocked) {
      st.unLockEprom(SERVO_ID);
      unlocked = true;
    }
    int32_t value = config[i];
    if (i == MIN_POS || i == MAX_POS) {
      // Серва проверяет пределы в своих сырых координатах.
      value = toRaw(value);
    }
    if (CONFIG[i].word) {
      st.writeWord(SERVO_ID, CONFIG[i].reg, (uint16_t)value);
    } else {
      st.writeByte(SERVO_ID, CONFIG[i].reg, (uint8_t)value);
    }
    written++;
  }
  if (unlocked) {
    st.LockEprom(SERVO_ID);
  }
  return written;
}

/* Перезаписывает 0x09 и 0x0B после сдвига системы координат. */
static void applyLimits() {
  st.unLockEprom(SERVO_ID);
  st.writeWord(SERVO_ID, 0x09, (uint16_t)toRaw(config[MIN_POS]));
  st.writeWord(SERVO_ID, 0x0B, (uint16_t)toRaw(config[MAX_POS]));
  st.LockEprom(SERVO_ID);
}

static void setServoMode(int wanted) {
  if (servoMode == wanted) {
    return;
  }
  st.unLockEprom(SERVO_ID);
  st.writeByte(SERVO_ID, 33, (uint8_t)wanted);   // 0x21, режим работы
  st.LockEprom(SERVO_ID);
  servoMode = wanted;
}

/* Одно чтение вместо шести: FeedBack забирает 15 байт с адреса 0x38,
 * дальше Read*(-1) берут значения из кэша, не обращаясь к шине.
 */
static bool readFeedback() {
  if (st.FeedBack(SERVO_ID) == -1) {
    servoFails = servoFails < 255 ? servoFails + 1 : 255;
    last.valid = false;
    return false;
  }
  servoFails = 0;
  servoSilentReported = false;
  last.pos = st.ReadPos(-1);
  last.spd = st.ReadSpeed(-1);
  last.load = st.ReadLoad(-1);
  last.cur = st.ReadCurrent(-1);
  last.volt = st.ReadVoltage(-1);
  last.temp = st.ReadTemper(-1);
  // getErr это флаг ошибки ОБМЕНА библиотеки, а статус сервы лежит
  // в SCS::Error, куда его кладёт SCS::Read из байта ответа.
  last.err = st.Error;
  last.valid = true;
  return true;
}

// ---------------------------------------------------------------------- телеметрия

static void sendTelemetry() {
  JsonDocument doc;
  doc["type"] = "tlm";
  doc["t"] = millis();
  doc["pos"] = toUser(last.pos);
  doc["tgt"] = toUser(targetPos);
  doc["spd"] = last.spd;
  doc["load"] = last.load;
  doc["cur"] = (int)(last.cur * 6.5f);          // единица регистра 6.5 мА
  // Медленные поля повторяем редко: экономит треть кадра.
  if (tlmFrame % TLM_SLOW_EVERY == 1) {
    doc["volt"] = last.volt;
    doc["temp"] = last.temp;
    doc["mode"] = mode == MODE_POSITION ? "position"
                  : mode == MODE_MOTOR  ? "motor" : "idle";
    doc["err"] = last.err;
  }
  sendLine(doc);
}

// -------------------------------------------------------------------------- homing

static void homingStop() {
  st.WriteSpe(SERVO_ID, 0, 0);
  mode = MODE_IDLE;
  homing = HOMING_OFF;
}

static void stopMotion();

static void homingBegin(long id) {
  homingStarted = millis();
  homingFrom = last.pos;
  homingHits = 0;
  homing = HOMING_RUN;
  mode = MODE_MOTOR;
  setServoMode(1);                              // режим постоянной скорости
  int16_t speed = (int16_t)config[HOME_SPEED];
  st.WriteSpe(SERVO_ID, config[HOME_DIR] == 0 ? -speed : speed, 0);
  replyOk(id);                                  // результат придёт событием
  emitHoming("running", nullptr, false);
}

static void homingStep() {
  uint32_t elapsed = millis() - homingStarted;
  if (elapsed > (uint32_t)config[HOME_TIMEOUT]) {
    homingStop();
    emitHoming("error", "timeout", false);
    return;
  }
  if (abs(last.pos - homingFrom) > config[HOME_TRAVEL]) {
    homingStop();
    emitHoming("error", "travel_exceeded", false);
    return;
  }
  if (!last.valid) {
    if (servoFails >= SERVO_FAIL_LIMIT) {
      homingStop();
      emitHoming("error", "servo_silent", false);
    }
    return;
  }
  bool over = last.load >= config[HOME_LOAD] || last.cur >= config[HOME_CURRENT];
  // Упором считаем только несколько замеров подряд: на разгоне нагрузка
  // скачет, и одиночное превышение это ещё не упор. Первые 200 мс не в счёт.
  homingHits = over ? homingHits + 1 : 0;
  if (homingHits >= 3 && elapsed > 200) {
    homingStop();
    setServoMode(0);
    // Текущую позицию объявляем нулём: запись 128 в регистр 0x28 делает
    // это средствами самой сервы.
    // Найденный упор объявляем нулём: с этого момента toUser(last.pos)
    // возвращает ровно config[HOME_ZERO].
    zeroRaw = last.pos;
    storage.begin("servo", false);
    storage.putInt("zero_raw", zeroRaw);
    storage.end();
    // Пределы заданы в новых координатах, значит их сырые значения уехали.
    applyLimits();
    emitHoming("completed", nullptr, true);
  }
}

// ---------------------------------------------------------------------- команды

static void cmdPing(long id) {
  JsonDocument data;
  data["fw"] = FIRMWARE;
  data["servo"] = st.Ping(SERVO_ID) != -1;
  data["model"] = st.readWord(SERVO_ID, 3);     // 0x03, код модели
  reply(id, true, data, nullptr);
}

static void cmdGetConfig(long id) {
  JsonDocument data;
  for (int i = 0; i < CONFIG_COUNT; i++) {
    if (i == HOME_DIR) {
      data[CONFIG[i].name] = config[i] == 0 ? "cw" : "ccw";
    } else {
      data[CONFIG[i].name] = config[i];
    }
  }
  reply(id, true, data, nullptr);
}

static void cmdSetConfig(long id, JsonDocument &message) {
  int32_t previous[CONFIG_COUNT];
  memcpy(previous, config, sizeof(config));

  for (int i = 0; i < CONFIG_COUNT; i++) {
    JsonVariant value = message[CONFIG[i].name];
    if (value.isNull()) {
      continue;
    }
    if (i == HOME_DIR) {
      const char *dir = value.as<const char *>();
      if (!dir || (strcmp(dir, "cw") != 0 && strcmp(dir, "ccw") != 0)) {
        replyBadParam(id, "home_dir", 0);
        return;
      }
      config[i] = strcmp(dir, "cw") == 0 ? 0 : 1;
    } else {
      config[i] = value.as<int32_t>();
    }
  }
  if (config[MIN_POS] >= config[MAX_POS]) {
    int32_t bad = config[MIN_POS];
    memcpy(config, previous, sizeof(config));
    replyBadParam(id, "min_pos", bad);
    return;
  }
  int written = applyToServo(previous);
  configSave();

  JsonDocument data;
  data["written"] = written;
  reply(id, true, data, nullptr);
}

static void cmdMove(long id, JsonDocument &message) {
  if (homing != HOMING_OFF) {
    replyError(id, "BUSY");
    return;
  }
  JsonVariant value = message["pos"];
  if (value.isNull()) {
    replyBadParam(id, "pos", 0);
    return;
  }
  int32_t pos = value.as<int32_t>();
  if (pos < config[MIN_POS] || pos > config[MAX_POS]) {
    replyBadParam(id, "pos", pos);
    return;
  }
  setServoMode(0);
  mode = MODE_POSITION;
  targetPos = toRaw(pos);
  st.WritePosEx(SERVO_ID, (s16)targetPos, (u16)config[SPEED], (u8)config[ACCEL]);
  JsonDocument data;
  data["pos"] = pos;
  reply(id, true, data, nullptr);
}

static void cmdMotor(long id, JsonDocument &message) {
  if (homing != HOMING_OFF) {
    replyError(id, "BUSY");
    return;
  }
  const char *dir = message["dir"].as<const char *>();
  if (!dir || (strcmp(dir, "cw") != 0 && strcmp(dir, "ccw") != 0)) {
    replyBadParam(id, "dir", 0);
    return;
  }
  int32_t speed = message["speed"].isNull() ? config[SPEED]
                                            : message["speed"].as<int32_t>();
  if (speed < 0 || speed > 3400) {
    replyBadParam(id, "speed", speed);
    return;
  }
  // Если уже за границей, наружу не пускаем, а внутрь разрешаем.
  if (last.valid) {
    int32_t here = toUser(last.pos);
    bool outward = (here <= config[MIN_POS] && strcmp(dir, "cw") == 0) ||
                   (here >= config[MAX_POS] && strcmp(dir, "ccw") == 0);
    if (outward) {
      replyBadParam(id, "pos", here);
      return;
    }
  }
  setServoMode(1);
  mode = MODE_MOTOR;
  st.WriteSpe(SERVO_ID, strcmp(dir, "cw") == 0 ? -(s16)speed : (s16)speed,
              (u8)config[ACCEL]);
  JsonDocument data;
  data["dir"] = dir;
  data["speed"] = speed;
  reply(id, true, data, nullptr);
}

/* STOP работает всегда, в том числе посреди homing: это аварийная команда.
 *
 * Обнуления скорости НЕДОСТАТОЧНО: WriteSpe пишет только в 0x29 и 0x2E,
 * а регистр цели 0x2A остаётся прежним, и в позиционном режиме серва
 * продолжит ехать к старой цели. Поэтому цель перебивается текущей
 * позицией. Если серва не ответила и позиции нет, снимаем момент:
 * обмякнуть безопаснее, чем ехать неизвестно куда.
 */
static void stopMotion() {
  if (servoMode == 1) {
    st.WriteSpe(SERVO_ID, 0, 0);
  }
  int pos = st.ReadPos(SERVO_ID);
  if (pos != -1) {
    setServoMode(0);
    targetPos = pos;
    uint16_t speed = config[SPEED] > 0 ? (uint16_t)config[SPEED] : 1000;
    st.WritePosEx(SERVO_ID, (s16)pos, speed, 0);
  } else {
    st.EnableTorque(SERVO_ID, 0);
  }
  mode = MODE_IDLE;
}

static void cmdStop(long id) {
  if (homing != HOMING_OFF) {
    homingStop();
    emitHoming("stopped", nullptr, false);
  }
  stopMotion();
  replyOk(id);
}

// ------------------------------------------------------------------------ разбор

static void handleLine(const char *text) {
  JsonDocument message;
  if (deserializeJson(message, text)) {
    replyError(0, "BAD_JSON");      // id неизвестен, наши начинаются с 1
    return;
  }
  lastHostCmd = millis();      // любая разобранная команда это признак жизни ПК
  long id = message["id"] | 0L;
  const char *cmd = message["cmd"];
  if (cmd == nullptr) {
    replyError(id, "NO_CMD");
  } else if (strcmp(cmd, "ping") == 0) {
    cmdPing(id);
  } else if (strcmp(cmd, "get_config") == 0) {
    cmdGetConfig(id);
  } else if (strcmp(cmd, "set_config") == 0) {
    cmdSetConfig(id, message);
  } else if (strcmp(cmd, "move") == 0) {
    cmdMove(id, message);
  } else if (strcmp(cmd, "motor") == 0) {
    cmdMotor(id, message);
  } else if (strcmp(cmd, "stop") == 0) {
    cmdStop(id);
  } else if (strcmp(cmd, "home") == 0) {
    if (homing != HOMING_OFF) {
      replyError(id, "BUSY");
    } else {
      homingBegin(id);
    }
  } else {
    replyError(id, "UNKNOWN_CMD");
  }
}

/* Накапливает байты из USB до '\n'. Строку длиннее буфера выбрасывает целиком
 * вместе с хвостом: иначе её остаток разобрался бы как отдельное сообщение.
 */
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
      continue;
    }
    if (lineLen + 1 >= HOST_LINE_MAX) {
      lineOverflow = true;
      continue;
    }
    line[lineLen++] = c;
  }
}

// -------------------------------------------------------------------- setup и loop

void setup() {
  Serial.begin(115200);
  Serial1.begin(SERVO_BAUD, SERIAL_8N1, SERVO_RX, SERVO_TX);
  st.pSerial = &Serial1;
  // По умолчанию 100 мс. Столько loop() простоял бы в ожидании молчащей сервы,
  // а на 1 Мбод ответ приходит меньше чем за миллисекунду.
  st.IOTimeOut = 10;
  delay(1000);                       // серве нужно время на загрузку

  lastHostCmd = millis();
  configLoad();
  storage.begin("servo", true);
  zeroRaw = storage.getInt("zero_raw", config[HOME_ZERO]);
  storage.end();
  applyToServo(nullptr);             // при старте пишем всё: что стоит в серве, мы не знаем
  setServoMode(0);
}

void loop() {
  readFromHost();

  uint32_t period = mode == MODE_IDLE ? TLM_IDLE_MS : TLM_FAST_MS;
  if (millis() - lastTlm >= period) {
    // Не догоняем пропущенное: при lastTlm += period после заминки ушла бы
    // пачка кадров подряд и забила буфер передачи.
    lastTlm = millis();
    tlmFrame++;

    bool ok = readFeedback();
    if (homing != HOMING_OFF) {
      homingStep();
    } else if (mode == MODE_MOTOR) {
      int32_t here = toUser(last.pos);
      if (ok && (here < config[MIN_POS] || here > config[MAX_POS])) {
        stopMotion();
        emitLimit(here < config[MIN_POS] ? "min_pos" : "max_pos", here);
      } else if (!ok && servoFails >= SERVO_FAIL_LIMIT) {
        // Серва замолчала, а привод крутится: следить за диапазоном больше
        // нечем, поэтому останавливаем. В homing этот случай уже обработан.
        stopMotion();
        emitFault("servo_silent");
      }
    }
    // Связь с ПК потеряна во время движения: плата питается отдельно,
    // и сама себя не остановит.
    if ((mode != MODE_IDLE || homing != HOMING_OFF) &&
        millis() - lastHostCmd > HOST_TIMEOUT_MS) {
      if (homing != HOMING_OFF) {
        homingStop();
        emitHoming("error", "host_timeout", false);
      }
      stopMotion();
      emitFault("host_timeout");
    }
    if (!ok && servoFails >= SERVO_FAIL_LIMIT && !servoSilentReported) {
      servoSilentReported = true;
      emitFault("servo_silent");
    }
    // Кадр телеметрии расходный: места в буфере нет, значит пропускаем.
    // Ответы на команды так не пропускаются никогда.
    if (ok && Serial.availableForWrite() > (int)TLM_RESERVE) {
      sendTelemetry();
    }
  }
}
