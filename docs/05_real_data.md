# 5. Retrain on real-cell data (optional, but it makes the results bulletproof)

**Why bother:** right now `ml/reports/metrics.json` shows `real_logs: []` — the
shipped model is trained purely on a (calibrated) simulator. The results are real
and honestly reported, but the *strongest* possible answer to "did you test it on
a real battery?" is "yes, and here are the numbers from our cell." This guide gets
you there.

**The catch — timeline:** this needs real charge/discharge cycles, which take
**hours**, not minutes. A full discharge of an 18650 at a safe current is itself
1–several hours, plus a 10-minute rest and recharge between cycles. Budget a day
or two of mostly-unattended cycling. If your presentation is tomorrow, skip this
and present the simulated results honestly (guide 4 shows how to frame it).

---

## What good training data looks like

The trainer gets its SoC *labels* by integrating current over a **full discharge**
(100% → cutoff). So each useful cycle is:

1. **Charge** the cell until the TP4056 LED says done (full).
2. **Rest** at least 10 minutes (let the voltage relax).
3. **Discharge** with logging on, until the firmware's 3.0 V undervoltage cutoff
   stops it on its own.

A few such cycles are enough. If you can run them at **different room
temperatures**, even better — it teaches the model temperature dependence.
Logging normal idle/charge time too improves the *anomaly* model (it learns what
normal looks like on your actual setup).

---

## Step-by-step

**1. Install the Python deps** (once):
```bash
pip install -r ml/requirements.txt
```

**2. Make sure v2 is flashed and the board is on the right COM port** (guide 1).
Close the Arduino Serial Monitor — only one program can hold the port.

**3. Charge fully, then rest 10 min.**

**4. Log a discharge cycle:**
```bash
python ml/collect_data.py COM5 --discharge
```
- Replace `COM5` with your port.
- `--discharge` sends `D` automatically once logging starts.
- It writes `ml/data/real/<timestamp>.csv` and prints a progress line every 60
  samples. Leave it running until the undervoltage cutoff stops the discharge,
  then press **Ctrl+C**.

**5. Repeat** for a few cycles (recharge, rest, log again). Each run makes its own
timestamped CSV in `ml/data/real/`.

**6. Retrain** — the trainer automatically picks up everything in `ml/data/real/`
and weights real samples about equally with the simulated ones:
```bash
python ml/train_models.py            # ~4 min (--quick for a ~2 min smoke test)
```
This regenerates `code/smart_bms_ml/ml_model.h`, the `ml/reports/` plots and
`metrics.json` (now with your real logs counted).

**7. Check the firmware math still matches Python:**
```bash
python ml/tests/run_parity_test.py   # needs g++/clang++, or: pip install ziglang
```

**8. Reflash** `code/smart_bms_ml/smart_bms_ml.ino` with the new `ml_model.h`
baked in (guide 1).

**9. Update the presentation** — `metrics.json` and the plots now reflect your
cell. Re-grab the numbers for slide 8/9.

---

## If you also want to demo the anomaly detector live (advanced, careful)

Only after a real-data retrain, and only if you can do it **safely**:

- External heating is the safe one to stage — gently warm the cell (e.g. warm air,
  not a flame) and watch the anomaly score climb past ~32.7 and the warning latch.
- Do **not** deliberately short a cell. Use the simulator plots
  (`anomaly_detection.png`) to show internal-short detection instead.
- You can also flip `ML_ANOMALY_TRIPS_FAULT` to `1` (sketch line ~41) so an anomaly
  actually opens the MOSFET — but **only** after retraining on your cell, or you'll
  get nuisance trips.

---

That's the full set. Back to the [index](README.md).
