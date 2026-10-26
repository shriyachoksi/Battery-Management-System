// Smart BMS v2 - ML-enhanced. Same hardware as v1 (code/smart_bms); adds an
// MLP SoC estimator fused with Coulomb counting, a Gaussian-mixture anomaly
// detector, capacity-based SoH and time-to-empty. See ml/README.md.

#include "secrets.h"

#include <Wire.h>
#include <Adafruit_INA219.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <BlynkSimpleEsp32.h>
#include <Preferences.h>

#include "bms_ml.h"

// ── Pins & Constants ─────────────────────────
#define ONE_WIRE_BUS 4
#define MOSFET_PIN 26
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1

#define CUTOFF_VOLTAGE 3.0
#define SAMPLE_INTERVAL 1000
#define MAX_TEMP 50
#define MAX_CURRENT 2000

#define RATED_CAPACITY 2500.0 // change depending on battery

static_assert(SAMPLE_INTERVAL == bmsml_model::SAMPLE_PERIOD_MS,
              "the ML features assume this sample interval; retrain (ml/train_models.py) after changing it");

// ── ML options ───────────────────────────────
// The anomaly detector is advisory by default: it warns on the OLED, Blynk
// and serial, and the fixed thresholds above stay in charge of switching.
// Set to 1 to also latch a fault (opening the MOSFET) on an ML anomaly - but
// only after retraining on your own cell's data, since the shipped model was
// trained on simulated cells (ml/README.md).
#define ML_ANOMALY_TRIPS_FAULT 0

// A discharge that starts at or above this SoC and runs to the undervoltage
// cutoff measures the cell's real capacity, which updates SoH.
#define CAPACITY_TEST_MIN_START_SOC 95.0
#define CAPACITY_TEST_MIN_FRACTION 0.4 // results below 40% of rated are rejected as implausible

#define BLYNK_PUSH_INTERVAL 5000 // ms between telemetry pushes to Blynk

// Blynk virtual pins - match these to the datastreams in your Blynk template.
#define VPIN_VOLTAGE V0
#define VPIN_CURRENT V1
#define VPIN_TEMP V2
#define VPIN_SOC V3
#define VPIN_SOH V4
#define VPIN_DISCHARGE V5 // switch widget: 1 = start discharge, 0 = stop
#define VPIN_SOC_V1 V6
#define VPIN_ANOMALY_SCORE V7
#define VPIN_ANOMALY V8
#define VPIN_TIME_TO_EMPTY V9 // minutes, -1 when not discharging

// ── Sensor plausibility bounds ────────────────
// These are physical-plausibility checks (NaN / out-of-range / disconnect
// sentinel), not I2C-transaction-level error checks — the Adafruit_INA219
// library doesn't surface bus ack/nack status through its public API.
// Ranges are intentionally wider than the safety thresholds above (e.g.
// MAX_PLAUSIBLE_CURRENT_MA > MAX_CURRENT) so a genuinely disconnected/failed
// sensor is distinguished from a real overcurrent/overtemp/undervoltage event.
// MAX_PLAUSIBLE_VOLTAGE was 4.2 in v1, which a healthy cell reaches at the end
// of every charge (TP4056 regulates to 4.2 V +/-1%, plus INA219 error), so a
// full charge latched a sensor fault.
#define MIN_PLAUSIBLE_VOLTAGE 0.0
#define MAX_PLAUSIBLE_VOLTAGE 4.5
#define MAX_PLAUSIBLE_CURRENT_MA 5000.0
#define MIN_PLAUSIBLE_TEMP -40.0
#define MAX_PLAUSIBLE_TEMP 85.0
#define DS18B20_POWER_ON_TEMP 85.0 // value read back before the first conversion completes

// ── Objects ──────────────────────────────────
Adafruit_INA219 ina219;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
Preferences prefs;

// ML engine (bms_ml.h): v1 estimator, MLP SoC, fusion, anomaly detection
bmsml::Engine ml;

// ── WiFi Credentials ─────────────────────────
// Values come from secrets.h (gitignored), not hardcoded here.
char auth[] = BLYNK_AUTH_TOKEN;
char ssid[] = WIFI_SSID;
char pass[] = WIFI_PASS;

// ── Variables ────────────────────────────────
bool discharging = false;

// SoH, from capacity tests (persisted in flash)
float sohPercent = 100.0;
float capacitymAh = 0.0; // charge drawn since the current discharge started
float cycleStartSoc = 0.0;

