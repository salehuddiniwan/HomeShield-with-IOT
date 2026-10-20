/*
  HomeShield node: one sketch for every ESP32 in the house.

  Flash it once with the Arduino IDE. Everything else is set from the
  HomeShield dashboard:

    1. On first start the board opens a Wi-Fi hotspot, HomeShield-Setup-XXXXXX.
       Join it with a phone; a page asks for your home Wi-Fi, the HomeShield
       computer's address and the device key (both on HomeShield's ESP32 nodes page).
    2. It joins your Wi-Fi and HomeShield's MQTT broker (port 1884) as
       esp32-XXXXXX, and appears under ESP32 nodes > Found on your network.
    3. Adopt it there: it gets its name (node-kitchen) and then a pin map,
       e.g. GPIO 26 = kitchen light relay, GPIO 34 = MQ-2 gas sensor.
    4. Firmware updates arrive over Wi-Fi (ESP32 nodes > Update over Wi-Fi).

  Topics (homeshield/<name>/...): status, info, config, config/ack,
  pin/<n>/set, pin/<n>/state, event, cmd, adopt; and homeshield/alarm, the
  alarm line every buzzer plays (ALARM FIRE 1729 / STOP 1730).

  BOOT button: press to silence an alarm; hold 5 s to forget the Wi-Fi and
  HomeShield settings and open the setup hotspot again.
  Blue LED: steady = connected to HomeShield, short flash every 2 s = not
  connected, fast blink = sounding, or "Blink LED" pressed in the dashboard.

  Needs: esp32 by Espressif Systems 3.x, and the libraries WiFiManager
  (tzapu), PubSubClient, ArduinoJson 7, DHT sensor library (Adafruit).
  Board: ESP32 Dev Module (ESP32 DevKit, CP2102), or for an ESP32-C3 SuperMini
  "Nologo ESP32C3 Super Mini" with USB CDC On Boot: Enabled.
*/

#include <WiFi.h>
#include <WiFiManager.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <DHT.h>
#include <driver/gpio.h>
#include <esp_mac.h>

#include "node_types.h"

#if ESP_ARDUINO_VERSION_MAJOR < 3
#error "HomeShield node needs the esp32 board package 3.x (Tools > Board > Boards Manager)."
#endif

// The board's own LED and BOOT button, and its name for HomeShield's pin rules.
#if CONFIG_IDF_TARGET_ESP32C3
static const char *BOARD = "esp32c3";            // ESP32-C3 SuperMini
static const uint8_t LED_PIN = 8;                // blue LED, lit when the pin is LOW
static const bool LED_ON_LOW = true;
static const uint8_t BOOT_BUTTON = 9;
#if !ARDUINO_USB_CDC_ON_BOOT
#warning "ESP32-C3: set Tools > USB CDC On Boot > Enabled, or the Serial Monitor stays silent."
#endif
#else
static const char *BOARD = "esp32";              // ESP32 DevKit (ESP-WROOM-32)
static const uint8_t LED_PIN = 2;
static const bool LED_ON_LOW = false;
static const uint8_t BOOT_BUTTON = 0;
#endif

static const char *FIRMWARE = "1.4";
static const uint16_t MQTT_PORT = 1884;
static const uint8_t MAX_CHANNELS = 16;
static const uint32_t LINK_TIMEOUT_MS = 15000;
static const uint32_t INFO_EVERY_MS = 60000;
static const uint32_t INPUT_REFRESH_MS = 60000;
static const uint32_t SOS_GRACE_MS = 10000;
static const uint32_t SOS_LOCAL_MS = 120000;
static const uint32_t SOS_REPEAT_MS = 3000;
static const uint32_t RESET_HOLD_MS = 5000;

// ---- settings kept across restarts ---------------------------------------------

static Preferences prefs;
static char serverHost[64] = "";
static char deviceKey[48] = "";
static char nodeId[40] = "";            // node-kitchen once adopted, else esp32-XXXXXX
static char chipId[16] = "";
static bool adopted = false;

