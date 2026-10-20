"""Pretend ESP32 nodes, for trying HomeShield's Devices and ESP32 nodes pages
without the hardware. Each one talks MQTT exactly like firmware/HomeShieldNode:
it introduces itself, can be adopted, takes a pin map, obeys switches and
reports sensor readings (made-up gas, temperature and motion values).

    python tools/sim_node.py --key <device key> --boards 3

The device key is on the ESP32 nodes page (Show or Copy). New boards appear under
ESP32 nodes > Found on your network; their adopted names are remembered in
tools/sim_nodes.json, so a restart brings them back as the same nodes.
Ctrl+C stops them (HomeShield then shows them offline).

To try the Sensors page, put readings in tools/sim_control.json while it
runs, per node and pin: millivolts for a gas sensor, 1/0 for the others.

    {"node-kitchen": {"34": 1500, "33": 1}}     gas leak (about 2000 ppm), flame
    {}                                          everything back to normal
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

STATE_FILE = Path(__file__).with_name("sim_nodes.json")
CONTROL_FILE = Path(__file__).with_name("sim_control.json")
FIRMWARE = "1.2"
CLEAN_MV = 200                                  # an MQ-2 in clean air, through a 1:2 divider


class SimNode:
    def __init__(self, chip: str, args, state: dict):
        self.chip, self.args, self.state = chip, args, state
        self.id = state.get(chip) or chip
        self.pins: dict[int, dict] = {}
        self.values: dict[int, object] = {}
        self.off_at: dict[int, float] = {}
        self.client = None
        self.started = time.time()
        self.rssi = random.randint(-70, -48)
        self.ip = f"192.168.1.{random.randint(50, 99)}"

    def base(self, rest: str) -> str:
        return f"homeshield/{self.id}/{rest}"

    def connect(self) -> None:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.id)
        c.username_pw_set("node", self.args.key)
        c.will_set(self.base("status"), "offline", qos=1, retain=True)
        c.on_connect = self.on_connect
        c.on_message = self.on_message
        c.connect(self.args.server, self.args.port, keepalive=20)
        c.loop_start()
        self.client = c

    def pub(self, rest: str, payload, retain=False) -> None:
        self.client.publish(self.base(rest), str(payload), qos=1, retain=retain)

    def on_connect(self, c, userdata, flags, rc, props=None):
        if rc.is_failure:
            print(f"[{self.id}] broker refused: {rc} (check --key)")
            return
        print(f"[{self.id}] connected")
        self.pub("status", "online", retain=True)
        self.info()
        for t in ("config", "cmd", "pin/+/set") + (("adopt",) if self.id == self.chip else ()):
            c.subscribe(self.base(t), 1)
        c.subscribe("homeshield/alarm", 1)

    def info(self) -> None:
        self.pub("info", json.dumps({"fw": FIRMWARE, "ip": self.ip, "rssi": self.rssi,
                                     "up": int(time.time() - self.started),
                                     "mac": "SI:MU:LA:TE:" + self.chip[-4:-2] + ":" + self.chip[-2:],
                                     "heap": 180000, "chip": self.chip}), retain=True)

    def on_message(self, c, userdata, m):
        payload = m.payload.decode()
        rest = m.topic.split("/", 2)[-1] if m.topic != "homeshield/alarm" else "alarm"
        if rest == "alarm":
            if any(p["t"] == "buzzer" for p in self.pins.values()):
                print(f"[{self.id}] alarm: {payload}")
        elif rest == "config" and payload:
            cfg = json.loads(payload)
            self.pins = {p["pin"]: p for p in cfg.get("pins", [])}
            self.pub("config/ack", cfg.get("v"))
            for pin, p in self.pins.items():
                if p["t"] in ("out", "pwm"):
                    self.values[pin] = self.values.get(pin, 0)
                    self.pub(f"pin/{pin}/state", self.values[pin], retain=True)
            print(f"[{self.id}] pin map: {sorted(self.pins)}")
        elif rest == "adopt":
            new = json.loads(payload)["id"]
            self.pub("status", "", retain=True)
            self.pub("info", "", retain=True)
            self.state[self.chip] = new
            save_state(self.state)
            print(f"[{self.chip}] adopted as {new}; restarting")
            c.disconnect()
            c.loop_stop()
            self.id = new
            time.sleep(0.5)
            self.connect()
        elif rest == "cmd":
            if payload == "forget":
                self.state.pop(self.chip, None)
                save_state(self.state)
                c.disconnect()
                c.loop_stop()
                self.id, self.pins = self.chip, {}
                time.sleep(0.5)
                self.connect()
            else:
                print(f"[{self.id}] command: {payload}")
        elif rest.startswith("pin/") and rest.endswith("/set"):
            pin = int(rest.split("/")[1])
            p = self.pins.get(pin)
            if not p:
                return
            time.sleep(0.15)                       # a real relay takes a moment
            if p["t"] == "out":
                self.values[pin] = 1 if payload == "1" else 0
                if self.values[pin] and p.get("auto"):
                    self.off_at[pin] = time.time() + p["auto"]
            elif p["t"] == "pwm":
                self.values[pin] = int(payload)
            elif p["t"] == "buzzer":
                print(f"[{self.id}] buzzer: {payload}")
                return
            self.pub(f"pin/{pin}/state", self.values[pin], retain=True)

    def step(self, now: float, control: dict) -> None:
        forced = {int(k): v for k, v in (control.get(self.id) or {}).items() if str(k).isdigit()}
        for pin, p in self.pins.items():
            t = p["t"]
            if pin in forced and t in ("adc", "in"):
                value = int(forced[pin])
                if t == "adc" and now - self.values.get(("t", pin), 0) >= p.get("every", 2):
                    self.values[("t", pin)] = now
                    self.pub(f"pin/{pin}/state", json.dumps({"mv": value + random.randint(-15, 15)}),
                             retain=True)
                elif t == "in" and self.values.get(pin) != value:
                    self.values[pin] = value
                    self.pub(f"pin/{pin}/state", value, retain=True)
                continue
            if t == "out" and self.off_at.get(pin) and now >= self.off_at[pin]:
                self.off_at.pop(pin)
                self.values[pin] = 0
                self.pub(f"pin/{pin}/state", 0, retain=True)
            elif t == "adc" and now - self.values.get(("t", pin), 0) >= p.get("every", 2):
                mv = self.values.get(pin, CLEAN_MV + 10) + random.randint(-6, 6)
                self.values[pin] = max(CLEAN_MV, min(CLEAN_MV + 40, mv))
                self.values[("t", pin)] = now
                self.pub(f"pin/{pin}/state", json.dumps({"mv": self.values[pin]}), retain=True)
            elif t in ("dht11", "dht22") and now - self.values.get(("t", pin), 0) >= p.get("every", 10):
                self.values[("t", pin)] = now
                self.pub(f"pin/{pin}/state", json.dumps({"t": round(27.5 + random.uniform(-0.4, 0.4), 1),
                                                         "h": random.randint(63, 69)}), retain=True)
            elif t == "in" and p.get("role") != "sos":
                if pin not in self.values:
                    self.values[pin] = 0
                    self.pub(f"pin/{pin}/state", 0, retain=True)
                elif self.values[pin] == 0 and random.random() < 0.01 and p.get("inv") == 0:
                    self.values[pin] = 1                 # a PIR seeing someone now and then
                    self.pub(f"pin/{pin}/state", 1, retain=True)
                elif self.values[pin] == 1 and random.random() < 0.2:
                    self.values[pin] = 0
                    self.pub(f"pin/{pin}/state", 0, retain=True)
        if now - self.values.get("info_at", 0) >= 60:
            self.values["info_at"] = now
            self.info()


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--key", required=True, help="the device key from the ESP32 nodes page")
    p.add_argument("--server", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1884)
    p.add_argument("--boards", type=int, default=1, help="how many pretend boards")
    args = p.parse_args()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    nodes = [SimNode(f"esp32-51{i:04X}", args, state) for i in range(1, args.boards + 1)]
    for n in nodes:
        n.connect()
    try:
        while True:
            now = time.time()
            try:
                control = json.loads(CONTROL_FILE.read_text(encoding="utf-8")) if CONTROL_FILE.exists() else {}
            except ValueError:
                control = {}
            for n in nodes:
                n.step(now, control)
            time.sleep(0.5)
    except KeyboardInterrupt:
        for n in nodes:                          # a clean disconnect sends no last will
            n.pub("status", "offline", retain=True)
            n.client.disconnect()


if __name__ == "__main__":
    main()