// ML anomaly warning. The detector reacts to how the cell is changing, so its
// flag can drop once a faulty cell settles into a new (hotter) steady state;
// the warning therefore stays latched until acknowledged with 'C'.
bool mlWarningLatched = false;

float timeToEmptyMin = -1;
bool loggingEnabled = false;
unsigned long lastValidSampleMillis = 0;
unsigned long lastBlynkPush = 0;

// Hardware timer sampling
hw_timer_t *sampleTimer = NULL;
volatile SemaphoreHandle_t sampleTimerSemaphore;
portMUX_TYPE sampleTimerMux = portMUX_INITIALIZER_UNLOCKED;
volatile uint32_t sampleIsrCount = 0;

// Sampling jitter tracking (evidence: actual vs expected SAMPLE_INTERVAL)
unsigned long lastFireMillis = 0;
long maxJitterMs = 0;
unsigned long samplesProcessed = 0;

// Fault protection
bool faultLatched = false;
bool faultUnderVoltage = false;
bool faultOverCurrent = false;
bool faultOverTemp = false;
bool faultSensor = false;          // distinct from the threshold faults above: means a reading was invalid/implausible, not that a real limit was crossed
bool faultMlAnomaly = false;       // only set when ML_ANOMALY_TRIPS_FAULT is 1
bool lastSampleSensorValid = true; // whether the most recent sample's readings passed validation; used to block clearing a sensor fault while still disconnected
float lastVoltage = 0.0;
float lastCurrent_mA = 0.0;
float lastTempC = 0.0;

// ── Sensor Validation ─────────────────────────
// Checks readings for NaN and physical-plausibility only. This does NOT
// detect every failure mode (e.g. a stuck-but-still-acking I2C device
// returning a plausible-looking wrong value would pass), but it does catch
// the two failure modes this project previously let through silently:
// a disconnected/failed INA219 producing NaN or an out-of-range value, and
// a disconnected DS18B20 returning its DEVICE_DISCONNECTED_C sentinel
// (-127.0 from the DallasTemperature library).
bool isVoltageValid(float v)
{
    return !isnan(v) && v >= MIN_PLAUSIBLE_VOLTAGE && v <= MAX_PLAUSIBLE_VOLTAGE;
}

bool isCurrentValid(float i)
{
    return !isnan(i) && fabs(i) <= MAX_PLAUSIBLE_CURRENT_MA;
}

bool isTempValid(float t)
{
    if (isnan(t))
        return false;
    if (t == DEVICE_DISCONNECTED_C)
        return false; // DallasTemperature sentinel for a disconnected/failed sensor
    if (t == DS18B20_POWER_ON_TEMP)
        return false; // scratchpad still holds the power-on value: no conversion has completed
    return t >= MIN_PLAUSIBLE_TEMP && t <= MAX_PLAUSIBLE_TEMP;
}

// ── Sample Timer ISR ──────────────────────────
// Runs in hardirq context: keep minimal, no I2C/1-Wire here (both are
// blocking bus transactions and unsafe to run inside an ISR). The ISR only
// increments a counter and signals loop() via semaphore; the actual sensor
// reads happen in loop() when the semaphore is taken.
void ARDUINO_ISR_ATTR onSampleTimer()
{
    portENTER_CRITICAL_ISR(&sampleTimerMux);
    sampleIsrCount++;
    portEXIT_CRITICAL_ISR(&sampleTimerMux);
    xSemaphoreGiveFromISR(sampleTimerSemaphore, NULL);
}

// ── Discharge control ────────────────────────
void startDischarge()
{
    if (faultLatched)
    {
        Serial.println("Discharge blocked: fault latched. Send 'C' to clear.");
        Blynk.virtualWrite(VPIN_DISCHARGE, 0);
        return;
    }
    discharging = true;
    capacitymAh = 0;
    cycleStartSoc = ml.out.socFused;
    digitalWrite(MOSFET_PIN, LOW); // enable discharge path
    Blynk.virtualWrite(VPIN_DISCHARGE, 1);
}

void stopDischarge()
{
    discharging = false;
    digitalWrite(MOSFET_PIN, HIGH); // disconnect load
    Blynk.virtualWrite(VPIN_DISCHARGE, 0);
}

