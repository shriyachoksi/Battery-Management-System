# 1. Flash the ML version (v2) onto your working board

Your hardware is already running **v1** (`code/smart_bms/`). The ML version
(`code/smart_bms_ml/`) uses the **exact same pins**, so you do **not** need to
touch a single wire. This is purely a firmware swap.

| Signal | GPIO | Same in v1 & v2? |
|---|---|---|
| I2C SDA (INA219 + OLED) | 21 | ✅ |
| I2C SCL (INA219 + OLED) | 22 | ✅ |
| DS18B20 data (1-Wire) | 4 | ✅ |
| MOSFET control (active-low) | 26 | ✅ |

If v1 runs, v2 will run on the same board.

---

## Step 1 — Pre-flight (one-time setup)

1. **Board package.** In Arduino IDE → Boards Manager, install **esp32 by Espressif v3.x**.
   v2 uses the 3.x timer API (`timerBegin(frequency)`, `timerAlarm`); it will not
   compile on 2.x. (You likely already have this from v1.)

2. **Libraries** (Library Manager) — same set as v1:
   - Adafruit INA219
   - DallasTemperature (pulls in OneWire)
   - Adafruit SSD1306 (pulls in Adafruit GFX)
   - Blynk

3. **Credentials.** Copy your existing secrets into the new sketch's folder:
   ```
   copy code\smart_bms\secrets.h  code\smart_bms_ml\secrets.h
   ```
   (Each sketch includes `secrets.h` from its own folder, and `secrets.h` is
   gitignored.) If you don't have one yet, copy `secrets_example.h` → `secrets.h`
   and fill in `BLYNK_TEMPLATE_ID`, `BLYNK_TEMPLATE_NAME`, `BLYNK_AUTH_TOKEN`,
   `WIFI_SSID`, `WIFI_PASS`.

4. **Set your cell's capacity.** In `code/smart_bms_ml/smart_bms_ml.ino`, line ~30:
   ```cpp
   #define RATED_CAPACITY 2500.0 // change depending on battery
   ```
   Set this to your 18650's rated mAh (the blue cell in your setup photo is
   printed on its wrapper). Getting this close matters — SoC, SoH and
   time-to-empty all scale off it.

---

## Step 2 — Flash

1. Open `code/smart_bms_ml/smart_bms_ml.ino`.
2. Select your ESP32 board and COM port.
3. Upload. First compile is slow (ML header + Blynk); that's normal.

---

## Step 3 — Confirm it booted (Serial Monitor @ 115200)

You should see, in order:

```
=== Smart BMS v2 (ML) Starting ===
SoH 100.0% (usable capacity 2500 mAh)      <- your capacity
Connecting to WiFi: <your ssid>
...
WiFi Connected!
IP: 192.168.x.x
Blynk Connected!
Smart BMS Ready. Commands: D S C J L(og) M(L status)
```

If you see `INA219 ERROR` or `OLED ERROR`, the board halts — that's an I2C wiring
issue, same as it would be on v1 (see Troubleshooting).

---

## Step 4 — Feature verification checklist

Run these in order. Each line is a feature you can point at during the demo.

| # | What to do | Expected result | Proves |
|---|---|---|---|
| 1 | Watch the OLED | Shows `V mA`, `T / SoH`, `SoC:x% (v1 y%)`, state, `ML: settling` | Display + SoH persistence |
| 2 | Open Serial, type `M` | Prints ML status (features, fused/MLP/v1 SoC, anomaly score vs threshold, SoH, time-to-empty) | ML engine is live |
| 3 | Wait ~3 min idle, type `M` again | `ML ready: yes`; OLED shows `ML: normal` | Settling → ML active |
| 4 | Compare `SoC fused` vs `SoC v1` in `M` | Two different numbers | ML is correcting v1, not copying it |
| 5 | Type `D` (or flip the dashboard switch) | MOSFET enables load, state → `DISCHARGING`, current goes positive | Discharge control + MOSFET |
| 6 | While discharging, watch OLED | `Empty in XhYYm` appears | Time-to-empty |
| 7 | Type `S` | Load disconnects, current → ~0 | Stop control |
| 8 | Type `J` | Prints `isr_fires / samples_processed / dropped / max_jitter_ms` | Timer-interrupt sampling (dropped≈0) |
| 9 | Unplug the DS18B20 briefly | OLED → `SENSOR FAULT / check wiring`, fault latches; replug + type `C` to clear | Sensor validation + latched faults |
| 10 | Let a full discharge run to the 3.0 V cutoff from ~full | Serial prints `Capacity test: ... SoH x.x%`, SoH updates & is saved to flash | SoH measurement |

> **Anomaly detection (feature 11)** is hard to trigger safely on demand — it
> needs unusual heating or a voltage drop. Don't force it live. Show it instead
> with `ml/reports/anomaly_detection.png` and the 15/15 detection stat. The
> warning, when it fires, latches on OLED (`ML: check cell (C)`) and Blynk (V8)
> until you acknowledge with `C`.

---

## The ML settling window (read this before demoing)

- At boot and for **180 s after any load/charger switch**, the ML is in a blind
  spot by design (the cell's polarisation hasn't relaxed). OLED shows
  `ML: settling`; Coulomb counting carries SoC.
- So if you type `D`, the ML re-enters settling for 3 minutes. Plan your demo
  narration around this (guide 4 does).
- To check where you are: type `M` — it tells you `yes` / `no (window filling,
  or < 3 min since a load/charger switch)`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `INA219 ERROR`, board halts | INA219 not on I2C | Check SDA=21, SCL=22, 3V3, GND; `0x40` address |
| `OLED ERROR`, board halts | OLED address/wiring | Confirm `0x3C`; some modules are `0x3D` |
| Compile fails on `timerBegin` | esp32 core is 2.x | Upgrade to esp32 **3.x** |
| `secrets.h: No such file` | Not copied into `smart_bms_ml/` | Redo Pre-flight step 3 |
| OLED stuck on `ML: settling` | Normal for first ~3 min | Wait; confirm with `M` |
| Temp reads `-127` → sensor fault | DS18B20 disconnected / no pull-up | Check data on GPIO4 and its 4.7 kΩ pull-up to 3V3 |
| Blynk offline but WiFi ok | Wrong template/token | Re-check `secrets.h` Blynk fields; see guide 2 |
| SoC/time-to-empty look wrong | `RATED_CAPACITY` not set to your cell | Set it (Pre-flight step 4), reflash |

Once every checklist row passes, move to [guide 2 — the dashboard](02_blynk_dashboard.md).
