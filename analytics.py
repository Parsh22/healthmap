import bisect
import math
from datetime import timedelta

VERY_LOW, LOW, HIGH, VERY_HIGH = 54, 70, 180, 250
STEP = timedelta(minutes=15)
STEP_MIN = 15
HORIZON = 8  # steps of 15 min -> 2 hours
LOOKBACK = 16  # 4 hours of history
MIN_SEG = 5  # need readings at lags 0..60 min for the regression features
DEFAULT_PARAMS = (0.6, 0.2, 0.9, 1.0)
PARAM_GRID = [
    (a, b, p, tau)
    for a in (0.3, 0.6, 0.9)
    for b in (0.05, 0.2, 0.4)
    for p in (0.8, 0.9, 0.98)
    for tau in (0.5, 1.0, 2.0, 6.0)
]
METHOD_NAMES = {"ensemble": "Ensemble (regression + smoothing)", "ridge": "Personal regression",
                "holt": "Smoothing + daily rhythm", "naive": "No change (baseline)"}


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def summary(readings):
    vals = [v for _, v in readings]
    n = len(vals)
    if not n:
        return None
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / n)

    def pct(cond):
        return round(100 * sum(1 for v in vals if cond(v)) / n, 1)

    return {
        "count": n,
        "mean": round(mean),
        "sd": round(sd),
        "cv": round(100 * sd / mean, 1),
        "gmi": round(3.31 + 0.02392 * mean, 1),
        "min": round(min(vals)),
        "max": round(max(vals)),
        "very_low": pct(lambda v: v < VERY_LOW),
        "low": pct(lambda v: VERY_LOW <= v < LOW),
        "in_range": pct(lambda v: LOW <= v <= HIGH),
        "high": pct(lambda v: HIGH < v <= VERY_HIGH),
        "very_high": pct(lambda v: v > VERY_HIGH),
    }


def hourly_profile(readings):
    buckets = [[] for _ in range(24)]
    for t, v in readings:
        buckets[t.hour].append(v)
    out = []
    for h, b in enumerate(buckets):
        b.sort()
        out.append({
            "hour": h,
            "n": len(b),
            **{k: (round(percentile(b, q), 1) if b else None)
               for k, q in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9))},
        })
    return out


def profile_value(profile, t, fallback):
    # Linear interpolation between hour-bucket centers for a smooth daily curve.
    x = t.hour + t.minute / 60 - 0.5
    h0 = math.floor(x)
    frac = x - h0
    v0 = profile[h0 % 24]["p50"]
    v1 = profile[(h0 + 1) % 24]["p50"]
    v0 = fallback if v0 is None else v0
    v1 = fallback if v1 is None else v1
    return v0 + (v1 - v0) * frac


def daily_breakdown(readings):
    days = {}
    for t, v in readings:
        days.setdefault(t.date(), []).append(v)
    out = []
    for d in sorted(days):
        s = summary([(None, v) for v in days[d]])
        out.append({"date": d.isoformat(), "mean": s["mean"], "very_low": s["very_low"], "low": s["low"],
                    "in_range": s["in_range"], "high": s["high"], "very_high": s["very_high"]})
    return out


def resample(readings, times, max_gap=timedelta(minutes=60), snap=timedelta(minutes=10)):
    """Linearly interpolate sorted readings onto `times`; None where data is missing."""
    out, j, n = [], 0, len(readings)
    if not n:
        return [None] * len(times)
    for t in times:
        while j + 1 < n and readings[j + 1][0] <= t:
            j += 1
        t0, v0 = readings[j]
        if t0 > t:
            out.append(v0 if t0 - t <= snap else None)
        elif t0 == t:
            out.append(v0)
        elif j + 1 == n:
            out.append(v0 if t - t0 <= snap else None)
        else:
            t1, v1 = readings[j + 1]
            if t1 - t0 <= max_gap:
                out.append(v0 + (v1 - v0) * ((t - t0) / (t1 - t0)))
            elif t - t0 <= snap:
                out.append(v0)
            elif t1 - t <= snap:
                out.append(v1)
            else:
                out.append(None)
    return out


def _trailing_segment(values, end_idx, max_len=LOOKBACK):
    seg = []
    i = end_idx
    while i >= 0 and len(seg) < max_len and values[i] is not None:
        seg.append(values[i])
        i -= 1
    seg.reverse()
    return seg


# ---------- model 1: damped-trend Holt smoothing blended toward the daily profile ----------