// A discharge from (nearly) full to the cutoff measured the usable capacity.
void finishCapacityTest()
{
    if (!(cycleStartSoc >= CAPACITY_TEST_MIN_START_SOC) || capacitymAh < 100)
        return;

    float measured_mAh = capacitymAh * 100.0 / cycleStartSoc;
    if (measured_mAh < CAPACITY_TEST_MIN_FRACTION * RATED_CAPACITY)
    {
        // Far too little charge for a full cycle: the cutoff came early (a
        // voltage dip from a loose contact, or a wrong start SoC), not from an
        // empty cell. Storing it would wreck Coulomb counting, so ignore it.
        Serial.printf("Capacity test rejected: only %.0f mAh drawn from %.0f%% SoC (early cutoff?)\n",
                      capacitymAh, cycleStartSoc);
        return;
    }
    sohPercent = constrain(measured_mAh / RATED_CAPACITY * 100.0, 0.0, 100.0);
    ml.usableCapacity_mAh = RATED_CAPACITY * sohPercent / 100.0;
    prefs.putFloat("soh", sohPercent);

    Serial.printf("Capacity test: %.0f mAh drawn from %.0f%% SoC -> %.0f mAh, SoH %.1f%%\n",
                  capacitymAh, cycleStartSoc, measured_mAh, sohPercent);
}

// ── WiFi + Blynk ─────────────────────────────
void connectWiFiAndBlynk()
{
    // Configured up front (v1 only did this if WiFi was up at boot, so a board
    // that booted without WiFi never reached Blynk after WiFi came back).
    Blynk.config(auth);

    Serial.print("Connecting to WiFi: ");
    Serial.println(ssid);

    WiFi.begin(ssid, pass);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40)
    {
        delay(500);
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED)
    {
        Serial.println("\nWiFi Connected!");
        Serial.print("IP: ");
        Serial.println(WiFi.localIP());

        Blynk.connect(3000);

        Serial.println(Blynk.connected() ? "Blynk Connected!" : "Blynk offline");
    }
    else
    {
        Serial.println("\nWiFi FAILED — running offline");
    }
}

BLYNK_CONNECTED()
{
    // v1 synced V5 from the server here but had no handler for it. With the
    // handler below, a sync would restart a discharge after every reboot or
    // reconnect if the switch was left on, so push the device's state instead.
    Blynk.virtualWrite(VPIN_DISCHARGE, discharging ? 1 : 0);
}

BLYNK_WRITE(VPIN_DISCHARGE)
{
    if (param.asInt())
        startDischarge();
    else
        stopDischarge();
}

void pushTelemetry(float voltage, float current_mA, float tempC)
{
    const bmsml::Output &o = ml.out;
    Blynk.virtualWrite(VPIN_VOLTAGE, voltage);
    Blynk.virtualWrite(VPIN_CURRENT, current_mA);
    Blynk.virtualWrite(VPIN_TEMP, tempC);
    Blynk.virtualWrite(VPIN_SOC, o.socFused);
    Blynk.virtualWrite(VPIN_SOH, sohPercent);
    Blynk.virtualWrite(VPIN_SOC_V1, o.socV1);
    if (o.mlReady)
        Blynk.virtualWrite(VPIN_ANOMALY_SCORE, o.anomalyScore);
    Blynk.virtualWrite(VPIN_ANOMALY, (o.anomaly || mlWarningLatched) ? 1 : 0);
    Blynk.virtualWrite(VPIN_TIME_TO_EMPTY, timeToEmptyMin);
}

// ── Setup ────────────────────────────────────
void setup()
{
    Serial.begin(115200);
    delay(500);

    Serial.println("\n=== Smart BMS v2 (ML) Starting ===");

    Wire.begin(21, 22);

    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, HIGH); // safe/idle: MOSFET active-low, HIGH keeps it off

    if (!ina219.begin())
    {
        Serial.println("INA219 ERROR");
        while (1)
            ;
    }

    if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C))
    {
        Serial.println("OLED ERROR");
        while (1)
            ;
    }

    // Non-blocking temperature conversions: v1 waited ~750 ms inside every
    // sample for the 12-bit conversion, stalling loop() (serial, Blynk) most
    // of the time. Now each sample reads the conversion started one sample
    // earlier and kicks off the next. The first one starts here, well over
    // 750 ms before the first timer fire.
    sensors.begin();
    sensors.setWaitForConversion(false);
    sensors.requestTemperatures();

    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    prefs.begin("bms", false);
    sohPercent = prefs.getFloat("soh", 100.0);
    ml.ratedCapacity_mAh = RATED_CAPACITY;
    ml.usableCapacity_mAh = RATED_CAPACITY * sohPercent / 100.0;
    Serial.printf("SoH %.1f%% (usable capacity %.0f mAh)\n", sohPercent, ml.usableCapacity_mAh);

    // Hardware timer: fires every SAMPLE_INTERVAL ms, signals loop() via semaphore
    sampleTimerSemaphore = xSemaphoreCreateBinary();
    sampleTimer = timerBegin(1000000); // 1 MHz -> 1 tick = 1 microsecond
    timerAttachInterrupt(sampleTimer, &onSampleTimer);
    timerAlarm(sampleTimer, (uint64_t)SAMPLE_INTERVAL * 1000, true, 0);

    connectWiFiAndBlynk();

    Serial.println("Smart BMS Ready. Commands: D S C J L(og) M(L status)");
}

