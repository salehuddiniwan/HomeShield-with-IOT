"""What can be wired to an ESP32 node, and on which pins.

A device has a kind (what it is to people: a light, a door lock, a gas
sensor) and the kind has a pin type (what the firmware does with the pin):

    out     relay or transistor, on/off             light, socket, fan, lock, valve, relay
    pwm     0-100 % duty cycle                      dimmer, fan with speed control
    buzzer  plays HomeShield's alarm patterns       buzzer
    in      digital input, reported on change       sos, motion, flame, water
    adc     analog input in millivolts, every N s   gas (MQ-2 or MQ-5); firmware 1.0-1.1 sent 0-4095
    dht11 / dht22   temperature and humidity        climate

Pin rules depend on the board: the 30-pin ESP32 DevKit (ESP-WROOM-32), or
the ESP32-C3 SuperMini. A board says which it is in its info message.
"""

from __future__ import annotations

from typing import Any

# kind -> what it is, its pin type, and its default options. "inv" means the
# device is on (or active) when the pin is LOW: most relay modules, flame and
# water modules, and buzzer modules printed 低电平触发.
KINDS: dict[str, dict[str, Any]] = {
    "light":   {"label": "Light", "type": "out", "options": {"inv": True}},
    "socket":  {"label": "Socket", "type": "out", "options": {"inv": True}},
    "fan":     {"label": "Fan", "type": "out", "options": {"inv": True}},
    "relay":   {"label": "Relay", "type": "out", "options": {"inv": True}},
    "lock":    {"label": "Door lock", "type": "out", "options": {"inv": True, "auto_off": 60}},
    "valve":   {"label": "Valve", "type": "out", "options": {"inv": True}},
    "dimmer":  {"label": "Dimmable light", "type": "pwm", "options": {"inv": False}},
    # A DC fan through a MOSFET or PWM fan driver: a relay can only switch a fan on or off.
    "fan_pwm": {"label": "Fan with speed control", "type": "pwm", "options": {"inv": False}},
    "buzzer":  {"label": "Alarm buzzer", "type": "buzzer",
                "options": {"inv": True, "passive": True, "hz": 2700}},
    "sos":     {"label": "SOS button", "type": "in",
                "options": {"inv": True, "pull": True, "hold": 300, "role": "sos"}},
    "motion":  {"label": "Motion sensor (PIR)", "type": "in", "options": {"inv": False}},
    "flame":   {"label": "Flame sensor", "type": "in", "options": {"inv": True}},
    "water":   {"label": "Water leak sensor", "type": "in", "options": {"inv": True}},
    "gas":     {"label": "Gas sensor (MQ-2 / MQ-5)", "type": "adc", "options": {"every": 2, "model": "mq2"}},
    "climate": {"label": "Temperature & humidity (DHT)", "type": "dht22",
                "options": {"model": "dht22", "every": 10}},
}
OUTPUT_KINDS = frozenset(k for k, v in KINDS.items() if v["type"] in ("out", "pwm", "buzzer"))
SWITCH_KINDS = frozenset(k for k, v in KINDS.items() if v["type"] == "out")

# A speed fan's three speeds, as duty cycles. Below about 40 % a small DC fan may
# not start turning.
FAN_SPEEDS = {"40": "Low", "70": "Medium", "100": "High"}

# Which "model" options a kind takes: the DHT type, or the MQ gas sensor.
MODELS = {"climate": ("dht11", "dht22"), "gas": ("mq2", "mq5")}

# Option -> (lowest, highest) for the numeric ones people can change.
NUMERIC_OPTIONS = {"auto_off": (0, 3600), "hold": (0, 5000), "every": (1, 3600),
                   "hz": (500, 6000)}

# ---- boards and their pins ------------------------------------------------------

ALL_PINS = (4, 5, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33,
            34, 35, 36, 39)
