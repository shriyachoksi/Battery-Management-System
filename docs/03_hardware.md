# 3. Hardware reference

Everything an audience (or a report) needs to understand the build. The pin map is
taken directly from the firmware, so it's authoritative. The driver-stage and
power details are described from the schematic topology — **confirm the exact
resistor/shunt values against your actual breadboard** before putting them on a slide.

---

## Pin map (ESP32 ↔ peripherals)

Hard-coded in both `smart_bms.ino` and `smart_bms_ml.ino` — identical in v1 and v2.

| ESP32 pin | Connects to | Bus / role |
|---|---|---|
| GPIO21 | INA219 SDA, OLED SDA | I2C data (shared) |
| GPIO22 | INA219 SCL, OLED SCL | I2C clock (shared) |
| GPIO4 | DS18B20 DQ (data) | 1-Wire (needs 4.7 kΩ pull-up to 3V3) |
| GPIO26 | MOSFET driver input | Discharge control, **active-low** (HIGH = load OFF / safe) |
| 3V3 | INA219 VCC, OLED VCC, DS18B20 VDD | Logic power |
| GND | Common ground | All modules + battery negative reference |

**I2C addresses:** INA219 = `0x40` (default), OLED SSD1306 = `0x3C`.

---

## Bill of materials

| Part | Module / value | Role |
|---|---|---|
| MCU | ESP32 dev board | Sampling, ML inference, WiFi, Blynk |
| Current/voltage sensor | INA219 breakout (0.1 Ω shunt) | Bus voltage + current over I2C |
| Temperature sensor | DS18B20 (TO-92, 1-Wire) | Cell temperature |
| Display | 0.96" OLED, SSD1306, I2C | Local telemetry |
| Load switch | IRLZ44N logic-level N-MOSFET | Switches the discharge load |
| MOSFET driver | BC548 NPN transistor + resistors | Level/drive stage from GPIO26 |
| Charger | TP4056 (USB-C) Li-ion charge module | CC/CV charging of the cell |
| Battery | Single 18650 Li-ion cell + holder | Device under management |
| Discharge load | Power resistor (see note) | Draws current during a discharge test |
| Buck converter | (adjustable, e.g. MP1584/LM2596) | Rail regulation if used |
| Passives | 4.7 kΩ (1-Wire pull-up), base/gate resistors, decoupling cap | Support |

> **Load resistor sizing:** the firmware's overcurrent limit is `MAX_CURRENT` =
> 2000 mA and SoH wants a meaningful discharge. Size the load so discharge current
> sits well under 2 A (e.g. a few hundred mA) — comfortably measurable by the
> INA219 and safe for an 18650.

---

## How the signal flows

```
          +-----------------+        I2C (GPIO21/22)        +-----------+
  18650 --| INA219 (V, I)   |------------------------------>|           |
   cell   +-----------------+                               |           |
    |                                                        |  ESP32    |--- WiFi --> Blynk cloud --> phone/web dashboard
    |     +-----------------+        1-Wire (GPIO4)          |           |
    +-----| DS18B20 (temp)  |------------------------------>|  (v2:     |
    |     +-----------------+                               |   ML on   |---- I2C ---> OLED (local readout)
    |                                                        |   device) |
    |     +-----------------+   GPIO26 (active-low)   drive  |           |
    +-----| Load  --[IRLZ44N]---<-- BC548 stage <-----------|           |
    |     +-----------------+                                +-----------+
    |     +-----------------+
    +-----| TP4056 charger  |<--- USB-C (charging)
          +-----------------+
```

1. The INA219 sits in series/across the cell and reports **bus voltage** and
   **current** over I2C.
2. The DS18B20 reports **cell temperature** over 1-Wire.
3. The ESP32 samples all three once per second (hardware-timer driven), runs the
   safety checks and — in v2 — the ML, then drives the **OLED** locally and pushes
   to **Blynk** over WiFi.
4. **GPIO26** controls the discharge path through a **BC548 → IRLZ44N** stage. It's
   active-low: HIGH keeps the load disconnected (the safe/idle state the firmware
   forces on any fault).
5. The **TP4056** charges the cell from USB-C; the firmware detects charging from
   the voltage trend (v1) and feeds it into the SoC/ML logic.

---

## Safety thresholds (what the firmware enforces)

| Limit | Value | Define |
|---|---|---|
| Undervoltage cutoff | 3.0 V | `CUTOFF_VOLTAGE` |
| Overcurrent | 2000 mA | `MAX_CURRENT` |
| Overtemperature | 50 °C | `MAX_TEMP` |

Any violation **latches** a fault, forces the MOSFET to the safe state, and blocks
discharge until you send `C` on serial — and only if the condition has cleared.

---

## For the schematic / Fritzing diagram

Your current `images/setup.jpg` is a real breadboard photo — great for "it's real,"
but the wiring is hard to follow on a slide. A clean **Fritzing** (or
draw.io/KiCad) schematic from the pin map above will read far better in a
presentation. Minimum to draw:

- ESP32 with the four signal pins labelled (21, 22, 4, 26)
- INA219 in the current path between cell and load/charger
- DS18B20 with its 4.7 kΩ pull-up
- OLED on the shared I2C bus
- BC548 → IRLZ44N driver with GPIO26 as input
- TP4056 on the charge side
- The 18650 and the discharge load

Next: [guide 4 — presentation kit](04_presentation.md).