// People paste the dashboard's address: "http://192.168.1.227:5000/" and
// " 192.168.1.227 " both mean the computer 192.168.1.227.
static void cleanHost(char *host, size_t size) {
  String s(host);
  s.trim();
  int i = s.indexOf("://");
  if (i >= 0) s = s.substring(i + 3);
  i = s.indexOf('/');
  if (i >= 0) s = s.substring(0, i);
  i = s.indexOf(':');
  if (i >= 0) s = s.substring(0, i);
  strncpy(host, s.c_str(), size - 1);
  host[size - 1] = '\0';
}

static void cleanKey(char *key, size_t size) {
  String s(key);
  s.trim();
  strncpy(key, s.c_str(), size - 1);
  key[size - 1] = '\0';
}

static void loadSettings() {
  prefs.begin("homeshield", true);
  prefs.getString("server", serverHost, sizeof(serverHost));
  prefs.getString("key", deviceKey, sizeof(deviceKey));
  String id = prefs.getString("id", "");
  prefs.end();
  cleanHost(serverHost, sizeof(serverHost));
  cleanKey(deviceKey, sizeof(deviceKey));
  adopted = id.length() > 0;
  strncpy(nodeId, adopted ? id.c_str() : chipId, sizeof(nodeId) - 1);
}

static void saveString(const char *key, const char *value) {
  prefs.begin("homeshield", false);
  prefs.putString(key, value);
  prefs.end();
}

static void forgetEverything(bool wifiToo) {
  prefs.begin("homeshield", false);
  prefs.clear();
  prefs.end();
  if (wifiToo) {
    WiFiManager wm;
    wm.resetSettings();
  }
}

// ---- sounds (the same patterns as the USB buzzer) ------------------------------------

static const Step FIRE_STEPS[] = {{500, 1}, {500, 0}, {500, 1}, {500, 0}, {500, 1}, {1500, 0}};
static const Step SOS_STEPS[] = {
  {150, 1}, {150, 0}, {150, 1}, {150, 0}, {150, 1}, {450, 0},
  {450, 1}, {150, 0}, {450, 1}, {150, 0}, {450, 1}, {450, 0},
  {150, 1}, {150, 0}, {150, 1}, {150, 0}, {150, 1}, {1350, 0}};
static const Step FALL_STEPS[] = {{700, 1}, {300, 0}, {700, 1}, {1300, 0}};
static const Step INTRUDER_STEPS[] = {{130, 1}, {60, 0}};
static const Step ZONE_STEPS[] = {{90, 1}, {90, 0}, {90, 1}, {90, 0}, {90, 1}, {1450, 0}};
static const Step TEST_STEPS[] = {{200, 1}, {200, 0}, {200, 1}, {200, 0}, {200, 1}, {100, 0}};
static const Step LOST_STEPS[] = {{120, 1}, {120, 0}, {120, 1}, {120, 0}, {120, 1}, {120, 0}};

#define PATTERN(name, steps, repeat) {name, steps, (uint8_t)(sizeof(steps) / sizeof(Step)), repeat}
static const Pattern ALARMS[] = {
  PATTERN("FIRE", FIRE_STEPS, true),
  PATTERN("SOS", SOS_STEPS, true),
  PATTERN("FALL", FALL_STEPS, true),
  PATTERN("INTRUDER", INTRUDER_STEPS, true),
  PATTERN("ZONE", ZONE_STEPS, true),
  PATTERN("TEST", TEST_STEPS, false),
};
static const Pattern *const SOS_ALARM = &ALARMS[1];
static const Pattern *const TEST_ALARM = &ALARMS[5];
static const Pattern LOST = PATTERN("LOST", LOST_STEPS, false);

// ---- the pin map -------------------------------------------------------------------

static Chan chans[MAX_CHANNELS];
static uint8_t chanCount = 0;
static Chan *buzzer = nullptr;
static String configVersion = "";

static WiFiClient wifiClient;
static PubSubClient mqtt(wifiClient);
static char topicBase[64];

static String topic(const char *rest) {
  return String(topicBase) + rest;
}

