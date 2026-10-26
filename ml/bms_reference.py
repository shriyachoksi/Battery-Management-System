"""Python mirror of the firmware's ML engine (code/smart_bms_ml/bms_ml.h).

Training and evaluation run the models through this module, so what is
measured on the PC is what the ESP32 does. tests/run_parity_test.py compiles
bms_ml.h on the host and checks the two implementations agree sample by
sample - keep them in step when changing either.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Firmware constants ───────────────────────────────────────────
# train_models.py writes these into ml_model.h, so Python and C share them.
SAMPLE_PERIOD_MS = 1000   # must equal SAMPLE_INTERVAL in the sketch
FEATURE_WINDOW = 60       # samples in the feature window (60 s)
SETTLE_SAMPLES = 180      # step-free samples needed after a load/charger switch (~3 polarisation time constants)
STEP_VOLTAGE_V = 0.015    # sample-to-sample jump that marks a load/charger switch, V
STEP_CURRENT_MA = 50.0    # same for current, mA
GAP_FACTOR = 1.5          # dt above this many periods means samples were lost
MAX_DT_S = 5.0            # cap on the Coulomb-counting dt after a gap
CC_DEADBAND_MA = 5.0      # |current| below this is treated as zero by Coulomb counting
REST_CURRENT_MA = 20.0    # window-average |current| below this counts as resting
ANOMALY_PERSIST = 10      # consecutive over-threshold samples that raise the flag
ANOMALY_CLEAR = 30        # consecutive under-threshold samples that clear it
V1_SOC_ALPHA = 0.5        # SOC_ALPHA from the original sketch

# Evaluation only: the sketch's own RATED_CAPACITY is what the device uses.
RATED_CAPACITY_MAH = 2500.0

SOC_FEATURES = ["voltage", "current_A", "temp_C", "currentAvg_A", "dVdt_mVmin"]

# Anomaly features and the unit each is divided by before the GMM sees it.
# The units double as noise floors: the GMM covariance is regularised by one
# unit^2, so the model can never get sharper than this resolution.
GMM_FEATURES = ["currentAvg_A", "dVdt_mVmin", "dTdt_Cmin", "soc_ml"]
GMM_SCALE = np.array([0.05, 2.0, 0.1, 10.0])

# OCV table from the original (v1) sketch.
OCV_TABLE = np.array([[3.00, 0], [3.20, 5], [3.40, 15], [3.60, 30], [3.70, 45],
                      [3.80, 60], [3.90, 75], [4.00, 88], [4.10, 95], [4.20, 100]])


def ocv_to_soc(voltage):
    """v1 OCV lookup: linear interpolation, clamped at both ends."""
    return np.interp(voltage, OCV_TABLE[:, 0], OCV_TABLE[:, 1])


def sample_dt(t_s):
    """Seconds since the previous sample; the first sample gets one period."""
    t_s = np.asarray(t_s, float)
    return np.diff(t_s, prepend=t_s[0] - SAMPLE_PERIOD_MS / 1000.0)


def segment_ids(t_s):
    """Label contiguous runs. A gap restarts the firmware's feature window."""
    gap = sample_dt(t_s) > GAP_FACTOR * SAMPLE_PERIOD_MS / 1000.0
    return np.cumsum(gap)