INPUT_ONLY = frozenset({34, 35, 36, 39})          # no output, no internal pull-up
ADC1 = frozenset({32, 33, 34, 35, 36, 39})         # analog that still works with Wi-Fi on
RESERVED = {
    0: "GPIO 0 is the BOOT button.",
    1: "GPIO 1 is the USB serial port (TX).",
    2: "GPIO 2 is the board's blue LED.",
    3: "GPIO 3 is the USB serial port (RX).",
    12: "GPIO 12 decides the flash voltage at start-up; anything wired to it can stop the board booting.",
    **{p: f"GPIO {p} is wired to the flash chip." for p in range(6, 12)},
}
LABELS = {36: "VP", 39: "VN"}

BOARDS: dict[str, dict[str, Any]] = {
    "esp32": {
        "label": "ESP32 DevKit", "pins": ALL_PINS, "input_only": INPUT_ONLY, "adc": ADC1,
        "reserved": RESERVED, "labels": LABELS,
        "adc_hint": "GPIO 32, 33, 34, 35, 36 (VP) or 39 (VN)",
        "out_hint": "GPIO 4, 13, 14, 16-19, 21-23, 25-27, 32 or 33",
        "pull_hint": "GPIO 33, 32 or 27",
    },
    # GPIO 0-10, 20 and 21 are brought out. 2, 8 and 9 set how the chip starts;
    # 8 also drives the blue LED and 9 is the BOOT button. Only ADC1 (0-4) reads
    # analog with Wi-Fi on.
    "esp32c3": {
        "label": "ESP32-C3 SuperMini", "pins": (0, 1, 3, 4, 5, 6, 7, 10, 20, 21),
        "input_only": frozenset(), "adc": frozenset({0, 1, 3, 4}),
        "reserved": {
            2: "GPIO 2 decides how the board starts; anything wired to it can stop it booting.",
            8: "GPIO 8 is the board's blue LED, and decides how the board starts.",
            9: "GPIO 9 is the BOOT button.",
        },
        "labels": {20: "RX", 21: "TX"},
        "adc_hint": "GPIO 0, 1, 3 or 4",
        "out_hint": "GPIO 0, 1, 3, 4, 5, 6, 7, 10, 20 or 21",
        "pull_hint": "GPIO 3 or 4",
    },
}
DEFAULT_BOARD = "esp32"


def board_info(board: Any) -> dict[str, Any]:
    return BOARDS.get(board, BOARDS[DEFAULT_BOARD])


class PinError(ValueError):
    """A pin that can't do what was asked; the message says why."""


def pin_type(kind: str, options: dict[str, Any]) -> str:
    t = KINDS[kind]["type"]
    if kind == "climate":
        return "dht11" if options.get("model") == "dht11" else "dht22"
    return t


def check_pin(pin: int, kind: str, options: dict[str, Any], board: str = DEFAULT_BOARD) -> None:
    """Raise PinError with a reason if this pin can't serve this kind on this board."""
    b = board_info(board)
    if pin in b["reserved"]:
        raise PinError(b["reserved"][pin])
    if pin not in b["pins"]:
        raise PinError(f"GPIO {pin} isn't available on an {b['label']}.")
    t = pin_type(kind, options)
    label = KINDS[kind]["label"]
    if t == "adc" and pin not in b["adc"]:
        raise PinError(f"A {label.lower()} needs an analog pin that works with Wi-Fi on: {b['adc_hint']}.")
    if t in ("out", "pwm", "buzzer", "dht11", "dht22") and pin in b["input_only"]:
        raise PinError(f"GPIO {pin} can only read, so it can't drive a {label.lower()}. "
                       f"Use one of {b['out_hint']}.")
    if t == "in" and options.get("pull") and pin in b["input_only"]:
        raise PinError(f"GPIO {pin} has no built-in pull-up, which a button needs. "
                       f"Use another pin, e.g. {b['pull_hint']}.")


def pin_choices(kind: str, options: dict[str, Any], taken: set[int],
                board: str = DEFAULT_BOARD) -> list[dict[str, Any]]:
    """Every pin of the board for the pin picker: usable or not, and why not.
    The board's reserved pins are listed too (never usable), so nobody wonders
    where GPIO 2 went."""
    b = board_info(board)
    out = []
    for pin in sorted(set(b["pins"]) | set(b["reserved"])):
        why = ""
        if pin in taken:
            why = "Already in use on this board."
        else:
            try:
                check_pin(pin, kind, options, board)
            except PinError as e:
                why = str(e)
        out.append({"pin": pin, "label": f"GPIO {pin}" + (f" ({b['labels'][pin]})" if pin in b["labels"] else ""),
                    "ok": not why, "why": why})
    return out