static void publish(const String &t, const String &payload, bool retain = false) {
  if (mqtt.connected()) mqtt.publish(t.c_str(), payload.c_str(), retain);
}

static void publishState(Chan &c) {
  String t = topic("pin/") + c.pin + "/state";
  publish(t, String(c.value), true);
  c.lastSentMs = millis();
}

static void fullDrive(uint8_t pin) {
  gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
}

// ---- buzzer and alarm --------------------------------------------------------------

static const Pattern *playing = nullptr;
static uint8_t stepIndex = 0;
static uint32_t stepStarted = 0;
static bool alarmOn = false;
static unsigned long currentSeq = 0, silencedSeq = 0, lastSeq = 0;
static uint32_t sosAtMs = 0, sosSilencedAtMs = 0, lastSosMs = 0;
static unsigned long sosFloorSeq = 0;

static void buzzerOutput(bool on) {
  if (!buzzer) return;
  if (buzzer->passive) {
    if (on) {
      ledcWriteTone(buzzer->pin, buzzer->hz);
    } else {
      ledcWrite(buzzer->pin, 0);
    }
  } else {
    digitalWrite(buzzer->pin, on != buzzer->inv ? HIGH : LOW);
  }
}

static void play(const Pattern *p) {
  playing = p;
  stepIndex = 0;
  stepStarted = millis();
  buzzerOutput(p->steps[0].on);
}

static void quiet() {
  playing = nullptr;
  buzzerOutput(false);
}

static void stopAlarm() {
  alarmOn = false;
  sosAtMs = 0;
  quiet();
}

static void updateSound(uint32_t now) {
  if (!playing || now - stepStarted < playing->steps[stepIndex].ms) return;
  stepStarted = now;
  if (++stepIndex >= playing->count) {
    if (!playing->repeat) {
      bool wasAlarm = playing != &LOST;
      quiet();
      if (wasAlarm) alarmOn = false;
      return;
    }
    stepIndex = 0;
  }
  buzzerOutput(playing->steps[stepIndex].on);
}

static const Pattern *findAlarm(const char *name) {
  if (!name) return nullptr;
  for (const Pattern &p : ALARMS) {
    if (strcmp(p.name, name) == 0) return &p;
  }
  return nullptr;
}

static void noteSeq(unsigned long seq) {
  if (seq > lastSeq) lastSeq = seq;
  if (seq > sosFloorSeq) sosAtMs = 0;
}

// "ALARM FIRE 1729" or "STOP 1730" on homeshield/alarm.
static void handleAlarmLine(char *line) {
  char *cmd = strtok(line, " ");
  if (!cmd) return;
  const char *seqText;
  if (strcmp(cmd, "STOP") == 0) {
    seqText = strtok(nullptr, " ");
    unsigned long seq = seqText ? strtoul(seqText, nullptr, 10) : 0;
    bool stale = sosAtMs && seq != 0 && seq <= sosFloorSeq && millis() - sosAtMs < SOS_GRACE_MS;
    noteSeq(seq);
    if (!stale && alarmOn) stopAlarm();
    return;
  }
  if (strcmp(cmd, "ALARM") != 0) return;
  const Pattern *p = findAlarm(strtok(nullptr, " "));
  if (!p) return;
  seqText = strtok(nullptr, " ");
  unsigned long seq = seqText ? strtoul(seqText, nullptr, 10) : 0;
  noteSeq(seq);
  if (seq != 0 && (seq == currentSeq || seq == silencedSeq)) return;
  if (p == SOS_ALARM && seq != 0 && sosSilencedAtMs && millis() - sosSilencedAtMs < SOS_GRACE_MS) {
    sosSilencedAtMs = 0;
    silencedSeq = seq;
    publish(topic("event"), "BTN");
    return;
  }
  if (alarmOn && playing == p) {
    currentSeq = seq;
    return;
  }
  currentSeq = seq;
  alarmOn = true;
  play(p);
}

static void onSilence(uint32_t now) {
  if (!alarmOn) return;
  if (sosAtMs) sosSilencedAtMs = now ? now : 1;
  silencedSeq = currentSeq;
  stopAlarm();
  publish(topic("event"), "BTN");
}