def _holt_predict(seg, prof_vals, params):
    alpha, beta, phi, tau = params
    level, trend = seg[0], 0.0
    for y in seg[1:]:
        prev = level
        level = alpha * y + (1 - alpha) * (prev + phi * trend)
        trend = beta * (level - prev) + (1 - beta) * phi * trend
    preds, damp = [], 0.0
    for h in range(1, HORIZON + 1):
        damp += phi ** h
        w = math.exp(-(h * STEP_MIN / 60) / tau)
        preds.append(w * (level + damp * trend) + (1 - w) * prof_vals[h - 1])
    return preds


# ---------- model 2: direct multi-horizon ridge regression (ARX-style) ----------

def _event_index(events):
    idx = {"meal": [], "exercise": [], "insulin": []}
    for e in events or []:
        if e[1] in idx:
            idx[e[1]].append(e[0])
    for v in idx.values():
        v.sort()
    return idx


EVENT_PEAK_HOURS = {"meal": 1.0, "exercise": 1.0, "insulin": 1.25}  # generic physiology, not fitted


def _action(dt_hours, peak):
    # Gamma-like action curve (same shape family as carbs-on-board / insulin-action curves).
    return 0.0 if dt_hours <= 0 else (dt_hours / peak) * math.exp(1 - dt_hours / peak)


def _event_features(ev, origin):
    """Per horizon: how much each event type's effect is expected to change between now and t+h."""
    out = [[0.0, 0.0, 0.0] for _ in range(HORIZON)]
    for j, kind in enumerate(("meal", "exercise", "insulin")):
        ts = ev[kind]
        i = bisect.bisect_right(ts, origin - timedelta(hours=6))
        while i < len(ts) and ts[i] <= origin:
            dt = (origin - ts[i]).total_seconds() / 3600
            now = _action(dt, EVENT_PEAK_HOURS[kind])
            for h in range(HORIZON):
                out[h][j] += _action(dt + (h + 1) * STEP_MIN / 60, EVENT_PEAK_HOURS[kind]) - now
            i += 1
    return out


def _base_features(seg, origin, ev):
    y0, y1, y2, y4 = seg[-1], seg[-2], seg[-3], seg[-5]
    tod = 2 * math.pi * (origin.hour * 60 + origin.minute) / 1440
    return {"lags": [y0 - y1, y1 - y2, y0 - y4, math.sin(tod), math.cos(tod)], "events": _event_features(ev, origin)}


def _row(base, h, prof_v, y0):
    return base["lags"] + base["events"][h] + [prof_v - y0]


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def _fit_ridge(X, y, lam=5.0):
    n, p = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(p)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1.0 for j in range(p)]
    Z = [[1.0] + [(r[j] - mu[j]) / sd[j] for j in range(p)] for r in X]
    A = [[sum(z[i] * z[k] for z in Z) + (lam if i == k and i > 0 else 0.0) for k in range(p + 1)]
         for i in range(p + 1)]
    b = [sum(z[i] * t for z, t in zip(Z, y)) for i in range(p + 1)]
    return {"mu": mu, "sd": sd, "w": _solve(A, b)}


def _ridge_apply(m, x):
    return m["w"][0] + sum(w * (xi - mu) / sd for w, xi, mu, sd in zip(m["w"][1:], x, m["mu"], m["sd"]))


def _ridge_predict(models, seg, base, prof_vals):
    y0 = seg[-1]
    return [y0 + _ridge_apply(models[h], _row(base, h, prof_vals[h], y0)) for h in range(HORIZON)]


def _fit_ridge_all(origins):
    return [_fit_ridge([_row(o["base"], h, o["prof"][h], o["seg"][-1]) for o in origins],
                       [o["actual"][h] - o["seg"][-1] for o in origins]) for h in range(HORIZON)]


# ---------- model selection and evaluation ----------

def _clip(preds):
    return [min(400.0, max(40.0, p)) for p in preds]