def clean_options(kind: str, given: dict[str, Any]) -> dict[str, Any]:
    """The kind's defaults, overridden by the given options that apply to it."""
    opts = dict(KINDS[kind]["options"])
    for key, value in (given or {}).items():
        if key not in opts and key not in ("inv",):
            continue
        if key in ("inv", "passive", "pull"):
            opts[key] = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
        elif key in NUMERIC_OPTIONS:
            lo, hi = NUMERIC_OPTIONS[key]
            try:
                n = int(float(value))
            except (TypeError, ValueError):
                raise PinError(f"{key} needs a number.") from None
            if not lo <= n <= hi:
                raise PinError(f"{key} must be between {lo} and {hi}.")
            opts[key] = n
        elif key == "model":
            models = MODELS.get(kind, ())
            if value not in models:
                raise PinError(f"The sensor model is {' or '.join(models)}.")
            opts[key] = value
    return opts


def firmware_entry(pin: int, kind: str, options: dict[str, Any]) -> dict[str, Any]:
    """One pin of the config sent to the node (short keys: the board has little RAM)."""
    t = pin_type(kind, options)
    entry: dict[str, Any] = {"pin": pin, "t": t, "inv": 1 if options.get("inv") else 0}
    if t == "out" and options.get("auto_off"):
        entry["auto"] = int(options["auto_off"])
    if t == "buzzer":
        entry["passive"] = 1 if options.get("passive") else 0
        entry["hz"] = int(options.get("hz", 2700))
    if t == "in":
        entry["pull"] = 1 if options.get("pull") else 0
        if options.get("role"):
            entry["role"] = options["role"]
            entry["hold"] = int(options.get("hold", 300))
    if t in ("adc", "dht11", "dht22"):
        entry["every"] = int(options.get("every", 2 if t == "adc" else 10))
    return entry


def describe(kind: str, value: Any) -> str:
    """A state as people say it: Locked, Open, 75 %, On."""
    text = str(value)
    t = KINDS.get(kind, {}).get("type")
    if t == "out":
        on = text == "1"
        if kind == "lock":
            return "Unlocked" if on else "Locked"
        if kind == "valve":
            return "Open" if on else "Closed"
        return "On" if on else "Off"
    if t == "pwm":
        if text in ("0", ""):
            return "Off"
        return FAN_SPEEDS.get(text, f"{text} %") if kind == "fan_pwm" else f"{text} %"
    if t == "buzzer":
        return {"test": "Test", "stop": "Stopped"}.get(text, text)
    return text


def normalise_value(kind: str, value: Any) -> str:
    """What a person asked for, as the payload the node understands."""
    t = KINDS[kind]["type"]
    text = str(value).strip().lower()
    if t == "out":
        on = {"on": "1", "1": "1", "true": "1", "open": "1", "unlock": "1", "unlocked": "1"}
        off = {"off": "0", "0": "0", "false": "0", "close": "0", "closed": "0",
               "lock": "0", "locked": "0"}
        if text in on:
            return on[text]
        if text in off:
            return off[text]
        raise PinError("Switch it on or off.")
    if t == "pwm":
        if text in ("on",):
            return "100"
        if text in ("off",):
            return "0"
        what = "a speed" if kind == "fan_pwm" else "a brightness"
        try:
            n = int(round(float(text)))
        except ValueError:
            raise PinError(f"Set {what} from 0 to 100.") from None
        if not 0 <= n <= 100:
            raise PinError(f"Set {what} from 0 to 100.")
        return str(n)
    if t == "buzzer":
        if text in ("test", "on", "1"):
            return "test"
        if text in ("off", "0", "stop"):
            return "stop"
        raise PinError("A buzzer can be tested or stopped.")
    raise PinError(f"A {KINDS[kind]['label'].lower()} is a sensor: it can't be switched.")