def window_features(voltage, current_mA, temp_c, seg):
    """Window features for every sample, and whether the window is settled.

    Settled means the window is full and SETTLE_SAMPLES contiguous samples have
    passed since the last load/charger step, so the cell's polarisation has
    mostly relaxed; only settled samples are used by the models.
    """
    n = len(voltage)
    feats = {
        "voltage": np.asarray(voltage, float),
        "current_A": np.asarray(current_mA, float) / 1000.0,
        "temp_C": np.asarray(temp_c, float),
        "currentAvg_A": np.full(n, np.nan),
        "dVdt_mVmin": np.full(n, np.nan),
        "dTdt_Cmin": np.full(n, np.nan),
    }
    settled = np.zeros(n, bool)

    w = FEATURE_WINDOW
    x = np.arange(w) - (w - 1) / 2.0
    sxx = float(x @ x)
    per_min = 60000.0 / SAMPLE_PERIOD_MS

    starts = np.flatnonzero(np.diff(seg, prepend=seg[0] - 1))
    ends = np.append(starts[1:], n)
    for a, b in zip(starts, ends):
        m = b - a
        if m < w:
            continue
        v = feats["voltage"][a:b]
        i = np.asarray(current_mA[a:b], float)
        t = feats["temp_C"][a:b]
        k = np.arange(a + w - 1, b)

        feats["dVdt_mVmin"][k] = np.correlate(v, x, "valid") / sxx * 1000.0 * per_min
        feats["dTdt_Cmin"][k] = np.correlate(t, x, "valid") / sxx * per_min
        cs = np.concatenate([[0.0], np.cumsum(i)])
        feats["currentAvg_A"][k] = (cs[w:] - cs[:-w]) / w / 1000.0

        # Step test in float32, exactly as the firmware does it.
        v32, i32 = v.astype(np.float32), i.astype(np.float32)
        step = np.zeros(m, bool)
        step[1:] = ((np.abs(v32[1:] - v32[:-1]) > np.float32(STEP_VOLTAGE_V))
                    | (np.abs(i32[1:] - i32[:-1]) > np.float32(STEP_CURRENT_MA)))
        csum = np.cumsum(step)
        kk = np.arange(max(w - 1, SETTLE_SAMPLES), m)
        settled[a + kk] = (csum[kk] - csum[kk - SETTLE_SAMPLES]) == 0
    return feats, settled


# ── Models ───────────────────────────────────────────────────────

def mlp_soc(X, p):
    """SoC MLP forward pass (5 -> tanh -> tanh -> 1), output in %."""
    z = (X - p["x_mean"]) / p["x_std"]
    h = np.tanh(z @ p["W1"].T + p["b1"])
    h = np.tanh(h @ p["W2"].T + p["b2"])
    return np.clip((h @ p["W3"] + p["b3"]) * 100.0, 0.0, 100.0)


def gmm_features(feats, soc_ml):
    return np.column_stack([feats["currentAvg_A"], feats["dVdt_mVmin"],
                            feats["dTdt_Cmin"], soc_ml]) / GMM_SCALE


def gmm_score(G, p):
    """Anomaly score: negative log-likelihood under the GMM (higher = stranger)."""
    diff = G[:, None, :] - p["means"][None, :, :]
    q = np.einsum("nkd,kde,nke->nk", diff, p["prec"], diff)
    logp = p["lognorm"][None, :] - 0.5 * q
    top = logp.max(axis=1)
    return -(top + np.log(np.exp(logp - top[:, None]).sum(axis=1)))


# ── Stateful parts of the engine ─────────────────────────────────

def v1_sequence(voltage, current_mA, load, dt_s, rated_mAh=RATED_CAPACITY_MAH):
    """The original sketch's SoC estimator (OCV table + Coulomb counting)."""
    n = len(voltage)
    out = np.empty(n)
    charging_out = np.zeros(n, bool)
    ocv = ocv_to_soc(voltage)
    soc = soc_f = 0.0
    init = False
    prev_v = 0.0
    rising = 0
    prev_load = False
    for k in range(n):
        v, i, on = voltage[k], current_mA[k], bool(load[k])
        if on and not prev_load:
            rising = 0  # the 'D' command reset this in the original sketch
        prev_load = on

        charging = False
        if not on and prev_v > 0.0:
            delta = v - prev_v
            if delta > 0.003:
                rising += 1
            elif delta < -0.003:
                rising -= 1
            rising = min(max(rising, 0), 20)
            charging = rising >= 5 and v < 4.18
        prev_v = v

        if not init:
            soc = soc_f = ocv[k]
            init = True
        if on and i > 5.0:
            soc -= i * (dt_s[k] / 3600.0) / rated_mAh * 100.0
        elif charging:
            soc = 0.98 * soc + 0.02 * ocv[k]
        else:
            soc = 0.97 * soc + 0.03 * ocv[k]
        soc = min(max(soc, 0.0), 100.0)
        soc_f = (1.0 - V1_SOC_ALPHA) * soc_f + V1_SOC_ALPHA * soc
        out[k] = soc_f
        charging_out[k] = charging
    return out, charging_out