static void onSos(uint32_t now) {
  if (lastSosMs && now - lastSosMs < SOS_REPEAT_MS) return;
  lastSosMs = now ? now : 1;
  publish(topic("event"), "SOS");
  Serial.println("# SOS: help requested");
  sosAtMs = lastSosMs;
  sosFloorSeq = lastSeq;
  sosSilencedAtMs = 0;
  currentSeq = 0;
  alarmOn = true;
  play(SOS_ALARM);
}

// ---- applying the pin map ------------------------------------------------------------

static void releaseChannels() {
  for (uint8_t i = 0; i < chanCount; i++) {
    Chan &c = chans[i];
    if (c.type == T_PWM || (c.type == T_BUZZER && c.passive)) ledcDetach(c.pin);
    if (c.dht) {
      delete c.dht;
      c.dht = nullptr;
    }
    if (c.type == T_OUT || c.type == T_BUZZER) digitalWrite(c.pin, c.inv ? HIGH : LOW);
    pinMode(c.pin, INPUT);
  }
  chanCount = 0;
  buzzer = nullptr;
  playing = nullptr;
}

static void writeOutput(Chan &c, int value) {
  c.value = value;
  if (c.type == T_OUT) {
    digitalWrite(c.pin, (value != 0) != c.inv ? HIGH : LOW);
    c.offAt = (value && c.autoMs) ? millis() + c.autoMs : 0;
  } else if (c.type == T_PWM) {
    ledcWrite(c.pin, (uint32_t)value * 1023 / 100);
  }
}

static ChanType typeOf(const char *t) {
  if (!t) return T_NONE;
  if (!strcmp(t, "out")) return T_OUT;
  if (!strcmp(t, "pwm")) return T_PWM;
  if (!strcmp(t, "buzzer")) return T_BUZZER;
  if (!strcmp(t, "in")) return T_IN;
  if (!strcmp(t, "adc")) return T_ADC;
  if (!strcmp(t, "dht11")) return T_DHT11;
  if (!strcmp(t, "dht22")) return T_DHT22;
  return T_NONE;
}

static bool applyConfig(const char *json, bool announce) {
  JsonDocument doc;
  if (deserializeJson(doc, json)) return false;
  releaseChannels();
  configVersion = doc["v"].as<String>();
  for (JsonObject p : doc["pins"].as<JsonArray>()) {
    if (chanCount >= MAX_CHANNELS) break;
    Chan &c = chans[chanCount];
    c = Chan();
    c.pin = p["pin"] | 0;
    c.type = typeOf(p["t"]);
    if (c.type == T_NONE || c.pin == BOOT_BUTTON || c.pin == LED_PIN || !GPIO_IS_VALID_GPIO(c.pin)) continue;
    c.inv = (p["inv"] | 0) != 0;
    c.pull = (p["pull"] | 0) != 0;
    c.passive = (p["passive"] | 0) != 0;
    c.sos = strcmp(p["role"] | "", "sos") == 0;
    c.hz = p["hz"] | 2700;
    c.holdMs = p["hold"] | 300;
    c.autoMs = (uint32_t)(p["auto"] | 0) * 1000UL;
    c.everyMs = (uint32_t)(p["every"] | 10) * 1000UL;
    c.lastRaw = c.stable = -1;
    switch (c.type) {
      case T_OUT:
        digitalWrite(c.pin, c.inv ? HIGH : LOW);   // off before it becomes an output
        pinMode(c.pin, OUTPUT);
        fullDrive(c.pin);
        c.value = 0;
        break;
      case T_PWM:
        ledcAttach(c.pin, 5000, 10);
        ledcOutputInvert(c.pin, c.inv);
        ledcWrite(c.pin, 0);
        c.value = 0;
        break;
      case T_BUZZER:
        if (c.passive) {
          ledcAttach(c.pin, c.hz, 10);
          ledcOutputInvert(c.pin, c.inv);
          ledcWrite(c.pin, 0);
        } else {
          digitalWrite(c.pin, c.inv ? HIGH : LOW);
          pinMode(c.pin, OUTPUT);
        }
        fullDrive(c.pin);
        buzzer = &c;
        break;
      case T_IN:
        pinMode(c.pin, c.pull ? INPUT_PULLUP : INPUT);
        break;
      case T_ADC:
        pinMode(c.pin, INPUT);
        break;
      case T_DHT11:
      case T_DHT22:
        c.dht = new DHT(c.pin, c.type == T_DHT11 ? DHT11 : DHT22);
        c.dht->begin();
        break;
      default:
        break;
    }
    chanCount++;
  }
  if (announce) {
    publish(topic("config/ack"), configVersion);
    for (uint8_t i = 0; i < chanCount; i++) {
      if (chans[i].type == T_OUT || chans[i].type == T_PWM) publishState(chans[i]);
      chans[i].lastReadMs = 0;                     // read the sensors at once
    }
  }
  Serial.printf("# pin map %s: %u pins\n", configVersion.c_str(), chanCount);
  return true;
}

