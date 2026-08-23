# 🔋 Smart Battery Management System (BMS)
 
A single-cell Battery Management System built on ESP32 for real-time monitoring of a Li-ion cell's voltage, current, temperature, and estimated State of Charge (SoC), with live telemetry on an OLED display and a Blynk IoT dashboard.
 
---
 
## 🚀 Features
 
- ⚡ Real-time voltage and current monitoring via INA219 (I2C)
- 🌡️ Temperature monitoring via DS18B20 (1-Wire)
- 🔋 State of Charge (SoC) estimation using an OCV lookup table, corrected with Coulomb counting during discharge and smoothed with an exponential filter
- 🔌 Charging vs. idle/discharge detection using a debounced voltage-slope algorithm
- 🛡️ Safety threshold enforcement (undervoltage, overcurrent, overtemperature) with a latched fault state — the MOSFET is forced to its safe/idle state on any violation and stays there until explicitly cleared via serial command, and only if the triggering condition has resolved
- ⏱️ Timer-interrupt-driven sampling: a hardware timer fires at a fixed interval and signals the main loop via a semaphore, replacing blocking `delay()`-based polling; sampling jitter and ISR-fire vs. samples-processed counts are tracked for verification
- 📱 Live telemetry pushed to a Blynk IoT dashboard
- 📟 OLED display showing voltage, current, temperature, SoC, and fault status
- 🎛️ Manual discharge-cycle control via serial commands (`D` to start discharge, `S` to stop, `C` to clear a latched fault, `J` to print sampling/jitter stats), which also switches a MOSFET on `GPIO26`
---
 
## 🛠️ Tech Stack
 
- **Microcontroller:** ESP32
- **Sensors:** INA219 (voltage/current), DS18B20 (temperature)
- **Display:** OLED (SSD1306)
- **IoT Platform:** Blynk
- **Language:** Arduino (C++)
---
 
## ⚙️ Working
 
1. A hardware timer interrupt fires every 1 second and signals the main loop via a semaphore; the loop reads voltage, current, and temperature when signaled, rather than polling on a blocking `delay()`. The ISR itself only increments a counter and gives the semaphore — the actual I2C/1-Wire reads happen in the main loop, since both buses are too slow/blocking to run safely inside interrupt context.
2. On each sample, voltage, current, and temperature are checked against fixed thresholds (`CUTOFF_VOLTAGE`, `MAX_CURRENT`, `MAX_TEMP`). Any violation forces the MOSFET to its safe/idle state, blocks further discharge start, and latches a fault flag that will not auto-clear — it requires an explicit `C` command, which itself only clears the latch if the most recent readings are back within safe limits.
3. Detects charging vs. discharging/idle by tracking the sign and persistence of short-term voltage changes (a rising/falling sample counter constrained to 0–20, with charging flagged once the counter crosses a threshold and the cell is below 4.18 V).
4. Estimates SoC:
   - On discharge (`current_mA > 5`): subtracts drawn charge from the running SoC using Coulomb counting against a fixed rated capacity.
   - Otherwise (charging or idle): blends the current SoC estimate toward a fresh OCV-table lookup (a different blend weight is used depending on whether the cell is charging or idle).
   - The result is passed through an additional exponential smoothing filter before display.
5. Displays voltage, current, temperature, filtered SoC, and fault status on the OLED and streams the same values to the Blynk dashboard.
6. Accepts serial commands to manually start/stop a discharge cycle (`D`/`S`), clear a latched fault (`C`), and print sampling/jitter diagnostics (`J`), toggling the MOSFET on `GPIO26` accordingly.
---
## 📸 Project Images

### Hardware Setup
![Setup](images/setup.jpg)

### OLED Output
![OLED](images/oled_display.jpg)

### Blynk Dashboard
![Blynk](images/blynk_dashboard.png)

---

## 🔌 Circuit Components
 
- ESP32
- INA219 sensor
- DS18B20 temperature sensor
- OLED display
- MOSFET (IRLZ44N, driven through a BC548 transistor stage; switched both manually via serial command and automatically on a latched safety fault)
- Single Li-ion cell
---

## ▶️ How to Run

1. Clone the repository.
2. Open `code/smart_bms.ino` in the Arduino IDE.
3. Install required libraries:
   - Adafruit INA219
   - DallasTemperature
   - Adafruit SSD1306
   - Blynk
4. Update WiFi and Blynk credentials in the sketch.
5. Upload to ESP32.

---


## 📊 Future Improvements

- Automatic protection logic wired to the existing cutoff constants
- Multi-cell support with passive cell balancing
- CAN bus interface for automotive integration
- Cloud data logging
- Mobile app UI improvements

---

## 📊 Future Improvements
 
- Multi-cell support with passive cell balancing
- CAN bus interface for automotive integration
- Cloud data logging
- Mobile app UI improvements

## 👩‍💻 Author

**Shriya Choksi**
