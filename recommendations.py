import bisect
from datetime import timedelta

import analytics
from analytics import HIGH, LOW, fmt_hour, resample, summary

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def event_responses(readings, events):
    """Glucose response in the 3 hours after each event (needs frequent readings around it)."""
    times = [t for t, _ in readings]
    out = []
    for t0, kind, note in events:
        base = resample(readings, [t0])[0]
        if base is None:
            continue
        i, j = bisect.bisect_right(times, t0), bisect.bisect_right(times, t0 + timedelta(hours=3))
        after = readings[i:j]
        if len(after) < 6:
            continue
        peak_t, peak_v = max(after, key=lambda r: r[1])
        v2h = resample(readings, [t0 + timedelta(hours=2)])[0]
        out.append({"kind": kind, "label": (note or kind).strip().lower(), "rise": peak_v - base,
                    "drop": base - min(v for _, v in after), "peak_min": (peak_t - t0).total_seconds() / 60,
                    "change_2h": None if v2h is None else v2h - base})
    return out


def group_responses(responses):
    groups = {}
    for r in responses:
        groups.setdefault((r["kind"], r["label"]), []).append(r)
    out = []
    for (kind, label), rs in groups.items():
        c2 = [r["change_2h"] for r in rs if r["change_2h"] is not None]
        out.append({"kind": kind, "label": label, "n": len(rs),
                    "rise": round(sum(r["rise"] for r in rs) / len(rs)),
                    "drop": round(sum(r["drop"] for r in rs) / len(rs)),
                    "peak_min": round(sum(r["peak_min"] for r in rs) / len(rs)),
                    "change_2h": round(sum(c2) / len(c2)) if c2 else None})
    out.sort(key=lambda g: (g["kind"] != "meal", -g["rise"]))
    return out


def weekday_stats(readings):
    by = [[] for _ in range(7)]
    for t, v in readings:
        by[t.weekday()].append((t, v))
    return [{"day": DAYS[i], "n": len(rs), **({"tir": summary(rs)["in_range"], "mean": summary(rs)["mean"]}
                                             if rs else {"tir": None, "mean": None})} for i, rs in enumerate(by)]


