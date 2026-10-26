"""Train the Smart BMS ML models and export them for the ESP32.

    python ml/train_models.py           # synthetic bootstrap + any logs in ml/data/real
    python ml/train_models.py --quick   # smaller dataset, for a fast smoke test

Writes
    code/smart_bms_ml/ml_model.h   weights and thresholds compiled into the firmware
    ml/reports/metrics.json        held-out test results (v1 estimator vs ML)
    ml/reports/*.png               SoC tracking and anomaly detection plots
    ml/build/model.pkl             the trained model, for analysing logs in Python
    ml/build/parity_*.csv          vectors for tests/run_parity_test.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.neural_network import MLPRegressor

import bms_reference as ref
import cell_sim

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "code" / "smart_bms_ml" / "ml_model.h"
BUILD = ROOT / "ml" / "build"
REPORTS = ROOT / "ml" / "reports"
REAL_DIR = ROOT / "ml" / "data" / "real"

SOC_HIDDEN = (16, 16)
GMM_COMPONENTS = 8
GAIN_GRID_REST = [0.01, 0.02, 0.05]
GAIN_GRID_LOAD = [0.0005, 0.002, 0.005]
THRESHOLD_QUANTILES = [0.999, 0.9995, 0.9999, 0.99995, 1.0]
THRESHOLD_MARGIN = 1.0      # nats added on top of the chosen quantile
DETECT_WINDOW_S = 30 * 60   # an alarm within this long after onset counts as a detection
FULL_REST_V = 4.15          # real logs: rested voltage that counts as a full cell


# ── Data ─────────────────────────────────────────────────────────

def simulate_dataset(args):
    rng = np.random.default_rng(args.seed)
    specs = []
    for split, count in (("train", args.train_cells), ("val", args.val_cells), ("test", args.test_cells)):
        specs += [cell_sim.normal_scenario(rng, split, f"{split}{k:02d}") for k in range(count)]
    for kind in cell_sim.ANOMALY_KINDS:
        specs += [cell_sim.anomaly_scenario(rng, kind, f"{kind}{k:02d}") for k in range(args.anomaly_runs)]
    runs = cell_sim.simulate_many(specs, jobs=args.jobs)
    for spec, run in zip(specs, runs):
        run.attrs.update(split=spec["split"], spec=spec)
    return runs


def label_real_log(df):
    """SoC labels for a real log, from full discharges only.

    A load-on segment that starts from a rested cell at >= FULL_REST_V and ends at
    the undervoltage cut-off is labelled 100 % -> 0 % by integrating current. The
    rest just before it is labelled 100 %, and the rest just after it 0 % (until
    the charger is connected, seen as a voltage step). Charging stays unlabelled.
    """
    v = df["voltage"].to_numpy(float)
    i = df["current_mA"].to_numpy(float)
    load = df["load"].to_numpy(bool)
    dt = np.minimum(ref.sample_dt(df["t_s"].to_numpy(float)), ref.MAX_DT_S)
    soc = np.full(len(df), np.nan)

    edges = np.flatnonzero(np.diff(np.concatenate([[0], load.astype(int), [0]])))
    for a, b in zip(edges[::2], edges[1::2]):
        if a == 0 or v[a - 1] < FULL_REST_V or v[b - 1] > 3.05:
            continue
        q = np.cumsum(np.clip(i[a:b], 0, None) * dt[a:b]) / 3600.0
        if q[-1] < 100.0:
            continue
        soc[a:b] = 100.0 * (1.0 - q / q[-1])
        k = a - 1
        while k >= 0 and not load[k] and a - k <= 1800 and v[k] >= FULL_REST_V:
            soc[k] = 100.0
            k -= 1
        k = b
        while k < len(df) and not load[k] and k - b < 1800 and (k == b or v[k] - v[k - 1] <= ref.STEP_VOLTAGE_V):
            soc[k] = 0.0
            k += 1
    return soc


def load_real_logs(directory):
    runs = []
    for path in sorted(Path(directory).glob("*.csv")):
        df = pd.read_csv(path)
        df = pd.DataFrame({
            "run": path.stem,
            "t_s": df["t_ms"].to_numpy(float) / 1000.0,
            "voltage": df["voltage"], "current_mA": df["current_mA"], "temp_c": df["temp_c"],
            "load": df["load"].astype(bool),
        })
        df["soc_true"] = label_real_log(df)
        df.attrs.update(split="real", spec=None)
        n_lab = int(np.isfinite(df["soc_true"]).sum())
        print(f"  real log {path.name}: {len(df)} samples, {n_lab} with SoC labels")
        runs.append(df)
    return runs


def settled_rows(runs, stride):
    """Window features of every settled sample, thinned by `stride`."""
    parts = []
    for run in runs:
        feats, settled = ref.window_features(run["voltage"].to_numpy(float), run["current_mA"].to_numpy(float),
                                             run["temp_c"].to_numpy(float), ref.segment_ids(run["t_s"].to_numpy(float)))
        idx = np.flatnonzero(settled)[::stride]
        part = pd.DataFrame({k: feats[k][idx] for k in feats})
        part["soc_true"] = run["soc_true"].to_numpy(float)[idx]
        part["real"] = run.attrs["split"] == "real"
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


# ── Training ─────────────────────────────────────────────────────

def train_soc_mlp(rows, seed):
    lab = rows[np.isfinite(rows["soc_true"])]
    n_real = int(lab["real"].sum())
    if n_real:
        # Repeat real samples so they carry about as much weight as the synthetic ones.
        reps = int(min(20, max(1, round((len(lab) - n_real) / n_real))))
        lab = pd.concat([lab[~lab["real"]]] + [lab[lab["real"]]] * reps, ignore_index=True)
    X = lab[ref.SOC_FEATURES].to_numpy(float)
    y = lab["soc_true"].to_numpy(float) / 100.0
    x_mean, x_std = X.mean(axis=0), X.std(axis=0) + 1e-9
    mlp = MLPRegressor(hidden_layer_sizes=SOC_HIDDEN, activation="tanh", alpha=1e-4,
                       batch_size=512, learning_rate_init=2e-3, max_iter=120,
                       early_stopping=True, n_iter_no_change=10, random_state=seed)
    mlp.fit((X - x_mean) / x_std, y)
    print(f"  MLP: {len(X)} samples ({n_real} real), {mlp.n_iter_} epochs")
    return {
        "x_mean": x_mean, "x_std": x_std,
        "W1": mlp.coefs_[0].T, "b1": mlp.intercepts_[0],
        "W2": mlp.coefs_[1].T, "b2": mlp.intercepts_[1],
        "W3": mlp.coefs_[2][:, 0], "b3": float(mlp.intercepts_[2][0]),
    }


def train_gmm(rows, soc_params, seed, max_rows=200_000):
    soc_ml = ref.mlp_soc(rows[ref.SOC_FEATURES].to_numpy(float), soc_params)
    G = ref.gmm_features({k: rows[k].to_numpy(float) for k in rows}, soc_ml)
    if len(G) > max_rows:
        G = G[np.random.default_rng(seed).choice(len(G), max_rows, replace=False)]
    gmm = GaussianMixture(GMM_COMPONENTS, covariance_type="full", reg_covar=1.0,
                          n_init=2, max_iter=300, random_state=seed).fit(G)
    d = G.shape[1]
    logdet = np.array([np.linalg.slogdet(c)[1] for c in gmm.covariances_])
    params = {"means": gmm.means_, "prec": gmm.precisions_,
              "lognorm": np.log(gmm.weights_) - 0.5 * (d * math.log(2 * math.pi) + logdet)}
    check = G[:2000]
    assert np.allclose(ref.gmm_score(check, params), -gmm.score_samples(check), atol=1e-6)
    print(f"  GMM: {GMM_COMPONENTS} components on {len(G)} samples")
    return params


def choose_threshold(val_outputs):
    """Smallest candidate threshold that gives no false alarms on the validation runs."""
    scores = np.concatenate([s[settled] for _, settled, _, s in val_outputs])
    for q in THRESHOLD_QUANTILES:
        thr = float(np.quantile(scores, q)) + THRESHOLD_MARGIN
        alarms = sum(int(np.diff(ref.detector_sequence(settled, s, thr).astype(int), prepend=0).clip(0).sum())
                     for _, settled, _, s in val_outputs)
        print(f"  threshold q={q}: {thr:.2f} -> {alarms} false alarms on validation")
        if alarms == 0:
            return thr
    return thr


def tune_fusion_gains(val_runs, val_outputs):
    best = None
    for g_rest in GAIN_GRID_REST:
        for g_load in GAIN_GRID_LOAD:
            errs = []
            for run, (feats, settled, soc_ml, _) in zip(val_runs, val_outputs):
                v = run["voltage"].to_numpy(float)
                i = run["current_mA"].to_numpy(float)
                dt = np.minimum(ref.sample_dt(run["t_s"].to_numpy(float)), ref.MAX_DT_S)
                fused = ref.fusion_sequence(v, i, dt, settled, soc_ml, feats["currentAvg_A"], g_rest, g_load)
                errs.append((fused - run["soc_true"].to_numpy(float))[60:])
            rmse = float(np.sqrt(np.mean(np.concatenate(errs) ** 2)))
            if best is None or rmse < best[0]:
                best = (rmse, g_rest, g_load)
    print(f"  fusion gains: rest {best[1]}, load {best[2]} (validation RMSE {best[0]:.2f} %)")
    return best[1], best[2]


# ── Evaluation ───────────────────────────────────────────────────

def soc_error_stats(err):
    err = err[np.isfinite(err)]
    if not len(err):
        return None
    return {"rmse": float(np.sqrt(np.mean(err ** 2))), "mae": float(np.mean(np.abs(err))),
            "p95": float(np.quantile(np.abs(err), 0.95)), "max": float(np.max(np.abs(err))), "n": int(len(err))}


def evaluate_soc(test_runs, outputs):
    state_masks = {
        "all": lambda r: np.ones(len(r), bool),
        "rest": lambda r: ~r["load"].to_numpy(bool) & ~r["charging"].to_numpy(bool),
        "discharge": lambda r: r["load"].to_numpy(bool),
        "charge": lambda r: r["charging"].to_numpy(bool),
    }
    result = {}
    for state, mask_fn in state_masks.items():
        errs = {"v1": [], "ml_only": [], "ml_fused": []}
        for run, out in zip(test_runs, outputs):
            m = mask_fn(run) & (run["t_s"].to_numpy() >= ref.FEATURE_WINDOW)
            truth = run["soc_true"].to_numpy(float)
            errs["v1"].append((out["soc_v1"].to_numpy() - truth)[m])
            errs["ml_fused"].append((out["soc_fused"].to_numpy() - truth)[m])
            mr = m & out["ml_ready"].to_numpy()
            errs["ml_only"].append((out["soc_ml"].to_numpy() - truth)[mr])
        result[state] = {k: soc_error_stats(np.concatenate(v)) for k, v in errs.items()}
    return result


def rising_edges(flag):
    return np.flatnonzero(np.diff(flag.astype(int), prepend=0) == 1)


def evaluate_anomalies(normal_runs, normal_outputs, anomaly_runs, anomaly_outputs):
    false_alarms = 0
    normal_hours = 0.0
    for run, out in zip(normal_runs, normal_outputs):
        false_alarms += len(rising_edges(out["anomaly"].to_numpy()))
        normal_hours += len(run) / 3600.0

    per_kind = {}
    for run, out in zip(anomaly_runs, anomaly_outputs):
        spec = run.attrs["spec"]
        kind, onset = spec["anomaly"]["kind"], spec["anomaly"]["onset_s"]
        t = run["t_s"].to_numpy()
        edges = t[rising_edges(out["anomaly"].to_numpy())]
        false_alarms += int(np.sum(edges < onset))
        normal_hours += onset / 3600.0
        hits = edges[(edges >= onset) & (edges <= onset + DETECT_WINDOW_S)]
        delay = float(hits[0] - onset) if len(hits) else None
        over = t[(t >= onset) & (run["temp_c"].to_numpy() > cell_sim.MAX_TEMP_C)]
        hard_limit = float(over[0] - onset) if len(over) else None
        per_kind.setdefault(kind, []).append({"run": spec["name"], "delay_s": delay, "hard_limit_s": hard_limit})

    summary = {}
    for kind, items in per_kind.items():
        delays = [x["delay_s"] for x in items if x["delay_s"] is not None]
        leads = [x["hard_limit_s"] - x["delay_s"] for x in items
                 if x["delay_s"] is not None and x["hard_limit_s"] is not None]
        summary[kind] = {
            "runs": len(items),
            "detected": len(delays),
            "median_delay_min": float(np.median(delays) / 60) if delays else None,
            "reached_max_temp": sum(x["hard_limit_s"] is not None for x in items),
            "median_lead_over_max_temp_min": float(np.median(leads) / 60) if leads else None,
        }
    return {"false_alarms": false_alarms, "normal_hours": normal_hours,
            "false_alarms_per_100h": 100.0 * false_alarms / max(normal_hours, 1e-9), "by_kind": summary}


# ── Export ───────────────────────────────────────────────────────

def c_floats(values):
    flat = np.asarray(values, float)
    if flat.ndim == 0:
        text = f"{float(flat):.9g}"
        return text + ("f" if any(c in text for c in ".e") else ".0f")  # "50f" is not valid C++
    return "{" + ", ".join(c_floats(v) for v in flat) + "}"


def write_header(model, summary):
    soc, gmm = model["soc"], model["gmm"]
    k, d = gmm["means"].shape
    lines = [
        "// AUTO-GENERATED by ml/train_models.py - do not edit by hand.",
        "// Re-run the script after collecting new data to regenerate this file.",
        "//",
    ]
    lines += [f"// {s}" for s in summary]
    lines += [
        "",
        "#pragma once",
        "",
        "namespace bmsml_model",
        "{",
        "// Engine constants (ml/bms_reference.py)",
        f"constexpr int SAMPLE_PERIOD_MS = {ref.SAMPLE_PERIOD_MS};",
        f"constexpr int FEATURE_WINDOW = {ref.FEATURE_WINDOW};",
        f"constexpr int SETTLE_SAMPLES = {ref.SETTLE_SAMPLES};",
        f"constexpr float STEP_VOLTAGE_V = {c_floats(ref.STEP_VOLTAGE_V)};",
        f"constexpr float STEP_CURRENT_MA = {c_floats(ref.STEP_CURRENT_MA)};",
        f"constexpr float GAP_FACTOR = {c_floats(ref.GAP_FACTOR)};",
        f"constexpr float MAX_DT_S = {c_floats(ref.MAX_DT_S)};",
        f"constexpr float CC_DEADBAND_MA = {c_floats(ref.CC_DEADBAND_MA)};",
        f"constexpr float REST_CURRENT_MA = {c_floats(ref.REST_CURRENT_MA)};",
        f"constexpr float V1_SOC_ALPHA = {c_floats(ref.V1_SOC_ALPHA)};",
        f"constexpr int ANOMALY_PERSIST = {ref.ANOMALY_PERSIST};",
        f"constexpr int ANOMALY_CLEAR = {ref.ANOMALY_CLEAR};",
        "",
        "// Fusion gains (tuned on validation data)",
        f"constexpr float FUSION_GAIN_REST = {c_floats(model['gain_rest'])};",
        f"constexpr float FUSION_GAIN_LOAD = {c_floats(model['gain_load'])};",
        "",
        "// SoC MLP: inputs " + ", ".join(ref.SOC_FEATURES),
        f"constexpr int SOC_IN = {len(ref.SOC_FEATURES)};",
        f"constexpr int SOC_H1 = {SOC_HIDDEN[0]};",
        f"constexpr int SOC_H2 = {SOC_HIDDEN[1]};",
        f"constexpr float SOC_X_MEAN[SOC_IN] = {c_floats(soc['x_mean'])};",
        f"constexpr float SOC_X_STD[SOC_IN] = {c_floats(soc['x_std'])};",
        f"constexpr float SOC_W1[SOC_H1][SOC_IN] = {c_floats(soc['W1'])};",
        f"constexpr float SOC_B1[SOC_H1] = {c_floats(soc['b1'])};",
        f"constexpr float SOC_W2[SOC_H2][SOC_H1] = {c_floats(soc['W2'])};",
        f"constexpr float SOC_B2[SOC_H2] = {c_floats(soc['b2'])};",
        f"constexpr float SOC_W3[SOC_H2] = {c_floats(soc['W3'])};",
        f"constexpr float SOC_B3 = {c_floats(soc['b3'])};",
        "",
        "// Anomaly GMM: features " + ", ".join(ref.GMM_FEATURES) + ", each divided by GMM_SCALE",
        f"constexpr int GMM_K = {k};",
        f"constexpr int GMM_D = {d};",
        f"constexpr float GMM_SCALE[GMM_D] = {c_floats(ref.GMM_SCALE)};",
        f"constexpr float GMM_MEAN[GMM_K][GMM_D] = {c_floats(gmm['means'])};",
        f"constexpr float GMM_PREC[GMM_K][GMM_D][GMM_D] = {c_floats(gmm['prec'])};",
        f"constexpr float GMM_LOGNORM[GMM_K] = {c_floats(gmm['lognorm'])};",
        f"constexpr float ANOMALY_THRESHOLD = {c_floats(model['threshold'])};",
        "} // namespace bmsml_model",
        "",
    ]
    HEADER.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {HEADER.relative_to(ROOT)}")


def write_parity_vectors(model, runs):
    """Raw samples for the C++ test, plus what the Python engine makes of them."""
    BUILD.mkdir(parents=True, exist_ok=True)
    raw = pd.concat([pd.DataFrame({
        "run": n, "t_ms": (r["t_s"] * 1000).round().astype(int), "voltage": r["voltage"].round(4),
        "current_mA": r["current_mA"].round(2), "temp_c": r["temp_c"].round(4), "load": r["load"].astype(int),
    }) for n, r in enumerate(runs)], ignore_index=True)
    raw.to_csv(BUILD / "parity_input.csv", index=False)

    # Re-read so the Python side sees exactly the rounded text the C++ side parses.
    raw = pd.read_csv(BUILD / "parity_input.csv")
    outs = []
    for n, r in raw.groupby("run", sort=True):
        r = r.assign(t_s=r["t_ms"] / 1000.0, load=r["load"].astype(bool))
        out = ref.run_engine(r.reset_index(drop=True), model)
        outs.append(out.assign(run=n))
    exp = pd.concat(outs, ignore_index=True)
    cols = ["run", "ml_ready", "currentAvg_A", "dVdt_mVmin", "dTdt_Cmin", "soc_ml",
            "anomaly_score", "anomaly", "soc_v1", "soc_fused"]
    exp[cols].astype({"ml_ready": int, "anomaly": int}).fillna(-999).to_csv(
        BUILD / "parity_expected.csv", index=False, float_format="%.6f")
    print(f"  wrote parity vectors ({len(raw)} samples)")


def plot_reports(test_runs, test_outputs, anomaly_runs, anomaly_outputs, threshold):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    run, out = max(zip(test_runs, test_outputs), key=lambda ro: ro[0]["load"].sum() + ro[0]["charging"].sum())
    h = run["t_s"] / 3600
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, height_ratios=[2, 1])
    ax1.plot(h, run["soc_true"], color="black", lw=2, label="true SoC (simulation)")
    ax1.plot(h, out["soc_v1"], color="#d95f02", lw=1.2, label="v1: OCV table + Coulomb counting")
    ax1.plot(h, out["soc_fused"], color="#1b9e77", lw=1.4, label="v2: ML + Coulomb counting")
    ax1.set_ylabel("SoC (%)")
    ax1.legend(loc="lower left", fontsize=9)
    ax1.set_title(f"SoC on a held-out simulated cell ({run.attrs['spec']['cell']['capacity_mAh']:.0f} mAh, "
                  f"firmware assumes {ref.RATED_CAPACITY_MAH:.0f} mAh)")
    ax2.plot(h, run["voltage"], color="#7570b3", lw=1)
    ax2.set_ylabel("Voltage (V)")
    ax2.set_xlabel("Time (h)")
    for ax in (ax1, ax2):
        ax.fill_between(h, 0, 1, where=run["load"], transform=ax.get_xaxis_transform(), color="#d95f02", alpha=0.08, lw=0)
        ax.fill_between(h, 0, 1, where=run["charging"], transform=ax.get_xaxis_transform(), color="#1b9e77", alpha=0.08, lw=0)
    fig.tight_layout()
    fig.savefig(REPORTS / "soc_tracking.png", dpi=130)
    plt.close(fig)

    picks = []
    for kind in cell_sim.ANOMALY_KINDS:
        for r, o in zip(anomaly_runs, anomaly_outputs):
            if r.attrs["spec"]["anomaly"]["kind"] == kind:
                picks.append((kind, r, o))
                break
    fig, axes = plt.subplots(2, len(picks), figsize=(4.2 * len(picks), 5.5), sharex="col", squeeze=False)
    for col, (kind, r, o) in enumerate(picks):
        onset = r.attrs["spec"]["anomaly"]["onset_s"]
        m = r["t_s"].to_numpy() >= onset - 40 * 60
        tm = (r["t_s"][m] - onset) / 60
        top, bottom = axes[0, col], axes[1, col]
        top.plot(tm, r["temp_c"][m], color="#d95f02")
        top.axhline(cell_sim.MAX_TEMP_C, color="grey", ls=":", lw=1)
        top.set_title(kind.replace("_", " "))
        top.set_ylabel("Temperature (C)")
        bottom.plot(tm, o["anomaly_score"][m], color="#7570b3", lw=1)
        bottom.axhline(threshold, color="grey", ls="--", lw=1)
        bottom.set_ylabel("Anomaly score")
        bottom.set_xlabel("Minutes from fault onset")
        edges = rising_edges(o["anomaly"].to_numpy())
        for ax in (top, bottom):
            ax.axvline(0, color="black", lw=1)
            for e in edges:
                ax.axvline((r["t_s"].iloc[e] - onset) / 60, color="#e7298a", lw=1.5)
    fig.suptitle("Black: fault onset   Pink: ML anomaly flag   Dotted: 50 C hard limit   Dashed: score threshold", fontsize=9)
    fig.tight_layout()
    fig.savefig(REPORTS / "anomaly_detection.png", dpi=130)
    plt.close(fig)
    print(f"  wrote plots to {REPORTS.relative_to(ROOT)}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train-cells", type=int, default=48)
    ap.add_argument("--val-cells", type=int, default=8)
    ap.add_argument("--test-cells", type=int, default=12)
    ap.add_argument("--anomaly-runs", type=int, default=15, help="simulated runs per anomaly kind")
    ap.add_argument("--real-dir", type=Path, default=REAL_DIR)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--stride", type=int, default=5, help="use every Nth settled sample for training")
    ap.add_argument("--quick", action="store_true", help="small dataset for a smoke test")
    args = ap.parse_args()
    if args.quick:
        args.train_cells, args.val_cells, args.test_cells, args.anomaly_runs = 10, 3, 3, 3
    t0 = time.time()

    print("Simulating cells...")
    sim = simulate_dataset(args)
    by_split = {s: [r for r in sim if r.attrs["split"] == s] for s in ("train", "val", "test", "anomaly")}
    real = load_real_logs(args.real_dir) if args.real_dir.exists() else []
    print(f"  {sum(len(r) for r in sim) / 3600:.0f} simulated hours, {len(real)} real logs")

    print("Training...")
    rows = settled_rows(by_split["train"] + real, args.stride)
    model = {"soc": train_soc_mlp(rows, args.seed)}
    model["gmm"] = train_gmm(rows, model["soc"], args.seed)
    val_outputs = [ref.stateless_outputs(r, model) for r in by_split["val"]]
    model["threshold"] = choose_threshold(val_outputs)
    model["gain_rest"], model["gain_load"] = tune_fusion_gains(by_split["val"], val_outputs)

    print("Evaluating on held-out cells...")
    test_outputs = [ref.run_engine(r, model) for r in by_split["test"]]
    anomaly_outputs = [ref.run_engine(r, model) for r in by_split["anomaly"]]
    metrics = {
        "data": {"train_cells": args.train_cells, "val_cells": args.val_cells, "test_cells": args.test_cells,
                 "anomaly_runs_per_kind": args.anomaly_runs, "real_logs": [r["run"].iloc[0] for r in real],
                 "seed": args.seed},
        "soc": evaluate_soc(by_split["test"], test_outputs),
        "anomaly": evaluate_anomalies(by_split["test"], test_outputs, by_split["anomaly"], anomaly_outputs),
        "model": {"threshold": model["threshold"], "gain_rest": model["gain_rest"], "gain_load": model["gain_load"]},
    }
    if real:
        real_outputs = [ref.run_engine(r, model) for r in real]
        metrics["real_logs_in_sample"] = {
            r["run"].iloc[0]: {k: soc_error_stats((o[k] - r["soc_true"]).to_numpy()) for k in ("soc_v1", "soc_fused")}
            for r, o in zip(real, real_outputs)}

    s = metrics["soc"]["all"]
    a = metrics["anomaly"]
    summary = [
        f"Training data: {args.train_cells} simulated cells + {len(real)} real logs (seed {args.seed}).",
        f"Held-out SoC RMSE: v1 {s['v1']['rmse']:.1f} %, v2 ML-fused {s['ml_fused']['rmse']:.1f} %.",
        "Anomaly detection: " + ", ".join(f"{k} {v['detected']}/{v['runs']}" for k, v in a["by_kind"].items())
        + f"; {a['false_alarms']} false alarms in {a['normal_hours']:.0f} h.",
    ]
    print("\n".join("  " + line for line in summary))

    print("Exporting...")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    write_header(model, summary)
    BUILD.mkdir(parents=True, exist_ok=True)
    with open(BUILD / "model.pkl", "wb") as f:
        pickle.dump(model, f)
    parity_runs = [by_split["test"][0].iloc[:6 * 3600]] + [
        next(r for r in by_split["anomaly"] if r.attrs["spec"]["anomaly"]["kind"] == "internal_short")]
    write_parity_vectors(model, parity_runs)
    plot_reports(by_split["test"], test_outputs, by_split["anomaly"], anomaly_outputs, model["threshold"])
    print(f"Done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
