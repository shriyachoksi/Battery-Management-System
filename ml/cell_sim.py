"""Synthetic single-cell Li-ion data for bootstrapping the BMS models.

Models the bench setup in this repo - one 18650 cell, a resistive load switched
by the MOSFET, a TP4056-style CC/CV charger, an INA219 and a DS18B20 - with a
first-order Thevenin equivalent circuit and a lumped thermal model. Sensor
quantisation (4 mV INA219 bus LSB, 0.0625 C DS18B20 LSB) and the firmware's
undervoltage/overtemperature cut-offs are applied, so the data looks like what
the ESP32 logs.

Each simulated cell gets its own capacity, resistance, OCV curve offset,
thermal constants and sensor offsets. Half the cells see their charge current
through the INA219 and half do not, since that depends on wiring.

Models trained on this data are a starting point. Retrain on logs from your
own cell (collect_data.py) before relying on them.
"""

from __future__ import annotations

import math
import random
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

# Typical NMC/graphite 18650 open-circuit voltage at 25 C (SoC %, V). Deliberately
# not the sketch's OCV table: a real cell never matches a generic table exactly.
OCV_SOC = np.array([0, 2, 5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100], float)
OCV_V = np.array([3.00, 3.27, 3.42, 3.51, 3.56, 3.60, 3.65, 3.69, 3.73, 3.79,
                  3.87, 3.95, 4.05, 4.11, 4.19])
_OCV_FINE = np.interp(np.linspace(0, 100, 1001), OCV_SOC, OCV_V).tolist()  # 0.1 % grid

CUTOFF_V = 3.0      # firmware CUTOFF_VOLTAGE: latches UNDERVOLTAGE and opens the MOSFET
MAX_TEMP_C = 50.0   # firmware MAX_TEMP: latches OVERTEMP and opens the MOSFET
CV_VOLTAGE = 4.2    # TP4056 constant-voltage setpoint

ANOMALY_KINDS = ("internal_short", "soft_short", "external_heat")


def random_cell(rng):
    return {
        "capacity_mAh": rng.uniform(2150, 2700),
        "r0": rng.uniform(0.045, 0.11),        # ohmic resistance at 25 C, ohm
        "r1": rng.uniform(0.015, 0.045),       # polarisation resistance, ohm
        "tau_s": rng.uniform(20, 70),          # polarisation time constant, s
        "ocv_offset": rng.normal(0, 0.006),    # V
        "ocv_gain": rng.normal(0, 0.006),      # V across the SoC range
        "c_th": rng.uniform(38, 50),           # heat capacity, J/K
        "r_th": rng.uniform(12, 25),           # thermal resistance to ambient, K/W
        "t_amb": rng.uniform(18, 36),          # C
        "r_wiring": rng.uniform(0.15, 0.30),   # MOSFET Rds(on) + shunt + wiring, ohm
        "v_gain_err": rng.normal(0, 0.002),    # INA219 voltage gain error
        "i_offset_mA": rng.normal(0, 0.3),     # INA219 current offset
        "t_offset": rng.normal(0, 0.25),       # DS18B20 offset, C
        "charge_visible": bool(rng.random() < 0.5),
    }


def random_phases(rng):
    """A day-in-the-life schedule: rests, discharges and charges in some order."""
    phases = [{"kind": "rest", "max_s": rng.uniform(5, 30) * 60}]
    kind = "discharge" if rng.random() < 0.6 else "charge"
    for _ in range(int(rng.integers(3, 6))):
        if kind == "discharge":
            # 60 %: run to the undervoltage cut-off; otherwise stopped early ('S').
            stop_soc = None if rng.random() < 0.6 else rng.uniform(5, 70)
            phases.append({"kind": "discharge", "r_load": rng.uniform(4, 25),
                           "stop_soc": stop_soc, "max_s": 8 * 3600})
            kind = "charge"
        else:
            # 70 %: charge until the TP4056 terminates; otherwise unplugged early.
            max_s = 4 * 3600 if rng.random() < 0.7 else rng.uniform(15, 90) * 60
            phases.append({"kind": "charge", "i_chg": rng.uniform(0.4, 1.0), "max_s": max_s})
            kind = "discharge"
        phases.append({"kind": "rest", "max_s": rng.uniform(5, 60) * 60})
    return phases


def normal_scenario(rng, split, name):
    return {"name": name, "split": split, "cell": random_cell(rng),
            "start_soc": rng.uniform(15, 100), "phases": random_phases(rng),
            "anomaly": None, "seed": int(rng.integers(2**31))}


def anomaly_scenario(rng, kind, name):
    """A normal schedule with one fault injected 30-150 min in, run 45 min past onset."""
    spec = normal_scenario(rng, "anomaly", name)
    onset = rng.uniform(30, 150) * 60
    if kind == "internal_short":
        anomaly = {"r_short": rng.uniform(4, 15)}      # ~0.25-0.9 A hidden drain
    elif kind == "soft_short":
        anomaly = {"r_short": rng.uniform(25, 60)}     # ~60-150 mA hidden drain
    else:
        rate = rng.uniform(1.0, 3.0)                   # C/min, e.g. a heat source nearby
        anomaly = {"rate_c_min": rate, "duration_s": min(rng.uniform(8, 20), 30 / rate) * 60}
    anomaly.update(kind=kind, onset_s=onset)
    spec["anomaly"] = anomaly
    spec["phases"].append({"kind": "rest", "max_s": 24 * 3600})
    spec["max_total_s"] = onset + 45 * 60
    return spec


