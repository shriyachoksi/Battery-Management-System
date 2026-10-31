# 2. Build the Blynk dashboard (with the ML outputs)

**The problem this fixes:** your current dashboard (`images/blynk_dashboard.png`)
has 6 widgets — Voltage, Current, Temp, SOC, SOH, Discharge. But the v2 firmware
sends **10** values. Four ML outputs have nowhere to show up:

- **V6** — SoC from the v1 estimator (for the *v1 vs ML* comparison, your headline result)
- **V7** — anomaly score
- **V8** — anomaly warning (0/1)
- **V9** — time to empty

If you present the ML version on the old dashboard, **none of the ML work is
visible.** The firmware already pushes all 10 pins (every 5 s) — you just need to
add the datastreams and widgets in the Blynk console.

---

## Step 1 — Datastreams (Blynk.Console → your Template → Datastreams)

Create one **Virtual Pin** datastream per row. Match pin numbers exactly — they're
hard-coded in the sketch (`VPIN_*` defines, lines 51–60).

| Pin | Name | Type | Units | Min | Max | Decimals | Notes |
|---|---|---|---|---|---|---|---|
| V0 | Voltage | Double | V | 2.5 | 4.3 | 2 (0.00) | |
| V1 | Current | Double | mA | -500 | 2500 | 1 (0.0) | +ve = discharge |
| V2 | Temperature | Double | °C | 0 | 60 | 1 (0.0) | |
| V3 | SoC (ML) | Double | % | 0 | 100 | 1 (0.0) | the fused estimate |
| V4 | SoH | Double | % | 0 | 100 | 0 (#) | |
| V5 | Discharge | Integer | — | 0 | 1 | 0 | **switch** (read+write) |
| V6 | SoC (v1) | Double | % | 0 | 100 | 1 (0.0) | for comparison |
| V7 | Anomaly score | Double | — | 0 | 60 | 1 (0.0) | threshold ≈ 32.7 |
| V8 | Anomaly warning | Integer | — | 0 | 1 | 0 | 1 = check cell |
| V9 | Time to empty | Integer | min | -1 | 600 | 0 | -1 when not discharging |

> V5 must be read **and** write (it's a two-way switch: the app starts/stops a
> discharge, and the firmware reports the current state back).

---

## Step 2 — Widgets (Web Dashboard + Mobile)

Lay it out in three bands so the story reads top-to-bottom: **live sensors →
battery state → ML intelligence.**

**Band 1 — Live sensor readings**
| Widget | Datastream | Settings |
|---|---|---|
| Gauge | V0 Voltage | 2.5–4.3 V, color by range |
| Gauge | V1 Current | -500–2500 mA |
| Gauge | V2 Temperature | 0–60 °C, red zone ≥ 50 |

**Band 2 — Battery state**
| Widget | Datastream | Settings |
|---|---|---|
| Gauge / Radial | V3 SoC (ML) | 0–100 %, this is the number people watch |
| Gauge | V4 SoH | 0–100 % |
| Label / Value | V9 Time to empty | show "min"; hide or show "—" when -1 |
| Switch | V5 Discharge | ON/OFF |

**Band 3 — ML intelligence (the part that makes this project special)**
| Widget | Datastream | Settings |
|---|---|---|
| **Chart** (2 series) | V3 **and** V6 | ML SoC vs v1 SoC on one axis — *this is your 6.0%→2.3% story, live* |
| LED / Value | V8 Anomaly warning | green at 0, red at 1; label "Cell health" |
| Chart or Gauge | V7 Anomaly score | add a reference line at ~32.7 (the threshold) |

**Why the V3-vs-V6 chart is the most important widget:** it shows the ML estimate
and the old rule-based estimate on the same graph at the same time. When they
diverge (especially during charging, where v1 is weakest), the audience *sees* the
ML doing something, instead of taking your word for it.

---

## Step 3 — Verify end-to-end

1. Board flashed with v2 and online (guide 1).
2. Within ~5 s, every gauge should populate.
3. Flip the V5 switch → OLED shows `DISCHARGING`, V1 current rises, V9 starts
   showing minutes. Flip off → V9 returns to -1.
4. Leave it idle 3 min → V3 and V6 should read *slightly different* values (ML
   correcting v1). If they're identical forever, the ML isn't ready yet (`M` on
   serial to confirm).

---

## Step 4 — Retake the dashboard screenshot

Once all 10 widgets are live, take a fresh screenshot to replace
`images/blynk_dashboard.png`. The current one predates the ML work and undersells
the project. Aim to capture a moment where:
- the V3-vs-V6 chart shows visible separation, and
- (if you can stage it safely later, after a real-data retrain) the anomaly LED is red.

---

## Notes

- Pin meanings also live in the README table ("Blynk virtual pins (v2)").
- If your Blynk template already uses different pin numbers, change the `VPIN_*`
  defines in the sketch instead of renumbering every datastream.
- Free Blynk limits the number of datastreams/widgets per template; 10 datastreams
  is within the standard free tier at time of writing, but if you hit a cap, drop
  V7 (anomaly *score*) first and keep V8 (the warning) — the warning is what
  matters for the demo.

Next: [guide 3 — hardware reference](03_hardware.md).
