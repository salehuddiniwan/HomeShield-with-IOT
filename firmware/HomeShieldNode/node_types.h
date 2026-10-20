// Types for HomeShieldNode.ino. They live here because the Arduino IDE writes
// declarations of the sketch's functions above its first function, which is
// above any struct defined in the .ino itself.

#pragma once

#include <Arduino.h>
#include <DHT.h>

// One step of a buzzer pattern: how long, and whether it sounds.
struct Step {
  uint16_t ms;
  bool on;
};

struct Pattern {
  const char *name;
  const Step *steps;
  uint8_t count;
  bool repeat;
};

enum ChanType : uint8_t { T_NONE, T_OUT, T_PWM, T_BUZZER, T_IN, T_ADC, T_DHT11, T_DHT22 };

// One pin of the pin map HomeShield sends.
struct Chan {
  uint8_t pin;
  ChanType type;
  bool inv;            // on/active when the pin is LOW
  bool pull;           // internal pull-up (buttons)
  bool passive;        // buzzer needs a tone
  bool sos;            // input is the SOS button
  uint16_t hz;
  uint16_t holdMs;
  uint32_t autoMs;     // outputs: switch off again after this long
  uint32_t everyMs;    // sensors: read this often
  int value;           // what it is now: 0/1, 0-100, raw reading
  uint32_t offAt;
  uint32_t lastReadMs;
  uint32_t lastSentMs;
  int lastRaw, stable;
  uint32_t changedAt, downAt;
  bool fired;
  DHT *dht;
};
