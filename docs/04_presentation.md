# 4. Presentation kit

Written to present with: a timed demo script, a slide-by-slide outline, talking
points, and prep for the questions you're most likely to get.

---

## A. The 90-second setup (do this BEFORE you present)

The single biggest way to flop this demo is to turn the board on in front of the
audience and have it say `ML: settling` for three minutes. Avoid it:

- [ ] **Charge the cell** to full the night before (so SoC starts high and you can
      show a discharge).
- [ ] **Power the board on ~4 minutes before** you talk, and leave it **idle**.
      By the time you present, the OLED reads `ML: normal` and the dashboard is live.
- [ ] Have the **dashboard open** on a phone/laptop already connected.
- [ ] Have `ml/reports/soc_tracking.png` and `anomaly_detection.png` open in a tab
      as backup if WiFi/Blynk misbehaves.
- [ ] Know your cell's capacity number (you set `RATED_CAPACITY`).

---

## B. Live demo script (~4–5 minutes)

> Narrate the *why*, not just the *what*. Timings assume the board is already
> settled (see above).

1. **Point at the OLED (15 s).** "This is running entirely on the ESP32 — voltage,
   current, temperature, state of charge, state of health, all on-device."

2. **Show the dashboard (20 s).** "The same data streams to the cloud over WiFi —
   this is the IoT side. Notice SoC here, and *this* chart" → point at the
   **V3-vs-V6 (ML vs v1) chart**.

3. **The headline — ML vs rule-based (40 s).** "The orange line is the old
   rule-based estimate; the green is our ML estimate. On a held-out test set the
   old method was off by 6% on average; ours is 2.3%. The gap is biggest while
   charging — 9% down to under 3% — because voltage alone lies during charging, and
   the network learned to correct for it." (Point at where the two lines diverge.)

4. **Start a discharge (30 s).** Flip the dashboard **Discharge** switch (or type
   `D`). "Now it's under load — current jumps, and here's **time-to-empty**
   predicted from the ML SoC and the real measured capacity." Point at V9 / the
   OLED `Empty in XhYYm`.
   > Mention: "The ML re-enters a 3-minute settling window after any load change —
   > that's deliberate, the cell needs to relax before the model trusts it. Coulomb
   > counting covers the gap." (Turning this limitation into a design choice is a
   > strong move.)

5. **Safety fault (30 s).** Briefly pull the temperature sensor (or talk through
   it). "Any sensor going invalid, or crossing a hard limit — under 3 V, over 2 A,
   over 50 °C — **latches a fault** and forces the load off. It won't re-enable
   until I clear it *and* the condition is gone." Replug, type `C`, "cleared."

6. **Anomaly detection — show, don't trigger (30 s).** Switch to
   `anomaly_detection.png`. "We also run an anomaly detector — a Gaussian mixture
   model that learns what *normal* looks like. In testing it caught internal shorts
   15 out of 15 times, a median of 11 minutes before the cell would've hit the
   50°C limit — with zero false alarms over 292 hours. It's a smoke detector for
   the battery." (Don't force a real fault live — it's slow and unsafe.)

7. **Close (15 s).** "So: a working single-cell BMS, with on-device ML that's
   measurably more accurate than the rule-based version, full IoT telemetry, and
   early-warning safety — all on a $5 microcontroller, no cloud inference."

---

## C. Slide outline (~11 slides)

1. **Title** — Smart Battery Management System (ML-enhanced). Names, one-line pitch.
2. **The problem** — Why BMS matters (safety, range anxiety, battery life); why SoC
   estimation is hard (voltage ≠ charge, especially under load/charging).
3. **System overview** — The block diagram from `docs/03_hardware.md`. Sensors →
   ESP32 → OLED + Blynk; MOSFET load control.
4. **Hardware** — Photo (`images/setup.jpg`) + the pin/BOM table. Keep it short.
5. **v1: the rule-based baseline** — OCV table + Coulomb counting + safety faults +
   timer-interrupt sampling. "Solid, but SoC drifts — 6% error, 9% while charging."
6. **v2: adding on-device ML** — Two models: an MLP that corrects SoC, a GMM that
   flags anomalies. Runs in plain C++, no TensorFlow, ~a few hundred µs/sample.
7. **How the ML SoC works** — 5 features (V, I, T, avg current, voltage slope) →
   small MLP → fused with Coulomb counting. One clear sentence per piece.
8. **Results: SoC** — The table (6.0→2.3% overall, 9.1→2.7% charging) +
   `soc_tracking.png`. **This is your strongest slide.**
9. **Results: anomaly detection** — 15/15 internal shorts, 11-min median lead, 0
   false alarms / 292 h + `anomaly_detection.png`.
10. **IoT dashboard** — The refreshed Blynk screenshot (after guide 2).
11. **Honest limitations + future work** — Trained on a calibrated simulator (real-
    data retrain is the next step); 3-min settling window; small drains missed.
    Future: multi-cell + balancing, real-cell retrain, CAN bus.

> Tip: put the demo between slide 10 and 11 (or right after slide 9), while
> attention is highest.

---

## D. Talking points (the narrative threads)

- **"We didn't just bolt on ML — we measured whether it helped."** The v1
  estimator still runs alongside v2 specifically so the comparison is honest and
  visible, not a marketing claim.
- **"It's a real embedded ML system, not a cloud call."** Inference is hand-written
  C++ running on the ESP32 itself; the parity test proves the on-device math
  matches the Python training exactly.
- **"Safety is layered and conservative."** Fixed thresholds always hold the
  MOSFET; the ML anomaly detector is *advisory* by default and never overrides them
  unless you explicitly enable and retrain it.
- **"We were careful about engineering, not just the model."** Timer-interrupt
  sampling (not blocking delays), non-blocking temperature reads, sensor
  validation, fault latching, capacity-based SoH saved to flash.

---

## E. Anticipated questions (and good answers)

| Question | Answer |
|---|---|
| "Did you test the ML on a *real* battery?" | "The shipped model is trained on a physics-based simulator calibrated to an 18650 on our bench; the firmware runs on real hardware now. Collecting real discharge logs and retraining is our documented next step [guide 5] — the pipeline for it is already built." |
| "Why ML instead of a Kalman filter?" | "Kalman needs an accurate cell model and tuned noise covariances; our MLP learns the correction directly from data, and it's strongest exactly where simple models are weakest — charging. We still fuse it with Coulomb counting, so it's not pure black box." |
| "Isn't a neural net overkill for an ESP32?" | "It's tiny — ~400 weights, a few hundred microseconds per 1-second sample. Memory and timing are a non-issue; we track sampling jitter to prove it." |
| "How do you know it's not overfitting?" | "All numbers are on held-out *cells* the model never saw — 12 test cells, separate from the 48 training cells." |
| "What if the ML is wrong / a sensor fails?" | "The fixed safety thresholds are independent of the ML and always control the MOSFET. A bad sensor reading latches a fault on its own. The ML can only advise unless we deliberately turn that off." |
| "Why the 3-minute delay before ML works?" | "Right after a load or charger switch the cell's voltage is still relaxing, so any model reading it would be misled. We wait for it to settle and let Coulomb counting carry SoC meanwhile — a deliberate accuracy/safety trade-off." |
| "What's the anomaly detector actually catching?" | "Unusual *changes* — abnormal heating or voltage drops, like an internal short developing — before a hard limit trips. It reacts to change, so we latch the warning until acknowledged in case the cell settles into a new bad steady state." |

Next (optional): [guide 5 — retrain on real data](05_real_data.md).