def _subsample(items, k):
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def build_model(readings, events=None):
    """Train on the first 60% of history, pick a method on the next 20%, report accuracy on the last 20%."""
    if len(readings) < 100 or readings[-1][0] - readings[0][0] < timedelta(days=2):
        return None
    ev = _event_index(events)
    start, end = readings[0][0], readings[-1][0]
    start = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
    times = []
    t = start
    while t <= end:
        times.append(t)
        t += STEP
    values = resample(readings, times)
    t60, t80 = start + (end - start) * 0.6, start + (end - start) * 0.8
    train_r = [r for r in readings if r[0] < t60]
    profile = hourly_profile(train_r)
    fallback = sum(v for _, v in train_r) / len(train_r)

    splits = {"train": [], "val": [], "test": []}
    for i in range(len(times) - HORIZON):
        if values[i] is None or any(values[i + h] is None for h in range(1, HORIZON + 1)):
            continue
        seg = _trailing_segment(values, i)
        if len(seg) < MIN_SEG:
            continue
        o = {"t": times[i], "seg": seg, "actual": values[i + 1:i + HORIZON + 1],
             "base": _base_features(seg, times[i], ev),
             "prof": [profile_value(profile, times[i + h], fallback) for h in range(1, HORIZON + 1)]}
        if times[i + HORIZON] < t60:
            splits["train"].append(o)
        elif times[i] >= t60 and times[i + HORIZON] < t80:
            splits["val"].append(o)
        elif times[i] >= t80:
            splits["test"].append(o)
    train = _subsample(splits["train"], 600)
    val, test = _subsample(splits["val"], 200), _subsample(splits["test"], 200)
    if len(train) < 40 or len(val) < 20 or len(test) < 20:
        return None

    holt_tune = _subsample(train, 150)

    def holt_mae(params):
        return sum(abs(p - a) for o in holt_tune for p, a in zip(_holt_predict(o["seg"], o["prof"], params),
                                                                  o["actual"])) / len(holt_tune)

    holt_params = min(PARAM_GRID, key=holt_mae)
    ridge = _fit_ridge_all(train)
    predictors = {
        "naive": lambda o: [o["seg"][-1]] * HORIZON,
        "holt": lambda o: _clip(_holt_predict(o["seg"], o["prof"], holt_params)),
        "ridge": lambda o: _clip(_ridge_predict(ridge, o["seg"], o["base"], o["prof"])),
    }
    predictors["ensemble"] = lambda o: [(a + b) / 2 for a, b in zip(predictors["holt"](o), predictors["ridge"](o))]

    def errors(name, origins):
        errs = [[] for _ in range(HORIZON)]
        for o in origins:
            for h, (p, a) in enumerate(zip(predictors[name](o), o["actual"])):
                errs[h].append(a - p)
        return errs

    val_mae = {k: sum(abs(e) for hs in errors(k, val) for e in hs) for k in predictors}
    method = min(val_mae, key=val_mae.get)

    comparison = {}
    for name in predictors:
        errs = errors(name, test)
        mae = [sum(abs(e) for e in hs) / len(hs) for hs in errs]
        rmse = [math.sqrt(sum(e * e for e in hs) / len(hs)) for hs in errs]
        comparison[name] = {"name": METHOD_NAMES[name], "key": name, "mae_30": round(mae[1], 1),
                            "mae_60": round(mae[3], 1), "mae_120": round(mae[7], 1),
                            "rmse_30": round(rmse[1], 1), "rmse_60": round(rmse[3], 1), "errs": errs}
    chosen = comparison[method]

    # Refit the regression on all data with a full-history profile for live forecasts.
    full_profile = hourly_profile(readings)
    full_fallback = sum(v for _, v in readings) / len(readings)
    all_o = _subsample(splits["train"] + splits["val"] + splits["test"], 1000)
    refit = [dict(o, prof=[profile_value(full_profile, o["t"] + h * STEP, full_fallback)
                           for h in range(1, HORIZON + 1)]) for o in all_o]
    naive60 = comparison["naive"]["mae_60"]
    return {
        "method": method,
        "method_name": METHOD_NAMES[method],
        "holt_params": holt_params,
        "ridge": _fit_ridge_all(refit),
        "n_test": len(test),
        "mae_30": chosen["mae_30"], "mae_60": chosen["mae_60"], "mae_120": chosen["mae_120"],
        "rmse_30": chosen["rmse_30"], "rmse_60": chosen["rmse_60"],
        "naive_60": naive60,
        "improvement_60": round(100 * (1 - chosen["mae_60"] / naive60), 1) if naive60 else 0.0,
        "comparison": [{k: v for k, v in comparison[n].items() if k != "errs"} for n in ("ensemble", "ridge", "holt", "naive")],
        "errs": [sorted(e) for e in chosen["errs"]],
    }