def build(readings, events):
    """Rule-based, non-prescriptive suggestions. Returns {"recs", "groups", "weekday", "period_days"}."""
    if not readings:
        return {"recs": [], "groups": [], "weekday": [], "period_days": 0}
    last_t = readings[-1][0]
    recent = [r for r in readings if r[0] > last_t - timedelta(days=14)]
    span_days = max(1, round((recent[-1][0] - recent[0][0]).total_seconds() / 86400))
    stats = summary(recent)
    profile = analytics.hourly_profile(recent)
    groups = group_responses(event_responses(readings, events))
    recs = []

    def add(level, title, body):
        recs.append({"level": level, "title": title, "body": body})

    tir = stats["in_range"]
    if tir >= 70:
        add("good", f"Time in range is {tir}%",
            "You're meeting the widely used goal of more than 70% of the day between 70 and 180 mg/dL. Keep doing what's working.")
    else:
        add("warn", f"Time in range is {tir}%",
            f"The common goal is above 70%. Every 5 points gained is linked to meaningful health benefits. "
            f"The suggestions below point to where the biggest gains are likely to be.")

    wow = analytics.week_over_week(readings)
    if wow and abs(wow["tir"]) >= 3:
        if wow["tir"] > 0:
            add("good", f"Up {wow['tir']} points vs last week",
                "Time in range improved compared with the previous 7 days. Think about what changed so you can repeat it.")
        else:
            add("warn", f"Down {abs(wow['tir'])} points vs last week",
                "Time in range dropped compared with the previous 7 days. Illness, stress, travel, or schedule changes are common causes.")

    nights, low_hours = set(), [0] * 24
    for t, v in recent:
        if v < LOW and t.hour < 6:
            nights.add(t.date())
            low_hours[t.hour] += 1
    if len(nights) >= 2:
        h = max(range(6), key=lambda i: low_hours[i])
        add("warn", f"Overnight lows on {len(nights)} of the last {span_days} nights",
            f"Most often around {fmt_hour(h)}. Lows during sleep can go unnoticed. Consider turning on CGM low alerts, and talk "
            "to your care team about evening insulin or a bedtime snack.")

    p3, p7 = profile[3]["p50"], profile[6]["p50"]
    if p3 is not None and p7 is not None and p7 - p3 >= 20:
        add("info", f"Early-morning rise of about {round(p7 - p3)} mg/dL",
            "Glucose climbs between 3 and 7 AM before you eat. This is often the “dawn phenomenon”, caused by morning hormones. "
            "It's a common, very treatable pattern worth mentioning at your next appointment.")

    blocks = []
    for b in range(0, 24, 3):
        vals = [v for t, v in recent if b <= t.hour < b + 3]
        if len(vals) >= 10:
            blocks.append((sum(1 for v in vals if v > HIGH) / len(vals), b))
    if blocks:
        frac, b = max(blocks)
        if frac >= 0.3:
            add("warn", f"Highs cluster between {fmt_hour(b)} and {fmt_hour(b + 3)}",
                f"{round(100 * frac)}% of readings in this window are above 180. Look at what usually comes just before it, "
                "most often a meal. A short walk after eating or adjusting that meal can help.")

    meals = [g for g in groups if g["kind"] == "meal" and g["n"] >= 2]
    if meals:
        worst, best = meals[0], meals[-1]
        if worst["rise"] >= 50:
            add("warn", f"“{worst['label']}” raises you the most: +{worst['rise']} mg/dL",
                f"On average across {worst['n']} times, peaking about {worst['peak_min']} minutes after eating. Ideas to try: "
                "a smaller portion, adding protein, fat, or fiber, eating vegetables first, or a 10–15 minute walk afterwards. "
                "Ask your care team about insulin timing if you use it.")
        if best is not worst and best["rise"] < 40:
            add("good", f"“{best['label']}” is gentle on you: +{best['rise']} mg/dL",
                f"One of your steadiest options ({best['n']} times logged). Good to keep in rotation.")
    else:
        add("info", "Log meals to see how each one affects you",
            "Add a meal event with a short note (like “pasta” or “oatmeal”) on the Readings page. With CGM data, HealthMap measures "
            "your rise after each food and ranks them here. Logged meals also make the forecast smarter.")

    for g in groups:
        if g["kind"] == "exercise" and g["n"] >= 2 and g["change_2h"] is not None and g["change_2h"] <= -15:
            add("info", f"“{g['label']}” lowers you by about {abs(g['change_2h'])} mg/dL",
                f"Measured 2 hours after {g['n']} sessions. Activity after a big meal can blunt the spike. If you use insulin, "
                "keep fast-acting carbs nearby in case you go low.")
            break

    wd = [v for t, v in recent if t.weekday() < 5]
    we = [v for t, v in recent if t.weekday() >= 5]
    if len(wd) >= 50 and len(we) >= 50:
        a = summary([(None, v) for v in wd])["in_range"]
        b = summary([(None, v) for v in we])["in_range"]
        if abs(a - b) >= 10:
            better, worse = ("weekdays", "weekends") if a > b else ("weekends", "weekdays")
            add("info", f"Better control on {better}",
                f"Time in range is {max(a, b)}% on {better} vs {min(a, b)}% on {worse}. Different routines, meal times, "
                f"or activity on {worse} may be worth a look.")

    if stats["cv"] > 36:
        add("warn", f"High variability (CV {stats['cv']}%)",
            "Big swings make lows more likely. Consistent meal timing and sizes, and pairing carbs with protein, are common first steps.")

    per_day = stats["count"] / span_days
    if per_day < 4:
        add("info", "More readings = better insights",
            f"You're averaging {per_day:.1f} readings a day. Checking before meals and 2 hours after, or importing CGM data, "
            "unlocks meal analysis and a personalized forecast.")

    order = {"warn": 0, "info": 1, "good": 2}
    recs.sort(key=lambda r: order[r["level"]])
    return {"recs": recs, "groups": groups, "weekday": weekday_stats(recent), "period_days": span_days}