static Chan *chanAt(uint8_t pin) {
  for (uint8_t i = 0; i < chanCount; i++) {
    if (chans[i].pin == pin) return &chans[i];
  }
  return nullptr;
}

// ---- inputs and sensors ------------------------------------------------------------------

static void updateInput(Chan &c, uint32_t now) {
  int raw = digitalRead(c.pin);
  if (raw != c.lastRaw) {
    c.lastRaw = raw;
    c.changedAt = now;
  }
  if (now - c.changedAt > 30 && raw != c.stable) {
    c.stable = raw;
    int active = (raw == LOW) == c.inv ? 1 : 0;
    if (c.sos) {
      if (active) {
        c.downAt = now;
        c.fired = false;
      }
    } else {
      c.value = active;
      publishState(c);
    }
  }
  if (c.sos && c.stable >= 0 && ((c.stable == LOW) == c.inv) && !c.fired &&
      now - c.downAt >= c.holdMs) {
    c.fired = true;
    onSos(now);
  }
  if (!c.sos && now - c.lastSentMs > INPUT_REFRESH_MS && c.stable >= 0) publishState(c);
}

static void updateSensors(uint32_t now) {
  for (uint8_t i = 0; i < chanCount; i++) {
    Chan &c = chans[i];
    switch (c.type) {
      case T_IN:
        updateInput(c, now);
        break;
      case T_ADC:
        if (c.lastReadMs == 0 || now - c.lastReadMs >= c.everyMs) {
          c.lastReadMs = now;
          // Millivolts at the pin, corrected with the chip's factory calibration;
          // HomeShield turns them into ppm (iot/sensors.py).
          uint32_t sum = 0;
          for (int k = 0; k < 8; k++) sum += analogReadMilliVolts(c.pin);
          c.value = sum / 8;
          char buf[24];
          snprintf(buf, sizeof(buf), "{\"mv\":%d}", c.value);
          publish(topic("pin/") + c.pin + "/state", buf, true);
          c.lastSentMs = now;
        }
        break;
      case T_DHT11:
      case T_DHT22:
        if (c.lastReadMs == 0 || now - c.lastReadMs >= c.everyMs) {
          c.lastReadMs = now;
          float t = c.dht->readTemperature(), h = c.dht->readHumidity();
          if (!isnan(t) && !isnan(h)) {
            char buf[48];
            snprintf(buf, sizeof(buf), "{\"t\":%.1f,\"h\":%.0f}", t, h);
            publish(topic("pin/") + c.pin + "/state", buf, true);
          }
        }
        break;
      case T_OUT:
        if (c.offAt && (int32_t)(now - c.offAt) >= 0) {
          writeOutput(c, 0);
          publishState(c);
        }
        break;
      default:
        break;
    }
  }
}

// ---- MQTT --------------------------------------------------------------------------------

static uint32_t identifyUntil = 0;
static bool updateRequested = false;
static String updateUrl;

