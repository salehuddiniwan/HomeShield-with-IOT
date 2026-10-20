# HomeShield buzzer and SOS button (ESP32)

An ESP32 board with a buzzer that sounds in the house when HomeShield opens an incident. HomeShield decides when and which alarm; the board plays it, silences it when its **BOOT** button is pressed, and shows its state on the blue LED.

It also carries an **SOS button** for someone who needs help, such as an elderly parent. Holding it for a moment makes the board sound SOS in Morse code at once, even with the computer off. When HomeShield is running, the press becomes a critical **"SOS: help requested"** incident that names the board, sends the WhatsApp alerts and sounds every buzzer in the house.

## What you need

- An **ESP32 DevKit** (the common ESP-WROOM-32 "DevKit V1" board with a **CP2102** USB chip; CH340 boards work too)
- A **buzzer**: an *active* one is simplest (it beeps by itself when powered). A 3-pin buzzer module or a small 2-pin 3–5 V buzzer both work.
- A **push button** for SOS. Any momentary button works; a large one (an arcade or doorbell-style button) is easiest to find and press in a hurry.
- A **USB data cable** (some cables only carry power, and the board then never shows up on the computer)
- Jumper wires
- For a loud 5 V buzzer: an NPN transistor (S8050 or 2N2222) and a 1 kΩ resistor

## Wiring

![Wiring: D25 to the buzzer's +, GND to its −, the SOS button between D33 and GND, and the board's USB port to the HomeShield computer](wiring.png)

(`wiring.svg` is the same drawing as a vector file, for the report.)

Pick the one that matches your buzzer. The pin labels are printed on the board (`D25` = GPIO 25).

**3-pin buzzer module** (pins marked VCC, GND, I/O or S)

```
Module VCC  ->  3V3
Module GND  ->  GND
Module I/O  ->  D25
```

If the module is printed with **低电平触发** or sold as "low level trigger" (the blue MH-FMD modules are), set `BUZZER_ON_LEVEL LOW` in `config.h`, and keep its VCC on **3V3**. For a passive module also set `BUZZER_PASSIVE 1`. `config.h` comes set for the MH-FMD **passive** module: `BUZZER_PASSIVE 1`, `BUZZER_ON_LEVEL LOW`.

**Small 2-pin buzzer**

```
Buzzer +  (longer leg, or the side marked +)  ->  D25
Buzzer -                                      ->  GND
```

**Loud 5 V buzzer, through a transistor**

An ESP32 pin gives 3.3 V and should supply no more than about 20 mA, so a bigger buzzer is switched by a transistor and powered from the board's 5 V pin:

```
D25 ---[1 kΩ]--- B   (NPN transistor)
                 E --- GND
                 C --- Buzzer -
Buzzer + --- VIN (5 V from USB)
```

Check your transistor's datasheet for which leg is which: an S8050 reads E B C with its flat side facing you, but some 2N2222 parts read C B E.

**SOS button**

```
Button, one leg    ->  D33
Button, other leg  ->  GND   (the same GND as the buzzer is fine)
```

A push button has no + or −, so either way round works. The board's own pull-up keeps D33 high until the button pulls it to ground; no resistor is needed. It must be held for 0.3 s (`SOS_HOLD_MS` in `config.h`), so a bump doesn't call for help.

Nothing to wire for the silence button and the LED: the sketch uses the board's own **BOOT** button (GPIO 0) and blue LED (GPIO 2).

## Upload the sketch

