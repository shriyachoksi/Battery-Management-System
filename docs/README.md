# Presentation & Bring-up Guides

Step-by-step guides to take the Smart BMS from "v1 running on the bench" to
"ML version demoed with a full dashboard." Work through them in order.

| # | Guide | What it gets you | Needs hardware? |
|---|---|---|---|
| 1 | [Flash the ML version](01_flash_ml_version.md) | The v2 (ML) firmware running on your existing board — no rewiring | Yes |
| 2 | [Build the Blynk dashboard](02_blynk_dashboard.md) | A dashboard that actually shows the ML outputs (anomaly, SoH, time-to-empty, v1-vs-ML SoC) | No (console only) |
| 3 | [Hardware reference](03_hardware.md) | Pin map, bill of materials, circuit description for slides/report | No |
| 4 | [Presentation kit](04_presentation.md) | Demo script, slide outline, talking points, Q&A prep | For the demo |
| 5 | [Real-data retrain](05_real_data.md) | Model numbers from a real cell instead of simulated (optional) | Yes, several hours |

## The one thing to know before you start

The ML outputs are **not instant**. On boot — and for ~3 minutes after every
load/charger switch — the firmware shows `ML: settling` and holds SoC with plain
Coulomb counting. The neural net and anomaly detector only run once the cell has
*settled*: a full 60-second feature window **and** 180 s since the last switch.

This matters most for the live demo (guide 4). Power the board on and leave it
idle for ~3–4 minutes **before** you present, so it reads `ML: normal` when the
audience is watching — not `ML: settling`.