static void publishInfo() {
  char buf[200];
  snprintf(buf, sizeof(buf),
           "{\"fw\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"up\":%lu,\"mac\":\"%s\",\"heap\":%u,\"chip\":\"%s\","
           "\"board\":\"%s\"}",
           FIRMWARE, WiFi.localIP().toString().c_str(), WiFi.RSSI(), (unsigned long)(millis() / 1000),
           WiFi.macAddress().c_str(), (unsigned)ESP.getFreeHeap(), chipId, BOARD);
  publish(topic("info"), buf, true);
}

static void onMessage(char *t, byte *payload, unsigned int length) {
  static char msg[2048];
  length = min(length, (unsigned int)sizeof(msg) - 1);
  memcpy(msg, payload, length);
  msg[length] = '\0';
  String tp(t);

  if (tp == "homeshield/alarm") {
    handleAlarmLine(msg);
  } else if (tp == topic("config")) {
    if (length && applyConfig(msg, true)) {
      prefs.begin("homeshield", false);
      prefs.putString("config", msg);
      prefs.end();
    }
  } else if (tp == topic("cmd")) {
    if (!strcmp(msg, "blink")) {
      identifyUntil = millis() + 10000;
    } else if (!strcmp(msg, "restart")) {
      ESP.restart();
    } else if (!strcmp(msg, "forget")) {
      forgetEverything(false);
      saveString("server", serverHost);            // keep the way back to HomeShield
      saveString("key", deviceKey);
      ESP.restart();
    } else if (!strncmp(msg, "update ", 7)) {
      updateUrl = String(msg + 7);
      updateRequested = true;                      // done from loop(), not inside a callback
    }
  } else if (tp == topic("adopt") && !adopted) {
    JsonDocument doc;
    if (!deserializeJson(doc, msg) && doc["id"].is<const char *>()) {
      publish(topic("status"), "", true);          // clear what this board left as esp32-XXXXXX
      publish(topic("info"), "", true);
      saveString("id", doc["id"].as<const char *>());
      delay(200);
      ESP.restart();
    }
  } else if (tp.startsWith(topic("pin/")) && tp.endsWith("/set")) {
    uint8_t pin = tp.substring(strlen(topicBase) + 4).toInt();
    Chan *c = chanAt(pin);
    if (!c) return;
    if (c->type == T_OUT) {
      writeOutput(*c, atoi(msg) ? 1 : 0);
      publishState(*c);
    } else if (c->type == T_PWM) {
      writeOutput(*c, constrain(atoi(msg), 0, 100));
      publishState(*c);
    } else if (c->type == T_BUZZER) {
      if (!strcmp(msg, "test") && !alarmOn) {
        alarmOn = true;
        currentSeq = 0;
        play(TEST_ALARM);
      } else if (!strcmp(msg, "stop")) {
        stopAlarm();
      }
    }
  }
}

static uint32_t lastMqttTry = 0, lastInfoMs = 0, lastLinkMs = 0;
static bool linked = false;

static void connectMqtt(uint32_t now) {
  if (mqtt.connected() || now - lastMqttTry < 5000 || WiFi.status() != WL_CONNECTED) return;
  lastMqttTry = now;
  mqtt.setServer(serverHost, MQTT_PORT);
  String will = topic("status");
  if (!mqtt.connect(nodeId, "node", deviceKey, will.c_str(), 1, true, "offline")) {
    int state = mqtt.state();
    const char *why = state == 4 || state == 5
        ? "wrong device key (HomeShield's ESP32 nodes page has the right one)"
        : "no answer: is this the address on HomeShield's ESP32 nodes page, is HomeShield running, "
          "and is mosquitto.exe allowed through Windows Firewall?";
    Serial.printf("# HomeShield broker at %s:%u: %s (state %d)\n", serverHost, MQTT_PORT, why, state);
    return;
  }
  Serial.printf("# connected to HomeShield as %s\n", nodeId);
  publish(topic("status"), "online", true);
  publishInfo();
  lastInfoMs = now;
  mqtt.subscribe("homeshield/alarm", 1);
  mqtt.subscribe(topic("config").c_str(), 1);
  mqtt.subscribe(topic("cmd").c_str(), 1);
  mqtt.subscribe(topic("pin/+/set").c_str(), 1);
  if (!adopted) mqtt.subscribe(topic("adopt").c_str(), 1);
}