def simulate(spec):
    """Run one scenario at 1 Hz and return the sensor log as a DataFrame."""
    cell = spec["cell"]
    anomaly = spec["anomaly"]
    rnd = random.Random(spec["seed"])
    max_total = spec.get("max_total_s", math.inf)

    q_mAh = cell["capacity_mAh"]
    r0_25, r1, tau = cell["r0"], cell["r1"], cell["tau_s"]
    c_th, r_th = cell["c_th"], cell["r_th"]
    ocv_off, ocv_gain = cell["ocv_offset"], cell["ocv_gain"]
    t_off = cell["t_offset"]
    fine = _OCV_FINE

    soc = spec["start_soc"]
    v1 = 0.0
    t_amb0 = t_amb = cell["t_amb"]
    temp = t_amb
    tripped = False  # firmware OVERTEMP latch: blocks the load from then on

    rec_v, rec_i, rec_t, rec_load, rec_chg, rec_soc, rec_anom = [], [], [], [], [], [], []
    t = 0
    for ph in spec["phases"]:
        kind = ph["kind"]
        steps = 0
        while steps < ph["max_s"] and t < max_total:
            g_sh = 0.0
            anom = anomaly is not None and t >= anomaly["onset_s"]
            if anom:
                if "r_short" in anomaly:
                    g_sh = 1.0 / anomaly["r_short"]
                elif t < anomaly["onset_s"] + anomaly["duration_s"]:
                    t_amb += anomaly["rate_c_min"] / 60.0
            t_amb += (t_amb0 - t_amb) / 7200.0 + rnd.gauss(0.0, 0.005)

            x = min(max(soc, 0.0), 100.0) * 10.0
            i0 = min(int(x), 999)
            ocv = fine[i0] + (x - i0) * (fine[i0 + 1] - fine[i0]) + ocv_off + ocv_gain * (soc / 100.0 - 0.5)
            r0 = r0_25 * math.exp(2500.0 * (1.0 / (temp + 273.15) - 1.0 / 298.15)) * (1.0 + 0.6 * math.exp(-soc / 8.0))

            load = charging = False
            if kind == "discharge":
                stop = ph["stop_soc"]
                if tripped or (stop is not None and soc <= stop):
                    break
                r_ext = ph["r_load"] + cell["r_wiring"]
                v = (ocv - v1) / (1.0 + r0 / r_ext + r0 * g_sh)
                if v < CUTOFF_V:
                    break
                i_ext = v / r_ext
                i_vis = i_ext
                load = True
            elif kind == "charge":
                i_chg = ph["i_chg"]
                v = (ocv - v1 + r0 * i_chg) / (1.0 + r0 * g_sh)
                i_ext = -i_chg
                if v >= CV_VOLTAGE:
                    v = CV_VOLTAGE
                    i_ext = (ocv - v1 - v) / r0 - v * g_sh
                    if -i_ext < i_chg / 10.0:
                        break  # TP4056 terminates at 1/10 of the programmed current
                i_vis = i_ext if cell["charge_visible"] else 0.0
                charging = True
            else:
                v = (ocv - v1) / (1.0 + r0 * g_sh)
                i_ext = i_vis = 0.0

            rec_v.append(v)
            rec_i.append(i_vis)
            rec_t.append(temp)
            rec_load.append(load)
            rec_chg.append(charging)
            rec_soc.append(soc)
            rec_anom.append(anom)

            i_cell = i_ext + v * g_sh
            soc = min(max(soc - i_cell * 1000.0 / 3600.0 / q_mAh * 100.0, 0.0), 100.0)
            v1 += (i_cell * r1 - v1) / tau
            heat = i_cell * i_cell * r0 + v1 * v1 / r1 + v * v * g_sh
            temp += (heat - (temp - t_amb) / r_th) / c_th
            if temp + t_off > MAX_TEMP_C:
                tripped = True
            t += 1
            steps += 1
        if t >= max_total:
            break

    n = len(rec_v)
    nrng = np.random.default_rng(spec["seed"] + 1)
    v_meas = np.array(rec_v) * (1.0 + cell["v_gain_err"]) + nrng.normal(0, 0.001, n)
    i_meas = np.array(rec_i) * 1000.0 + cell["i_offset_mA"] + nrng.normal(0, 0.3, n)
    t_meas = np.array(rec_t) + t_off + nrng.normal(0, 0.01, n)
    return pd.DataFrame({
        "run": spec["name"],
        "t_s": np.arange(n, dtype=float),
        "voltage": np.round(np.round(v_meas / 0.004) * 0.004, 4),
        "current_mA": np.round(i_meas, 1),
        "temp_c": np.round(t_meas / 0.0625) * 0.0625,
        "load": np.array(rec_load),
        "charging": np.array(rec_chg),
        "soc_true": np.array(rec_soc),
        "anomaly_true": np.array(rec_anom),
    })


def simulate_many(specs, jobs=1):
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as ex:
            return list(ex.map(simulate, specs, chunksize=1))
    return [simulate(s) for s in specs]
