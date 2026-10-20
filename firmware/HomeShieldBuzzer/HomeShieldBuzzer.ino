/*
  HomeShield buzzer: an ESP32 that sounds the alarm for HomeShield, with an
  SOS button for someone who needs help.

  HomeShield, on the PC, decides when to sound and which alarm. This sketch
  plays it, so the timing of each beep never depends on how busy the PC is.

  Links, either or both:
    USB    the board's CP2102 (or CH340) serial port at 115200 baud
    Wi-Fi  polls SERVER_URL/api/device/poll once a second (see config.h)

  Messages, one per line:
    from HomeShield   ALARM <FIRE|SOS|FALL|INTRUDER|ZONE|TEST> <seq>
                      STOP <seq>
                      HELLO        (answered with the line below)
    to HomeShield     HOMESHIELD-BUZZER <version> wifi=<on|off> id=<DEVICE_ID>
                      BTN          (the BOOT button silenced an alarm)
                      SOS          (the SOS button was pressed)
    Lines starting with "#" are notes for a person reading the Serial Monitor.

  <seq> changes whenever HomeShield wants a different sound. The board plays
  each seq once, so HomeShield repeating the same line (it does, every two
  seconds over USB, as a heartbeat) never restarts or revives a sound.

  BOOT button: silences an alarm at once. Pressed while quiet, it gives one
  short beep, to check the buzzer works.

  SOS button (SOS_PIN, held SOS_HOLD_MS): starts the SOS sound right here at
  once, then tells HomeShield, which opens a "help requested" incident, sends
  the WhatsApp alerts and sounds every buzzer in the house. With HomeShield
  unreachable it still sounds here, for up to two minutes or until BOOT.

  The LED: on = HomeShield connected and quiet, fast blink = sounding, short
  flash every 2 s = no word from HomeShield. After 15 s without word an alarm
  stops with three low beeps, so a PC that crashed or was switched off never
  leaves the buzzer sounding forever. An SOS keeps going: someone needs help.

  Board: ESP32 Dev Module (esp32 by Espressif Systems, core 2.x or 3.x).
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <driver/gpio.h>

#include "config.h"

static const char *FIRMWARE = "1.3";
static const uint32_t LINK_TIMEOUT_MS = 15000;
static const uint32_t POLL_MS = 1000;
static const uint32_t SOS_GRACE_MS = 10000;    // HomeShield gets this long to answer an SOS
static const uint32_t SOS_LOCAL_MS = 120000;   // unanswered, it sounds here this long
static const uint32_t SOS_REPEAT_MS = 3000;    // presses closer than this count once

// ---- Sounds ------------------------------------------------------------------
// Each step is a length and a tone in Hz (0 = silent). An active buzzer just
// switches on for any tone. A passive buzzer is loudest at one frequency,
// BUZZER_HZ in config.h, so every alarm uses it and they differ by rhythm.

#define BEEP BUZZER_HZ
#define LOW_BEEP (BUZZER_HZ / 2)

struct Step {
  uint16_t ms;
  uint16_t hz;
};

struct Pattern {
  const char *name;
  const Step *steps;
  uint8_t count;
  bool repeat;
};

// Fire: the "temporal-three" rhythm of smoke alarms (ISO 8201).
static const Step FIRE_STEPS[] = {{500, BEEP}, {500, 0}, {500, BEEP}, {500, 0}, {500, BEEP}, {1500, 0}};
// SOS: the Morse code for SOS, ... --- ..., then a pause.
static const Step SOS_STEPS[] = {
  {150, BEEP}, {150, 0}, {150, BEEP}, {150, 0}, {150, BEEP}, {450, 0},
  {450, BEEP}, {150, 0}, {450, BEEP}, {150, 0}, {450, BEEP}, {450, 0},
  {150, BEEP}, {150, 0}, {150, BEEP}, {150, 0}, {150, BEEP}, {1350, 0}};
// Fall: two long beeps, then a pause.
static const Step FALL_STEPS[] = {{700, BEEP}, {300, 0}, {700, BEEP}, {1300, 0}};
// Intruder: fast beeps without a pause.
static const Step INTRUDER_STEPS[] = {{130, BEEP}, {60, 0}};
// Danger zone: three quick chirps, then a pause.
static const Step ZONE_STEPS[] = {{90, BEEP}, {90, 0}, {90, BEEP}, {90, 0}, {90, BEEP}, {1450, 0}};
// Test: three beeps, played once.
static const Step TEST_STEPS[] = {{200, BEEP}, {200, 0}, {200, BEEP}, {200, 0}, {200, BEEP}, {100, 0}};
// One short beep: at start-up, and for the BOOT button pressed while quiet.
static const Step CHIRP_STEPS[] = {{70, BEEP}, {30, 0}};
// HomeShield went quiet: three short beeps, lower on a passive buzzer, once.
static const Step LOST_STEPS[] = {{120, LOW_BEEP}, {120, 0}, {120, LOW_BEEP}, {120, 0}, {120, LOW_BEEP}, {120, 0}};

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
static const Pattern CHIRP = PATTERN("CHIRP", CHIRP_STEPS, false);
static const Pattern LOST = PATTERN("LOST", LOST_STEPS, false);

static const Pattern *playing = nullptr;     // nullptr: quiet
static uint8_t stepIndex = 0;
static uint32_t stepStarted = 0;
static bool alarmOn = false;                 // playing one of ALARMS
static unsigned long currentSeq = 0;         // the seq last started
static unsigned long silencedSeq = 0;        // the seq the BOOT button silenced
static unsigned long lastSeq = 0;            // the highest seq heard from HomeShield

// An SOS started here, before HomeShield has answered it.
static uint32_t sosAtMs = 0;                 // 0: none
static unsigned long sosFloorSeq = 0;        // lastSeq when it started
static uint32_t sosSilencedAtMs = 0;         // BOOT pressed before HomeShield answered
static uint32_t lastSosMs = 0;

// ---- Links -------------------------------------------------------------------

static bool serialSeen = false;
static uint32_t lastSerialMs = 0;
static volatile bool wifiSeen = false;
static volatile uint32_t lastWifiMs = 0;
static volatile bool wifiButtonPending = false;
static volatile bool wifiSosPending = false;
static volatile bool wifiKeyRejected = false;
static bool linked = false;

struct Message {
  char text[48];
};
static QueueHandle_t wifiInbox = nullptr;    // lines from the Wi-Fi task to loop()

static bool wifiConfigured() {
  return WIFI_SSID[0] != '\0';
}

// ---- Buzzer and sound engine ---------------------------------------------------

// The pin's strongest drive (about 40 mA instead of the default 20), for a
// buzzer wired straight to it.
static void fullDrive() {
  gpio_set_drive_capability((gpio_num_t)BUZZER_PIN, GPIO_DRIVE_CAP_3);
}

#if BUZZER_PASSIVE
// A passive buzzer gets a 50% square wave from the LEDC PWM unit, attached
// once. Between beeps the pin rests at the "off" level: on a low level
// trigger module that is HIGH, so its transistor stays off. (tone() and
// noTone() leave the pin LOW, which on such a module passes current through
// the coil the whole time it is quiet.)
#if ESP_ARDUINO_VERSION_MAJOR >= 3
static void passiveTone(uint16_t hz) {
  if (hz) {
    ledcWriteTone(BUZZER_PIN, hz);
  } else {
    ledcWrite(BUZZER_PIN, 0);                  // the resting level, inverted if need be
  }
}

static void passiveBegin() {
  ledcAttach(BUZZER_PIN, BUZZER_HZ, 10);
  ledcOutputInvert(BUZZER_PIN, BUZZER_ON_LEVEL == LOW);
  passiveTone(0);
  fullDrive();
}
#else
static const uint8_t BUZZER_CHANNEL = 0;

static void passiveTone(uint16_t hz) {
  if (hz) {
    ledcWriteTone(BUZZER_CHANNEL, hz);
  } else {
    ledcWrite(BUZZER_CHANNEL, BUZZER_ON_LEVEL == LOW ? 1023 : 0);   // full duty: steady HIGH
  }
}

static void passiveBegin() {
  ledcSetup(BUZZER_CHANNEL, BUZZER_HZ, 10);
  ledcAttachPin(BUZZER_PIN, BUZZER_CHANNEL);
  passiveTone(0);
  fullDrive();
}
#endif
#endif

static void buzzerOutput(uint16_t hz) {
#if BUZZER_PASSIVE
  static uint16_t current = 0;
  if (hz == current) return;
  passiveTone(hz);
  current = hz;
#else
  digitalWrite(BUZZER_PIN, hz ? BUZZER_ON_LEVEL : !BUZZER_ON_LEVEL);
#endif
}

static void play(const Pattern *p) {
  playing = p;
  stepIndex = 0;
  stepStarted = millis();
  buzzerOutput(p->steps[0].hz);
}

static void quiet() {
  playing = nullptr;
  buzzerOutput(0);
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
      bool wasAlarm = playing != &CHIRP && playing != &LOST;
      quiet();
      if (wasAlarm) alarmOn = false;       // the test finished
      return;
    }
    stepIndex = 0;
  }
  buzzerOutput(playing->steps[stepIndex].hz);
}

static const Pattern *findAlarm(const char *name) {
  if (!name) return nullptr;
  for (const Pattern &p : ALARMS) {
    if (strcmp(p.name, name) == 0) return &p;
  }
  return nullptr;
}

// ---- Messages ------------------------------------------------------------------

static void sayHello() {
  Serial.print("HOMESHIELD-BUZZER ");
  Serial.print(FIRMWARE);
  Serial.print(wifiConfigured() ? " wifi=on" : " wifi=off");
  Serial.print(" id=");
  Serial.println(DEVICE_ID);
}

static void sendButton() {
  Serial.println("BTN");
  if (wifiConfigured()) wifiButtonPending = true;
}

static void heardFromSerial(bool fromSerial) {
  if (fromSerial) {
    serialSeen = true;
    lastSerialMs = millis();
  }
}

static unsigned long readSeq() {
  const char *text = strtok(nullptr, " \t");
  return text ? strtoul(text, nullptr, 10) : 0;
}

// A newer seq means HomeShield has caught up, including with an SOS sent from here.
static void noteSeq(unsigned long seq) {
  if (seq > lastSeq) lastSeq = seq;
  if (seq > sosFloorSeq) sosAtMs = 0;
}

// SWEEP, typed in the Serial Monitor: play 1500 to 4500 Hz and print each,
// to find the passive buzzer's loudest tone for BUZZER_HZ. Takes 8 s.
static void sweep() {
#if BUZZER_PASSIVE
  alarmOn = false;
  quiet();
  Serial.println("# sweep: note the loudest frequency, then set BUZZER_HZ in config.h");
  for (uint16_t hz = 1500; hz <= 4500; hz += 250) {
    Serial.print("# ");
    Serial.print(hz);
    Serial.println(" Hz");
    passiveTone(hz);
    delay(350);
    passiveTone(0);
    delay(250);
  }
  Serial.println("# sweep done");
#else
  Serial.println("# SWEEP is for passive buzzers: an active one has one built-in tone");
#endif
}

// One line from HomeShield, over USB or Wi-Fi. Changes the text in place.
static void handleLine(char *line, bool fromSerial) {
  char *cmd = strtok(line, " \t");
  if (!cmd) return;

  if (fromSerial && strcmp(cmd, "SWEEP") == 0) {
    sweep();
    return;
  }

  if (strcmp(cmd, "HELLO") == 0) {
    heardFromSerial(fromSerial);
    sayHello();
    return;
  }
  if (strcmp(cmd, "STOP") == 0) {
    heardFromSerial(fromSerial);
    unsigned long seq = readSeq();
    // Sent before HomeShield heard the SOS pressed here: keep sounding.
    bool stale = sosAtMs && seq != 0 && seq <= sosFloorSeq && millis() - sosAtMs < SOS_GRACE_MS;
    noteSeq(seq);
    if (!stale && alarmOn) stopAlarm();
    return;
  }
  if (strcmp(cmd, "ALARM") == 0) {
    const Pattern *p = findAlarm(strtok(nullptr, " \t"));
    if (!p) return;
    heardFromSerial(fromSerial);
    unsigned long seq = readSeq();
    noteSeq(seq);
    // seq 0 is someone typing ALARM FIRE in the Serial Monitor: always play.
    if (seq != 0 && (seq == currentSeq || seq == silencedSeq)) return;
    // BOOT silenced an SOS from here before HomeShield answered it: the
    // answer arrives now, so silence that too, there as well as here.
    if (p == SOS_ALARM && seq != 0 && sosSilencedAtMs && millis() - sosSilencedAtMs < SOS_GRACE_MS) {
      sosSilencedAtMs = 0;
      silencedSeq = seq;
      sendButton();
      return;
    }
    if (alarmOn && playing == p) {             // already playing it: keep the rhythm
      currentSeq = seq;
      return;
    }
    currentSeq = seq;
    alarmOn = true;
    play(p);
  }
}

static char serialLine[64];
static uint8_t serialLen = 0;

static void readSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialLen) {
        serialLine[serialLen] = '\0';
        handleLine(serialLine, true);
        serialLen = 0;
      }
    } else if (serialLen < sizeof(serialLine) - 1) {
      serialLine[serialLen++] = c;
    } else {
      serialLen = 0;                           // too long to be ours
    }
  }
}

static void readWifiInbox() {
  if (!wifiInbox) return;
  Message m;
  while (xQueueReceive(wifiInbox, &m, 0) == pdTRUE) {
    handleLine(m.text, false);
  }
}

// ---- Wi-Fi -----------------------------------------------------------------------
// Runs on its own task so a slow request never stalls the beeps in loop().
// An SOS goes first, then a button press, then the regular poll.

static void wifiTask(void *) {
  const String base = String(SERVER_URL);
  const String query = String("?id=") + DEVICE_ID + "&fw=" + FIRMWARE;
  const String pollUrl = base + "/api/device/poll" + query;
  const String buttonUrl = base + "/api/device/button" + query;
  const String sosUrl = base + "/api/device/sos" + query;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    const bool sos = wifiSosPending;
    const bool button = !sos && wifiButtonPending;
    HTTPClient http;
    http.setTimeout(3000);
    if (http.begin(sos ? sosUrl : button ? buttonUrl : pollUrl)) {
      http.addHeader("X-Device-Key", DEVICE_KEY);
      int code = (sos || button) ? http.POST("") : http.GET();
      if (code == 200) {
        String body = http.getString();
        body.trim();
        Message m;
        strncpy(m.text, body.c_str(), sizeof(m.text) - 1);
        m.text[sizeof(m.text) - 1] = '\0';
        xQueueSend(wifiInbox, &m, 0);
        lastWifiMs = millis();
        wifiSeen = true;
        wifiKeyRejected = false;
        if (sos) wifiSosPending = false;
        if (button) wifiButtonPending = false;
      } else if (code == 401) {
        wifiKeyRejected = true;
      }
      http.end();
    }
    if (!sos && !button) vTaskDelay(pdMS_TO_TICKS(POLL_MS));
  }
}

static void reportWifi() {
  static bool wasConnected = false;
  static bool keyWarned = false;
  if (!wifiConfigured()) return;
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected != wasConnected) {
    wasConnected = connected;
    if (connected) {
      Serial.print("# wifi connected, ip ");
      Serial.println(WiFi.localIP());
    } else {
      Serial.println("# wifi not connected");
    }
  }
  if (wifiKeyRejected && !keyWarned) {
    keyWarned = true;
    Serial.println("# HomeShield refused DEVICE_KEY. Copy it from the ESP32 nodes page into config.h");
  }
}

// ---- Buttons ---------------------------------------------------------------------

// A member function, not a free one taking Button&: the Arduino builder puts
// declarations of free functions at the top of the sketch, above this type.
struct Button {
  uint8_t pin;
  int lastReading;
  int stable;
  uint32_t changedAt;
  uint32_t downAt;
  bool fired;

  // True once per press, after the button has been held for holdMs. Both
  // buttons pull their pin to ground; the readings are debounced for 30 ms.
  bool pressed(uint32_t now, uint32_t holdMs) {
    int reading = digitalRead(pin);
    if (reading != lastReading) {
      lastReading = reading;
      changedAt = now;
    }
    if (now - changedAt > 30 && reading != stable) {
      stable = reading;
      if (stable == LOW) {
        downAt = now;
        fired = false;
      }
    }
    if (stable == LOW && !fired && now - downAt >= holdMs) {
      fired = true;
      return true;
    }
    return false;
  }
};
static Button bootButton = {BUTTON_PIN, HIGH, HIGH, 0, 0, false};
static Button sosButton = {SOS_PIN, HIGH, HIGH, 0, 0, false};

static void onBootButton(uint32_t now) {
  if (alarmOn) {
    if (sosAtMs) sosSilencedAtMs = now ? now : 1;   // HomeShield may not have answered yet
    silencedSeq = currentSeq;
    stopAlarm();
    sendButton();
  } else if (!playing) {
    play(&CHIRP);
  }
}

static void onSosButton(uint32_t now) {
  if (lastSosMs && now - lastSosMs < SOS_REPEAT_MS) return;
  lastSosMs = now ? now : 1;
  Serial.println("SOS");
  if (wifiConfigured()) wifiSosPending = true;
  // Sound at once, without waiting for HomeShield, and keep sounding through
  // the STOP lines it sends before it has heard about the press.
  sosAtMs = lastSosMs;
  sosFloorSeq = lastSeq;
  sosSilencedAtMs = 0;
  currentSeq = 0;
  alarmOn = true;
  play(SOS_ALARM);
  Serial.println("# SOS: help requested");
}

static void updateButtons(uint32_t now) {
  if (bootButton.pressed(now, 0)) onBootButton(now);
  if (sosButton.pressed(now, SOS_HOLD_MS)) onSosButton(now);
}

// ---- Link and LED ------------------------------------------------------------------

static void updateLink(uint32_t now) {
  bool serialOk = serialSeen && now - lastSerialMs < LINK_TIMEOUT_MS;
  bool wifiOk = wifiSeen && now - lastWifiMs < LINK_TIMEOUT_MS;
  bool nowLinked = serialOk || wifiOk;
  if (linked && !nowLinked) {
    currentSeq = 0;                            // an alarm still on when HomeShield returns plays again
    if (alarmOn && playing == SOS_ALARM) {
      // Someone asked for help: keep sounding on our own, for SOS_LOCAL_MS.
      sosAtMs = now ? now : 1;
      sosFloorSeq = lastSeq;
      Serial.println("# no word from HomeShield for 15 s; the SOS keeps sounding");
    } else {
      alarmOn = false;
      play(&LOST);                             // replaces any alarm sound
      Serial.println("# no word from HomeShield for 15 s");
    }
  }
  linked = nowLinked;
}

// An SOS nobody answers (HomeShield off) stops here after SOS_LOCAL_MS.
static void updateLocalSos(uint32_t now) {
  if (sosAtMs && !linked && now - sosAtMs > SOS_LOCAL_MS) {
    stopAlarm();
    Serial.println("# SOS stopped after 2 minutes with no answer from HomeShield");
  }
}

static void updateLed(uint32_t now) {
  bool on;
  if (alarmOn) {
    on = (now / 125) % 2;                      // fast blink: sounding
  } else if (linked) {
    on = true;                                 // steady: connected and quiet
  } else {
    on = (now % 2000) < 80;                    // short flash: waiting for HomeShield
  }
  digitalWrite(LED_PIN, on ? HIGH : LOW);
}

// ---- Arduino -------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
#if BUZZER_PASSIVE
  passiveBegin();
#else
  digitalWrite(BUZZER_PIN, !BUZZER_ON_LEVEL);  // off from the first moment
  pinMode(BUZZER_PIN, OUTPUT);
  fullDrive();
  buzzerOutput(0);
#endif
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(SOS_PIN, INPUT_PULLUP);

  sayHello();
  play(&CHIRP);

  if (wifiConfigured()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    wifiInbox = xQueueCreate(4, sizeof(Message));
    xTaskCreatePinnedToCore(wifiTask, "homeshield-wifi", 6144, nullptr, 1, nullptr, 0);
  }
}

void loop() {
  uint32_t now = millis();
  readSerial();
  readWifiInbox();
  updateButtons(now);
  updateLink(now);
  updateLocalSos(now);
  updateSound(now);
  updateLed(now);
  reportWifi();
  delay(2);
}