// ── Loop ─────────────────────────────────────
void loop()
{

    // WiFi reconnect
    static unsigned long lastReconnect = 0;
    static unsigned long lastBlynkAttempt = 0;

    if (WiFi.status() != WL_CONNECTED)
    {
        if (millis() - lastReconnect > 5000)
        {
            WiFi.begin(ssid, pass);
            lastReconnect = millis();
        }
    }
    else
    {
        // Blynk.connect() blocks for up to its timeout. v1 retried it on every
        // loop pass, so an unreachable server stalled sampling and serial; now
        // it is retried every 10 s.
        if (!Blynk.connected() && millis() - lastBlynkAttempt > 10000)
        {
            lastBlynkAttempt = millis();
            Blynk.connect(1000);
        }
        Blynk.run();
    }

    // Serial control
    if (Serial.available())
    {
        char cmd = Serial.read();

        if (cmd == 'D' || cmd == 'd')
        {
            startDischarge();
        }
        else if (cmd == 'S' || cmd == 's')
        {
            stopDischarge();
        }
        else if (cmd == 'C' || cmd == 'c')
        {
            if (!faultLatched)
            {
                if (mlWarningLatched && !ml.out.anomaly)
                {
                    mlWarningLatched = false;
                    Serial.println("ML warning acknowledged.");
                }
                else
                {
                    Serial.println(mlWarningLatched ? "ML anomaly still active." : "No fault latched.");
                }
            }
            else
            {
                bool stillUnderVoltage = (lastVoltage < CUTOFF_VOLTAGE);
                bool stillOverCurrent = (abs(lastCurrent_mA) > MAX_CURRENT);
                bool stillOverTemp = (lastTempC > MAX_TEMP);
                bool stillSensorInvalid = !lastSampleSensorValid;
                bool stillMlAnomaly = faultMlAnomaly && ml.out.anomaly;

                if (stillUnderVoltage || stillOverCurrent || stillOverTemp || stillSensorInvalid || stillMlAnomaly)
                {
                    if (stillSensorInvalid)
                        Serial.println("Fault NOT cleared: sensor reading still invalid/disconnected.");
                    else if (stillMlAnomaly)
                        Serial.println("Fault NOT cleared: ML anomaly still active.");
                    else
                        Serial.println("Fault NOT cleared: condition still present.");
                }
                else
                {
                    faultLatched = false;
                    faultUnderVoltage = false;
                    faultOverCurrent = false;
                    faultOverTemp = false;
                    faultSensor = false;
                    faultMlAnomaly = false;
                    Serial.println("Fault cleared.");
                }
            }
        }
        else if (cmd == 'J' || cmd == 'j')
        {
            uint32_t isrCountSnapshot;
            portENTER_CRITICAL(&sampleTimerMux);
            isrCountSnapshot = sampleIsrCount;
            portEXIT_CRITICAL(&sampleTimerMux);

            Serial.print("isr_fires: ");
            Serial.print(isrCountSnapshot);
            Serial.print(" samples_processed: ");
            Serial.print(samplesProcessed);
            Serial.print(" dropped: ");
            Serial.print(isrCountSnapshot - samplesProcessed);
            Serial.print(" max_jitter_ms: ");
            Serial.println(maxJitterMs);
        }
        else if (cmd == 'L' || cmd == 'l')
        {
            // CSV log for training (ml/collect_data.py): one LOG line per valid sample.
            loggingEnabled = !loggingEnabled;
            if (loggingEnabled)
                Serial.println("LOGHDR,t_ms,voltage,current_mA,temp_c,load,charging_v1,soc_v1,soc_ml,soc_fused,anomaly_score,anomaly,ml_ready");
            else
                Serial.println("Logging off.");
        }
        else if (cmd == 'M' || cmd == 'm')
        {
            const bmsml::Output &o = ml.out;
            Serial.printf("ML ready: %s\n", o.mlReady ? "yes" : "no (window filling, or < 3 min since a load/charger switch)");
            if (o.mlReady)
                Serial.printf("  features: Iavg %.3f A, dV/dt %.2f mV/min, dT/dt %.3f C/min\n",
                              o.features.currentAvg_A, o.features.dVdt_mVmin, o.features.dTdt_Cmin);
            Serial.printf("  SoC: fused %.1f%%, MLP %.1f%%, v1 %.1f%%\n", o.socFused, o.socMl, o.socV1);
            Serial.printf("  anomaly score %.2f (threshold %.2f), flag %s, warning %s\n",
                          o.anomalyScore, bmsml_model::ANOMALY_THRESHOLD, o.anomaly ? "ANOMALY" : "normal",
                          mlWarningLatched ? "LATCHED" : "none");
            Serial.printf("  SoH %.1f%%, usable %.0f mAh, time to empty %.0f min\n",
                          sohPercent, ml.usableCapacity_mAh, timeToEmptyMin);
        }
    }

    // Sampling: gated by the timer ISR via semaphore, not millis() polling.
    // xSemaphoreTake with 0 timeout is non-blocking; if the timer hasn't
    // fired since the last check, loop() falls through immediately.
    if (xSemaphoreTake(sampleTimerSemaphore, 0) == pdTRUE)
    {
        unsigned long nowMillis = millis();
        if (lastFireMillis != 0)
        {
            long jitter = (long)(nowMillis - lastFireMillis) - SAMPLE_INTERVAL;
            long absJitter = (jitter < 0) ? -jitter : jitter;
            if (absJitter > maxJitterMs)
                maxJitterMs = absJitter;
            if (absJitter > 2)
            {
                Serial.print("sample jitter_ms: ");
                Serial.print(jitter);
                Serial.print(" max_ms: ");
                Serial.println(maxJitterMs);
            }
        }
        lastFireMillis = nowMillis;
        samplesProcessed++;

        float busVoltage = ina219.getBusVoltage_V();
        float shuntVoltage = ina219.getShuntVoltage_mV();
        float current_mA = ina219.getCurrent_mA();
        float batteryVoltage = busVoltage + (shuntVoltage / 1000.0);

        float tempC = sensors.getTempCByIndex(0); // conversion started last sample
        sensors.requestTemperatures();            // returns immediately (see setup)

        // Sensor validation: catches NaN / out-of-plausible-range INA219 reads
        // (e.g. disconnected sensor) and the DallasTemperature disconnect
        // sentinel, before any of these values reach fault comparisons or
        // SoC math. See isVoltageValid/isCurrentValid/isTempValid above.
        bool voltageValid = isVoltageValid(batteryVoltage);
        bool currentValid = isCurrentValid(current_mA);
        bool tempValid = isTempValid(tempC);
        bool sensorsValid = voltageValid && currentValid && tempValid;
        lastSampleSensorValid = sensorsValid;

        if (sensorsValid)
        {
            lastVoltage = batteryVoltage;
            lastCurrent_mA = current_mA;
            lastTempC = tempC;
        }
        // else: intentionally do NOT overwrite lastVoltage/lastCurrent_mA/lastTempC —
        // the 'C' command re-checks these against thresholds, and we don't want a
        // garbage reading to make a stale-but-safe-looking value pass that check.

        // Sensor fault: latches exactly like the threshold faults, but is kept
        // in a separate flag (faultSensor) since it means "reading not trustworthy",
        // not "a real physical limit was crossed".
        if (!sensorsValid && !faultLatched)
        {
            faultLatched = true;
            faultSensor = true;
            stopDischarge(); // force safe/idle state (active-low MOSFET)

            Serial.print("FAULT LATCHED: SENSOR INVALID (");
            if (!voltageValid)
                Serial.print(" voltage");
            if (!currentValid)
                Serial.print(" current");
            if (!tempValid)
                Serial.print(" temperature");
            Serial.println(" )");
        }

        // Safety threshold enforcement — only evaluated on a validated reading,
        // since comparing a NaN/garbage value against these thresholds would be
        // meaningless (and is now caught by the sensor-fault check above instead).
        if (!faultLatched && sensorsValid)
        {
            faultUnderVoltage = (batteryVoltage < CUTOFF_VOLTAGE);
            faultOverCurrent = (abs(current_mA) > MAX_CURRENT);
            faultOverTemp = (tempC > MAX_TEMP);

            if (faultUnderVoltage || faultOverCurrent || faultOverTemp)
            {
                if (faultUnderVoltage && discharging)
                    finishCapacityTest();

                faultLatched = true;
                stopDischarge(); // force safe/idle state (active-low MOSFET)

                Serial.print("FAULT LATCHED:");
                if (faultUnderVoltage)
                    Serial.print(" UNDERVOLTAGE");
                if (faultOverCurrent)
                    Serial.print(" OVERCURRENT");
                if (faultOverTemp)
                    Serial.print(" OVERTEMP");
                Serial.println();
            }
        }

        // ML engine, SoH and time-to-empty only run on a validated reading.
        // Invalid samples are skipped; the gap they leave makes the engine
        // restart its feature window on the next valid one.
        if (sensorsValid)
        {
            float dt_s = (lastValidSampleMillis == 0) ? SAMPLE_INTERVAL / 1000.0
                                                      : (nowMillis - lastValidSampleMillis) / 1000.0;
            lastValidSampleMillis = nowMillis;

            static bool anomalyWasActive = false;
            const bmsml::Output &o = ml.update(batteryVoltage, current_mA, tempC, discharging, dt_s);

            if (o.anomaly && !anomalyWasActive)
            {
                mlWarningLatched = true;
                Serial.printf("ML WARNING: unusual cell behaviour (score %.1f > %.1f). Check the cell, then send 'C'.\n",
                              o.anomalyScore, bmsml_model::ANOMALY_THRESHOLD);
            }
            else if (!o.anomaly && anomalyWasActive)
            {
                Serial.println("ML: anomaly score back to normal; warning stays latched until 'C'.");
            }
            anomalyWasActive = o.anomaly;

#if ML_ANOMALY_TRIPS_FAULT
            if (o.anomaly && !faultLatched)
            {
                faultLatched = true;
                faultMlAnomaly = true;
                stopDischarge();
                Serial.println("FAULT LATCHED: ML ANOMALY");
            }
#endif

            if (discharging && current_mA > 0)
                capacitymAh += current_mA * dt_s / 3600.0;

            float iAvg_mA = o.mlReady ? o.features.currentAvg_A * 1000.0 : current_mA;
            timeToEmptyMin = (discharging && iAvg_mA > 20)
                                 ? o.socFused / 100.0 * ml.usableCapacity_mAh / iAvg_mA * 60.0
                                 : -1;

            if (loggingEnabled)
                Serial.printf("LOG,%lu,%.4f,%.2f,%.4f,%d,%d,%.2f,%.2f,%.2f,%.3f,%d,%d\n",
                              nowMillis, batteryVoltage, current_mA, tempC, discharging, o.chargingV1,
                              o.socV1, o.mlReady ? o.socMl : -1.0f, o.socFused,
                              o.mlReady ? o.anomalyScore : -1.0f, o.anomaly, o.mlReady);

            if (Blynk.connected() && nowMillis - lastBlynkPush >= BLYNK_PUSH_INTERVAL)
            {
                lastBlynkPush = nowMillis;
                pushTelemetry(batteryVoltage, current_mA, tempC);
            }
        }

        // OLED Display (21 characters x 8 lines)
        display.clearDisplay();
        display.setCursor(0, 0);

        if (sensorsValid)
        {
            const bmsml::Output &o = ml.out;
            display.printf("%.3fV  %.1fmA\n", batteryVoltage, current_mA);
            display.printf("T:%.1fC  SoH:%.0f%%\n", tempC, sohPercent);
            display.printf("SoC:%.1f%% (v1 %.0f%%)\n", o.socFused, o.socV1);

            if (timeToEmptyMin >= 0)
                display.printf("Empty in %dh%02dm\n", (int)timeToEmptyMin / 60, (int)timeToEmptyMin % 60);
            else
                display.println(discharging ? "DISCHARGING" : (o.chargingV1 ? "CHARGING" : "IDLE"));

            if (o.anomaly)
                display.println("ML: ANOMALY!");
            else if (mlWarningLatched)
                display.println("ML: check cell (C)");
            else
                display.println(o.mlReady ? "ML: normal" : "ML: settling");
        }
        else
        {
            display.println("SENSOR FAULT");
            display.println("check wiring");
        }

        if (faultLatched)
        {
            display.println(faultSensor ? "FAULT: SENSOR" : (faultMlAnomaly ? "FAULT: ML ANOMALY" : "FAULT LATCHED"));
        }

        display.display();
    }
}