static void runUpdate() {
  updateRequested = false;
  Serial.printf("# updating from %s\n", updateUrl.c_str());
  publish(topic("event"), "UPDATING");
  mqtt.loop();
  WiFiClient client;
  httpUpdate.setAuthorization("node", deviceKey);
  httpUpdate.rebootOnUpdate(true);
  t_httpUpdate_return r = httpUpdate.update(client, updateUrl);
  if (r != HTTP_UPDATE_OK) {
    String why = "UPDATE_FAILED " + httpUpdate.getLastErrorString();
    publish(topic("event"), why);
    Serial.println("# " + why);
  }
}

// ---- setup hotspot -------------------------------------------------------------------------

// The board's LED: on the SuperMini it is lit when its pin is LOW.
static void ledWrite(bool on) {
  digitalWrite(LED_PIN, on != LED_ON_LOW ? HIGH : LOW);
}

// Many ESP32-C3 SuperMini boards can't join Wi-Fi at full power: their small
// antenna is badly tuned. 8.5 dBm still reaches across a house.
static void lowerTxPower() {
#if CONFIG_IDF_TARGET_ESP32C3
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
#endif
}

static void runSetup(bool force) {
  WiFiManager wm;
  WiFiManagerParameter pServer("server", "HomeShield computer address (shown on its ESP32 nodes page)",
                               serverHost, sizeof(serverHost) - 1);
  WiFiManagerParameter pKey("key", "Device key (on HomeShield's ESP32 nodes page)",
                            deviceKey, sizeof(deviceKey) - 1);
  wm.addParameter(&pServer);
  wm.addParameter(&pKey);
  wm.setTitle("HomeShield node setup");
  wm.setConfigPortalTimeout(300);                  // then try the saved Wi-Fi again
  String ap = String("HomeShield-Setup-") + (chipId + 6);
  ledWrite(true);
  lowerTxPower();
  bool ok = force || !serverHost[0] || !deviceKey[0] ? wm.startConfigPortal(ap.c_str())
                                                     : wm.autoConnect(ap.c_str());
  if (strlen(pServer.getValue())) {
    strncpy(serverHost, pServer.getValue(), sizeof(serverHost) - 1);
    cleanHost(serverHost, sizeof(serverHost));
    saveString("server", serverHost);
  }
  if (strlen(pKey.getValue())) {
    strncpy(deviceKey, pKey.getValue(), sizeof(deviceKey) - 1);
    cleanKey(deviceKey, sizeof(deviceKey));
    saveString("key", deviceKey);
  }
  if (!ok) {
    Serial.println("# no Wi-Fi; restarting");
    ESP.restart();
  }
}

// ---- BOOT button and LED ---------------------------------------------------------------------

static void updateBootButton(uint32_t now) {
  static int stable = HIGH, last = HIGH;
  static uint32_t changedAt = 0, downAt = 0;
  int raw = digitalRead(BOOT_BUTTON);
  if (raw != last) {
    last = raw;
    changedAt = now;
  }
  if (now - changedAt > 30 && raw != stable) {
    stable = raw;
    if (stable == LOW) {
      downAt = now;
      onSilence(now);
    }
  }
  if (stable == LOW && now - downAt >= RESET_HOLD_MS) {
    Serial.println("# BOOT held 5 s: forgetting Wi-Fi and HomeShield settings");
    forgetEverything(true);
    ESP.restart();
  }
}

static void updateLink(uint32_t now) {
  if (mqtt.connected()) lastLinkMs = now;
  bool nowLinked = mqtt.connected() || (lastLinkMs && now - lastLinkMs < LINK_TIMEOUT_MS);
  if (linked && !nowLinked) {
    currentSeq = 0;
    if (alarmOn && playing == SOS_ALARM) {
      sosAtMs = now ? now : 1;                       // someone asked for help: keep going
      sosFloorSeq = lastSeq;
    } else if (buzzer) {
      alarmOn = false;
      play(&LOST);
    }
  }
  linked = nowLinked;
  if (sosAtMs && !linked && now - sosAtMs > SOS_LOCAL_MS) stopAlarm();
}

