# 🔋 Smart Battery Management System (BMS)

A single-cell Battery Management System built on ESP32 for real-time monitoring of a Li-ion cell's voltage, current, temperature, and estimated State of Charge (SoC), with live telemetry on an OLED display and a Blynk IoT dashboard.

---

## 🚀 Features

- ⚡ Real-time voltage and current monitoring via INA219 (I2C)
- 🌡️ Temperature monitoring via DS18B20 (1-Wire)
- 🔋 State of Charge (SoC) estimation using an OCV lookup table, corrected with Coulomb counting during discharge and smoothed with an exponential filter
- 🔌 Charging vs. idle/discharge detection using a debounced voltage-slope algorithm
- 📱 Live telemetry pushed to a Blynk IoT dashboard
- 📟 OLED display showing voltage, current, temperature, and SoC
- 🎛️ Manual discharge-cycle control via serial commands (`D` to start discharge, `S` to stop), which also switches a MOSFET on `GPIO26`

---

## 🛠️ Tech Stack

- **Microcontroller:** ESP32
- **Sensors:** INA219 (voltage/current), DS18B20 (temperature)
- **Display:** OLED (SSD1306)
- **IoT Platform:** Blynk
- **Language:** Arduino (C++)

---

## ⚙️ Working

1. Samples voltage, current, and temperature every 1 second.
2. Detects charging vs. discharging/idle by tracking the sign and persistence of short-term voltage changes (a rising/falling sample counter constrained to 0–20, with charging flagged once the counter crosses a threshold and the cell is below 4.18 V).
3. Estimates SoC:
   - On discharge (`current_mA > 5`): subtracts drawn charge from the running SoC using Coulomb counting against a fixed rated capacity.
   - Otherwise (charging or idle): blends the current SoC estimate toward a fresh OCV-table lookup (a different blend weight is used depending on whether the cell is charging or idle).
   - The result is passed through an additional exponential smoothing filter before display.
4. Displays voltage, current, temperature, and filtered SoC on the OLED and streams the same values to the Blynk dashboard.
5. Accepts serial commands to manually start/stop a discharge cycle, toggling the MOSFET on `GPIO26` accordingly.

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
- MOSFET (manually switched, not part of an automatic protection circuit)
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

## 👩‍💻 Author

**Shriya Choksi**