def fusion_sequence(voltage, current_mA, dt_s, settled, soc_ml, current_avg_A,
                    gain_rest, gain_load, usable_mAh=RATED_CAPACITY_MAH):
    """Coulomb counting, pulled toward the MLP estimate whenever it is valid."""
    n = len(voltage)
    out = np.empty(n)
    soc = float(ocv_to_soc(voltage[0])) if n else 0.0
    for k in range(n):
        i = current_mA[k]
        if abs(i) > CC_DEADBAND_MA:
            soc -= i * (dt_s[k] / 3600.0) / usable_mAh * 100.0
        if settled[k]:
            gain = gain_rest if abs(current_avg_A[k]) * 1000.0 < REST_CURRENT_MA else gain_load
            soc += gain * (soc_ml[k] - soc)
        soc = min(max(soc, 0.0), 100.0)
        out[k] = soc
    return out


def detector_sequence(settled, score, threshold):
    """Debounce the anomaly score into a flag (raise after PERSIST, clear after CLEAR)."""
    n = len(score)
    out = np.zeros(n, bool)
    above = below = 0
    active = False
    for k in range(n):
        if not settled[k]:
            above = 0
        elif score[k] > threshold:
            above += 1
            below = 0
            if above >= ANOMALY_PERSIST:
                active = True
        else:
            below += 1
            above = 0
            if below >= ANOMALY_CLEAR:
                active = False
        out[k] = active
    return out


# ── Whole engine ─────────────────────────────────────────────────

def stateless_outputs(run, model):
    """Features, MLP SoC and anomaly score for one run (NaN where not settled)."""
    voltage = run["voltage"].to_numpy(float)
    current = run["current_mA"].to_numpy(float)
    feats, settled = window_features(voltage, current, run["temp_c"].to_numpy(float),
                                     segment_ids(run["t_s"].to_numpy(float)))
    soc_ml = np.full(len(run), np.nan)
    score = np.full(len(run), np.nan)
    if settled.any():
        X = np.column_stack([feats[k][settled] for k in SOC_FEATURES])
        soc_ml[settled] = mlp_soc(X, model["soc"])
        if "gmm" in model:
            sub = {k: feats[k][settled] for k in feats}
            score[settled] = gmm_score(gmm_features(sub, soc_ml[settled]), model["gmm"])
    return feats, settled, soc_ml, score


def run_engine(run, model, rated_mAh=RATED_CAPACITY_MAH, usable_mAh=RATED_CAPACITY_MAH):
    """Everything bmsml::Engine::update() produces, for every sample of one run."""
    feats, settled, soc_ml, score = stateless_outputs(run, model)
    voltage = run["voltage"].to_numpy(float)
    current = run["current_mA"].to_numpy(float)
    load = run["load"].to_numpy(bool)
    dt = np.minimum(sample_dt(run["t_s"].to_numpy(float)), MAX_DT_S)

    soc_v1, charging_v1 = v1_sequence(voltage, current, load, dt, rated_mAh)
    soc_fused = fusion_sequence(voltage, current, dt, settled, soc_ml, feats["currentAvg_A"],
                                model["gain_rest"], model["gain_load"], usable_mAh)
    anomaly = detector_sequence(settled, score, model["threshold"])
    return pd.DataFrame({
        "ml_ready": settled,
        "currentAvg_A": feats["currentAvg_A"],
        "dVdt_mVmin": feats["dVdt_mVmin"],
        "dTdt_Cmin": feats["dTdt_Cmin"],
        "soc_ml": soc_ml,
        "anomaly_score": score,
        "anomaly": anomaly,
        "soc_v1": soc_v1,
        "charging_v1": charging_v1,
        "soc_fused": soc_fused,
    }, index=run.index)