static void updateLed(uint32_t now) {
  bool on;
  if (identifyUntil && (int32_t)(identifyUntil - now) > 0) {
    on = (now / 100) % 2;
  } else if (alarmOn) {
    on = (now / 125) % 2;
  } else if (mqtt.connected()) {
    on = true;
  } else {
    on = (now % 2000) < 80;
  }
  ledWrite(on);
}

// ---- USB commands (Serial Monitor, 115200 baud, newline) ----------------------------------------
// SERVER 192.168.1.227   the HomeShield computer's address
// KEY <device key>       the device key from HomeShield's ESP32 nodes page
// SETUP                  open the setup hotspot now
// INFO                   what the board is using
// Lines it doesn't know are ignored.

static void reconnectNow() {
  mqtt.disconnect();
  lastMqttTry = millis() - 5000;
}

static void updateSerial() {
  static String line;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch != '\n') {
      if (line.length() < 120) line += ch;
      continue;
    }
    line.trim();
    if (line.startsWith("SERVER ")) {
      strncpy(serverHost, line.c_str() + 7, sizeof(serverHost) - 1);
      cleanHost(serverHost, sizeof(serverHost));
      saveString("server", serverHost);
      Serial.printf("# server set to %s\n", serverHost);
      reconnectNow();
    } else if (line.startsWith("KEY ")) {
      strncpy(deviceKey, line.c_str() + 4, sizeof(deviceKey) - 1);
      cleanKey(deviceKey, sizeof(deviceKey));
      saveString("key", deviceKey);
      Serial.println("# device key saved");
      reconnectNow();
    } else if (line == "SETUP") {
      runSetup(true);
      reconnectNow();
    } else if (line == "INFO") {
      Serial.printf("# HOMESHIELD-NODE %s board=%s id=%s server=%s key=%s wifi=%s ip=%s mqtt=%s\n", FIRMWARE,
                    BOARD, nodeId, serverHost[0] ? serverHost : "(not set)", deviceKey[0] ? "set" : "(not set)",
                    WiFi.status() == WL_CONNECTED ? WiFi.SSID().c_str() : "(not connected)",
                    WiFi.localIP().toString().c_str(), mqtt.connected() ? "connected" : "not connected");
    }
    line = "";
  }
}

// ---- Arduino ----------------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  ledWrite(false);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);
  WiFi.mode(WIFI_STA);
  lowerTxPower();
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);            // WiFi.macAddress() reads zeros until Wi-Fi starts
  snprintf(chipId, sizeof(chipId), "esp32-%02X%02X%02X", mac[3], mac[4], mac[5]);
  loadSettings();
  snprintf(topicBase, sizeof(topicBase), "homeshield/%s/", nodeId);
  Serial.printf("\nHOMESHIELD-NODE %s board=%s id=%s server=%s\n", FIRMWARE, BOARD, nodeId,
                serverHost[0] ? serverHost : "(not set)");

  // The last pin map, so buzzer and SOS button work before HomeShield answers.
  prefs.begin("homeshield", true);
  String saved = prefs.getString("config", "");
  prefs.end();
  if (adopted && saved.length()) applyConfig(saved.c_str(), false);

  runSetup(false);
  mqtt.setBufferSize(2048);
  mqtt.setKeepAlive(20);
  mqtt.setSocketTimeout(5);
  mqtt.setCallback(onMessage);
}

void loop() {
  uint32_t now = millis();
  connectMqtt(now);
  mqtt.loop();
  if (updateRequested) runUpdate();
  if (mqtt.connected() && now - lastInfoMs >= INFO_EVERY_MS) {
    lastInfoMs = now;
    publishInfo();
  }
  updateSerial();
  updateBootButton(now);
  updateSensors(now);
  updateLink(now);
  updateSound(now);
  updateLed(now);
  delay(2);
}