def forecast(readings, model, events=None):
    """Returns {"points": [...], "low_risk": % or None, "method": str}."""
    if not readings:
        return {"points": [], "low_risk": None, "method": None}
    last_t = readings[-1][0]
    times = [last_t - k * STEP for k in range(LOOKBACK - 1, -1, -1)]
    seg = _trailing_segment(resample(readings, times), LOOKBACK - 1)
    profile = hourly_profile(readings)
    fallback = sum(v for _, v in readings) / len(readings)
    future = [last_t + h * STEP for h in range(1, HORIZON + 1)]
    prof_vals = [profile_value(profile, t, fallback) for t in future]

    method = model["method"] if model else "holt"
    if method in ("ridge", "ensemble") and len(seg) < MIN_SEG:
        method = "holt"
    holt = _clip(_holt_predict(seg, prof_vals, model["holt_params"] if model else DEFAULT_PARAMS))
    if method in ("ridge", "ensemble"):
        ridge = _clip(_ridge_predict(model["ridge"], seg, _base_features(seg, last_t, _event_index(events)), prof_vals))
        preds = ridge if method == "ridge" else [(a + b) / 2 for a, b in zip(holt, ridge)]
    elif method == "naive":
        preds = [seg[-1]] * HORIZON
    else:
        preds = holt

    calibrated = model is not None and method == model["method"]
    sd = summary(readings)["sd"] or 20
    points, low_risk = [], 0.0
    for h, (t, p) in enumerate(zip(future, preds)):
        if calibrated:
            errs = model["errs"][h]
            lo, hi = p + percentile(errs, 0.1), p + percentile(errs, 0.9)
            low_risk = max(low_risk, sum(1 for e in errs if p + e < LOW) / len(errs))
        else:
            band = 1.28 * sd * math.sqrt((h + 1) / HORIZON)
            lo, hi = p - band, p + band
        points.append({"t": t.isoformat(), "v": round(p), "lo": round(max(40, lo)), "hi": round(min(400, hi))})
    return {"points": points, "low_risk": round(100 * low_risk) if calibrated else None, "method": METHOD_NAMES[method]}


def trend(readings):
    if len(readings) < 2:
        return None
    last_t, last_v = readings[-1]
    prev = resample(readings, [last_t - STEP])[0]
    if prev is None:
        return None
    rate = (last_v - prev) / STEP_MIN
    for limit, arrow, label in ((3, "⇈", "rising fast"), (2, "↑", "rising"), (1, "↗", "rising slowly"),
                                (-1, "→", "steady"), (-2, "↘", "falling slowly"), (-3, "↓", "falling")):
        if rate >= limit:
            return {"rate": round(rate, 1), "arrow": arrow, "label": label}
    return {"rate": round(rate, 1), "arrow": "⇊", "label": "falling fast"}


def week_over_week(readings):
    if not readings:
        return None
    last_t = readings[-1][0]
    this = [r for r in readings if r[0] > last_t - timedelta(days=7)]
    prev = [r for r in readings if last_t - timedelta(days=14) < r[0] <= last_t - timedelta(days=7)]
    if len(prev) < 20:
        return None
    a, b = summary(this), summary(prev)
    return {"tir": round(a["in_range"] - b["in_range"], 1), "mean": a["mean"] - b["mean"]}


def fmt_hour(h):
    return f"{(h % 12) or 12} {'AM' if h % 24 < 12 else 'PM'}"


def insights(stats, profile, readings):
    if not stats:
        return []
    out = []
    tir = stats["in_range"]
    out.append(f"Time in range (70–180 mg/dL) is {tir}%"
               + (" — meeting the common ≥70% target." if tir >= 70 else ", below the common ≥70% target."))
    below = stats["low"] + stats["very_low"]
    if below > 4:
        out.append(f"{below}% of readings are below 70 mg/dL (common target: under 4%).")
    if stats["cv"] > 36:
        out.append(f"Glucose variability is high (CV {stats['cv']}%, target ≤36%).")
    else:
        out.append(f"Glucose variability is stable (CV {stats['cv']}%, target ≤36%).")
    filled = [p for p in profile if p["p50"] is not None and p["n"] >= 3]
    if len(filled) >= 6:
        hi = max(filled, key=lambda p: p["p50"])
        lo = min(filled, key=lambda p: p["p50"])
        out.append(f"Highest typical level around {fmt_hour(hi['hour'])} (median {round(hi['p50'])}); "
                   f"lowest around {fmt_hour(lo['hour'])} (median {round(lo['p50'])}).")
    lows = [0] * 24
    for t, v in readings:
        if v < LOW:
            lows[t.hour] += 1
    if sum(lows) >= 3:
        h = max(range(24), key=lambda i: lows[i])
        out.append(f"Lows happen most often around {fmt_hour(h)}.")
    return out
