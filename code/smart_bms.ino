#include <Wire.h>
#include <Adafruit_INA219.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <BlynkSimpleEsp32.h>

// ── Blynk Config ─────────────────────────────
#define BLYNK_TEMPLATE_ID "TMPL3llXmDJqa"
#define BLYNK_TEMPLATE_NAME "Smart BMS"
#define BLYNK_AUTH_TOKEN "u8yVPxaX8OqLiDyXz0GQ6p6rFfS7U-RN"

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

#define SOC_ALPHA 0.5

// ── Objects ──────────────────────────────────
Adafruit_INA219 ina219;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ── WiFi Credentials ─────────────────────────
char auth[] = BLYNK_AUTH_TOKEN;
char ssid[] = "WIFI_NAME";     // WRITE
char pass[] = "WIFI_PASSWORD"; // WRITE

// ── Variables ────────────────────────────────
float capacitymAh = 0.0;
float soh = 100.0;
float soc = 50.0;
float socFiltered = 50.0;

bool discharging = false;
bool cycleComplete = false;
bool socInitialized = false;

unsigned long lastSample = 0;

// Charging detection
float prevVoltage = 0.0;
int risingCount = 0;

// ── OCV Table ────────────────────────────────
const float ocvTable[][2] = {
    {3.00, 0}, {3.20, 5}, {3.40, 15}, {3.60, 30}, {3.70, 45}, {3.80, 60}, {3.90, 75}, {4.00, 88}, {4.10, 95}, {4.20, 100}};
const int OCV_POINTS = 10;

// ── OCV → SoC Function ───────────────────────
float ocvToSoC(float voltage)
{
    if (voltage <= ocvTable[0][0])
        return ocvTable[0][1];
    if (voltage >= ocvTable[OCV_POINTS - 1][0])
        return ocvTable[OCV_POINTS - 1][1];

    for (int i = 0; i < OCV_POINTS - 1; i++)
    {
        if (voltage >= ocvTable[i][0] && voltage <= ocvTable[i + 1][0])
        {
            float slope = (ocvTable[i + 1][1] - ocvTable[i][1]) /
                          (ocvTable[i + 1][0] - ocvTable[i][0]);
            return ocvTable[i][1] + slope * (voltage - ocvTable[i][0]);
        }
    }
    return 50.0;
}

// ── WiFi + Blynk ─────────────────────────────
void connectWiFiAndBlynk()
{
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

        Blynk.config(auth);
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
    Blynk.syncVirtual(V5);
}

// ── Setup ────────────────────────────────────
void setup()
{
    Serial.begin(115200);
    delay(500);

    Serial.println("\n=== Smart BMS Starting ===");

    Wire.begin(21, 22);

    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, HIGH);

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

    sensors.begin();

    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);

    connectWiFiAndBlynk();

    Serial.println("Smart BMS Ready");
}

// ── Loop ─────────────────────────────────────
void loop()
{

    // WiFi reconnect
    static unsigned long lastReconnect = 0;

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
        if (!Blynk.connected())
        {
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
            discharging = true;
            capacitymAh = 0;
            risingCount = 0;
            digitalWrite(MOSFET_PIN, LOW);
        }
        else if (cmd == 'S' || cmd == 's')
        {
            discharging = false;
            digitalWrite(MOSFET_PIN, HIGH);
        }
    }

    // Sampling
    if (millis() - lastSample >= SAMPLE_INTERVAL)
    {
        lastSample = millis();

        float busVoltage = ina219.getBusVoltage_V();
        float shuntVoltage = ina219.getShuntVoltage_mV();
        float current_mA = ina219.getCurrent_mA();
        float batteryVoltage = busVoltage + (shuntVoltage / 1000.0);

        sensors.requestTemperatures();
        float tempC = sensors.getTempCByIndex(0);

        // Charging detection
        bool isCharging = false;

        if (!discharging && prevVoltage > 0.0)
        {
            float delta = batteryVoltage - prevVoltage;

            if (delta > 0.003)
                risingCount++;
            else if (delta < -0.003)
                risingCount--;

            risingCount = constrain(risingCount, 0, 20);
            isCharging = (risingCount >= 5) && (batteryVoltage < 4.18);
        }

        prevVoltage = batteryVoltage;

        // Initialize SoC
        if (!socInitialized)
        {
            soc = ocvToSoC(batteryVoltage);
            socFiltered = soc;
            socInitialized = true;
        }

        float dt_hours = SAMPLE_INTERVAL / 3600000.0;

        // SoC update
        if (discharging && current_mA > 5)
        {
            soc -= (current_mA * dt_hours / RATED_CAPACITY) * 100;
        }
        else if (isCharging)
        {
            soc = 0.98 * soc + 0.02 * ocvToSoC(batteryVoltage);
        }
        else
        {
            soc = 0.97 * soc + 0.03 * ocvToSoC(batteryVoltage);
        }

        soc = constrain(soc, 0, 100);
        socFiltered = (1 - SOC_ALPHA) * socFiltered + SOC_ALPHA * soc;

        // OLED Display
        display.clearDisplay();
        display.setCursor(0, 0);
        display.print("Voltage: ");
        display.println(batteryVoltage);

        display.print("Current: ");
        display.println(current_mA);

        display.print("Temp: ");
        display.println(tempC);

        display.print("SoC: ");
        display.println(socFiltered);

        display.display();
    }
}