1. Install the **Arduino IDE 2** from arduino.cc.
2. **Tools → Board → Boards Manager**, search for **esp32**, and install **esp32 by Espressif Systems**. (If it isn't listed, add `https://espressif.github.io/arduino-esp32/package_esp32_index.json` under **File → Preferences → Additional boards manager URLs** first.)
3. Plug the ESP32 into the computer. Windows normally installs the CP2102 driver by itself, and the board appears in Device Manager under *Ports (COM & LPT)* as **Silicon Labs CP210x USB to UART Bridge (COMx)**. If it shows as an unknown device, install the **CP210x VCP driver** from Silicon Labs.
4. Open `firmware/HomeShieldBuzzer/HomeShieldBuzzer.ino`. Its `config.h` tab holds the settings: change `BUZZER_PIN` or `BUZZER_PASSIVE` if your buzzer differs from the wiring above, and set `DEVICE_ID` to where the board will be (e.g. `bedroom`): SOS alerts name it.
5. **Tools → Board → ESP32 Dev Module**, and **Tools → Port →** the COM port from step 3.
6. If HomeShield is running, free the port first: **Settings → Alarm buzzer → Disabled**, then **Save**. Only one program can use the port at a time.
7. Click **Upload**. If it stops at `Connecting....`, hold the **BOOT** button until the upload starts.
8. Open **Tools → Serial Monitor** at **115200** baud. The board prints `HOMESHIELD-BUZZER 1.3 wifi=off id=buzzer-1` and the buzzer chirps once. To hear a pattern, type `ALARM FIRE` and press Enter; type `STOP` to end it. Hold the SOS button: the board prints `SOS` and plays SOS in Morse code; press **BOOT** to stop it.
9. Close the Serial Monitor, and set the buzzer back to **Enabled** in HomeShield. Within a few seconds **Settings → Alarm buzzer** says it is connected and the LED stays on. Press **Test buzzer** in Settings → Alarm buzzer: three beeps.
10. Hold the SOS button once more. The Live page shows **SOS: help requested** with the board's name, and the WhatsApp numbers get a message. Press **Acknowledge** to close it.

## Make it louder

The sketch already drives the buzzer pin at the ESP32's full strength (about 40 mA instead of the default 20), keeps the buzzer switched fully off between beeps, and, on a passive buzzer, plays every alarm at one tone, the loudest one. Beyond that, loudness comes from the buzzer and its power:

1. **Peel off the sticker.** New buzzers often have a seal over the sound hole (printed "REMOVE SEAL AFTER WASHING"). It muffles them a lot.
2. **Match `BUZZER_PASSIVE` and `BUZZER_ON_LEVEL` to the buzzer.** An active buzzer set to passive sounds rough and quiet; a passive one set to active only clicks. A low level trigger module (低电平触发) set to `HIGH` sits switched on while quiet and sounds weak.
3. **Passive buzzer: use its loudest frequency.** Open the Serial Monitor at 115200 baud and type `SWEEP`. The board plays 1500 to 4500 Hz in steps, printing each one; note the loudest, put it in `BUZZER_HZ` in `config.h`, and upload again.
4. **Give it 5 V instead of 3.3 V.** Most small buzzers are rated 5 V and are clearly quieter on a 3.3 V pin. The board's **VIN** pin has 5 V while it runs on USB.
   - **Low level trigger modules (低电平触发, such as the MH-FMD) must stay on 3V3.** Their transistor is switched off by a HIGH equal to VCC, and the ESP32's HIGH is only 3.3 V: on 5 V the buzzer would never switch off. For 5 V, use a bare buzzer with the transistor wiring above instead.
   - **3-pin module sold as "high level trigger"** (the usual kind): move its **VCC** wire from **3V3** to **VIN**. Its I/O stays on D25. Don't do this with a "low level trigger" module: on 5 V it would never switch off.
   - **2-pin buzzer:** use the transistor wiring above, with the buzzer's + on **VIN**.
5. **Louder still:** a 12 V piezo alarm siren (sold as 95–110 dB) switched by a transistor or a relay module, with its own 12 V supply. Join that supply's GND to the ESP32's GND.

## Wi-Fi (optional)

To put the buzzer away from the computer (in a bedroom, say), give it the Wi-Fi details in `config.h` and upload again:

```cpp
#define WIFI_SSID "YourWiFi"
#define WIFI_PASSWORD "your-password"
#define SERVER_URL "http://192.168.1.10:5000"      // http:// + the address on the ESP32 nodes page + :5000
#define DEVICE_KEY "3f9c..."                        // ESP32 nodes > Add an ESP32 node > Device key (Copy)
#define DEVICE_ID "bedroom"                         // shown in Settings and in SOS alerts
```

Then power it from any USB phone charger. Settings → Alarm buzzer shows it as *Connected over Wi-Fi (bedroom at 192.168.1.23)*.

- The ESP32 only joins **2.4 GHz** Wi-Fi.
- Windows Firewall must let Python accept connections on private networks (the same rule phones need to open the dashboard).
- A board can use USB and Wi-Fi at the same time; HomeShield sends both the same alarm.
- **Make a new key** in Settings stops boards with the old key until you update `DEVICE_KEY` and upload again.

## What it does

| LED | Meaning |
|---|---|
| On | Connected to HomeShield, quiet |
| Fast blink | Sounding |
| Short flash every 2 s | No word from HomeShield |

| Sound | Meaning |
|---|---|
| One short chirp | Started up, or BOOT was pressed while quiet (a quick buzzer check) |
| Three beeps, pause (the smoke-alarm rhythm) | Fire or smoke |
| ··· ––– ··· (SOS in Morse code), pause | Someone pressed an SOS button |
| Two long beeps, pause | Fall |
| Fast beeping without a pause | Intruder |
| Three quick chirps, pause | A child in a danger zone |
| Three beeps, once | Test from Settings |
| Three low beeps, once | No word from HomeShield for 15 s: any alarm stops, so a crashed or switched-off PC never leaves it sounding. An SOS keeps going. |

Pressing **BOOT** while it sounds silences it and tells HomeShield, which records who silenced it. The incident stays open on the dashboard until someone acknowledges it or marks it a false alarm.

**The SOS button, step by step**

1. Held for 0.3 s, the board starts SOS in Morse code itself, without waiting for anything, and reports `SOS`.
2. HomeShield opens a critical incident, **SOS: help requested**, named after the board (`DEVICE_ID`). It sends the WhatsApp alerts and sounds SOS on every buzzer, including this one.
3. It sounds until someone acknowledges the incident, presses **Silence buzzer** or BOOT, or the buzzer's time limit passes. Pressing SOS again sounds it again, even after it was silenced: a person is asking again.
4. If HomeShield can't be reached (the computer is off, or the board is on Wi-Fi without a connection), the board still sounds SOS by itself, for up to 2 minutes or until BOOT is pressed. Nobody is messaged then, so the sound is the only alert.

## How it talks to HomeShield

One line of text per message, at 115200 baud over USB, or as the reply to the board's Wi-Fi poll (`GET /api/device/poll` with the `X-Device-Key` header, about once a second):

| From HomeShield | Meaning |
|---|---|
| `ALARM FIRE 1729` | Play the fire pattern (also `SOS`, `FALL`, `INTRUDER`, `ZONE`, `TEST`) |
| `STOP 1730` | Be quiet |
| `HELLO` | Say who you are |

| From the board | Meaning |
|---|---|
| `HOMESHIELD-BUZZER 1.3 wifi=off id=bedroom` | At start-up, and in reply to `HELLO` |
| `BTN` | BOOT silenced the alarm (over Wi-Fi: `POST /api/device/button`) |
| `SOS` | The SOS button was pressed (over Wi-Fi: `POST /api/device/sos`) |
| `# ...` | A note for a person reading the Serial Monitor |

The number grows each time HomeShield wants a different sound. The board plays each number once, so the same line repeated (over USB it is resent every 2 s, as a heartbeat) never restarts an alarm, or brings back one the button silenced.

Right after an SOS press, HomeShield may still answer `STOP` with the old number for a moment, because the incident is created a fraction of a second later. The board ignores a `STOP` with a number no newer than the one it had when the button was pressed, for up to 10 s, so the SOS keeps sounding until HomeShield's `ALARM SOS` arrives.
