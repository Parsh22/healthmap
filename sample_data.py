import math
import random
from datetime import datetime, timedelta

# (center hour, jitter hours, foods with typical glucose impact in mg/dL)
MEALS = (
    (7.5, 0.5, {"oatmeal": 45, "toast and eggs": 35, "cereal": 85}),
    (13.0, 0.75, {"salad": 25, "sandwich": 55, "rice bowl": 90}),
    (19.5, 1.0, {"chicken and vegetables": 35, "pasta": 105, "pizza": 95}),
)


def generate(days=14, end=None, seed=None):
    """Synthetic 5-minute CGM trace plus the meal/exercise events that drive it.

    Returns (readings, events) where readings are (time, mg/dL) and events are (time, kind, note).
    """
    rng = random.Random(seed)
    end = (end or datetime.now()).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 5)
    start = end - timedelta(days=days)

    meals, walks, night_lows, events = [], [], [], []
    d = start.date() - timedelta(days=1)
    while d <= end.date():
        midnight = datetime.combine(d, datetime.min.time())
        for center, jitter, foods in MEALS:
            food = rng.choice(list(foods))
            t = midnight + timedelta(hours=center + rng.uniform(-jitter, jitter))
            meals.append((t, foods[food] * rng.uniform(0.8, 1.2)))
            events.append((t.replace(second=0, microsecond=0), "meal", food))
        if rng.random() < 0.4:
            t = midnight + timedelta(hours=rng.uniform(15.5, 17))
            meals.append((t, rng.uniform(20, 40)))
            events.append((t.replace(second=0, microsecond=0), "meal", "snack"))
        if rng.random() < 0.45:
            t = midnight + timedelta(hours=rng.uniform(17.5, 18.5))
            walks.append(t)
            events.append((t.replace(second=0, microsecond=0), "exercise", "walk"))
        if rng.random() < 0.2:
            night_lows.append(midnight + timedelta(hours=rng.uniform(2, 4)))
        d += timedelta(days=1)

    out, noise, t = [], 0.0, start
    while t <= end:
        hour = t.hour + t.minute / 60
        v = 112 + 18 * math.exp(-((hour - 6.5) / 1.5) ** 2)
        for mt, size in meals:
            dt = (t - mt).total_seconds() / 3600
            if 0 < dt < 5:
                v += size * (dt / 0.75) * math.exp(1 - dt / 0.75)
        for wt in walks:
            dt = (t - wt).total_seconds() / 3600
            if 0 < dt < 4:
                v -= 30 * (dt / 0.8) * math.exp(1 - dt / 0.8)
        for lt in night_lows:
            dt = (t - lt).total_seconds() / 3600
            if abs(dt) < 2.5:
                v -= 55 * math.exp(-(dt / 0.7) ** 2)
        noise = 0.95 * noise + rng.gauss(0, 3)
        out.append((t, float(min(400, max(40, round(v + noise))))))
        t += timedelta(minutes=5)
    return out, [e for e in events if start <= e[0] <= end]
