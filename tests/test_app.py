import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import analytics  # noqa: E402
import recommendations  # noqa: E402
import sample_data  # noqa: E402
from app import app, parse_glucose_csv  # noqa: E402


class AnalyticsTest(unittest.TestCase):
    def test_summary(self):
        t = datetime(2026, 1, 1)
        s = analytics.summary([(t, 50), (t, 100), (t, 150), (t, 200)])
        self.assertEqual(s["mean"], 125)
        self.assertEqual(s["in_range"], 50.0)
        self.assertEqual(s["very_low"], 25.0)
        self.assertEqual(s["high"], 25.0)
        self.assertEqual(s["gmi"], round(3.31 + 0.02392 * 125, 1))

    def test_resample_interpolates_and_respects_gaps(self):
        t = datetime(2026, 1, 1, 8)
        r = [(t, 100), (t + timedelta(minutes=30), 130), (t + timedelta(hours=3), 90)]
        out = analytics.resample(r, [t + timedelta(minutes=15), t + timedelta(hours=2)])
        self.assertAlmostEqual(out[0], 115)
        self.assertIsNone(out[1])

    def test_model_beats_naive_on_sample_data(self):
        data, events = sample_data.generate(days=21, end=datetime(2026, 1, 22), seed=1)
        model = analytics.build_model(data, events)
        self.assertIsNotNone(model)
        self.assertLess(model["mae_60"], model["naive_60"])
        self.assertEqual({c["key"] for c in model["comparison"]}, {"ensemble", "ridge", "holt", "naive"})
        fc = analytics.forecast(data, model, events)
        self.assertEqual(len(fc["points"]), analytics.HORIZON)
        self.assertTrue(all(p["lo"] <= p["v"] <= p["hi"] for p in fc["points"]))
        self.assertTrue(0 <= fc["low_risk"] <= 100)

    def test_ridge_recovers_linear_relationship(self):
        X = [[i % 7, (i * 3) % 11] for i in range(200)]
        y = [2 + 3 * a - 1.5 * b for a, b in X]
        m = analytics._fit_ridge(X, y, lam=1e-6)
        self.assertAlmostEqual(analytics._ridge_apply(m, [4, 5]), 2 + 12 - 7.5, places=3)

    def test_meal_responses_rank_foods(self):
        data, events = sample_data.generate(days=21, end=datetime(2026, 1, 22), seed=2)
        out = recommendations.build(data, events)
        meals = {g["label"]: g["rise"] for g in out["groups"] if g["kind"] == "meal" and g["n"] >= 2}
        self.assertGreater(meals["pasta"], meals["salad"])
        self.assertTrue(any("raises you the most" in r["title"] for r in out["recs"]))

    def test_sparse_data_still_forecasts(self):
        t = datetime(2026, 1, 1, 7)
        data = [(t + timedelta(hours=6 * i), 100 + (i % 4) * 20) for i in range(20)]
        self.assertIsNone(analytics.build_model(data))
        fc = analytics.forecast(data, None)
        self.assertEqual(len(fc["points"]), analytics.HORIZON)
        self.assertIsNone(fc["low_risk"])


class CsvTest(unittest.TestCase):
    def test_dexcom(self):
        text = ("Index,Timestamp (YYYY-MM-DDThh:mm:ss),Event Type,Glucose Value (mg/dL)\n"
                "1,,FirstName,\n2,2026-01-01T08:00:00,EGV,105\n3,2026-01-01T08:05:00,EGV,Low\n")
        rows, skipped = parse_glucose_csv(text)
        self.assertEqual([r[1] for r in rows], [105.0, 40.0])
        self.assertEqual(skipped, 1)

    def test_libre_mmol(self):
        text = ("Glucose Data,Generated on,01-02-2026\n"
                "Device,Serial Number,Device Timestamp,Record Type,Historic Glucose mmol/L,Scan Glucose mmol/L\n"
                "FreeStyle,X,01-01-2026 08:00,0,5.5,\nFreeStyle,X,01-01-2026 08:15,1,,6.0\n")
        rows, _ = parse_glucose_csv(text)
        self.assertEqual([r[1] for r in rows], [99.0, 108.0])
        self.assertEqual(rows[0][0], datetime(2026, 1, 1, 8, 0))


class AppFlowTest(unittest.TestCase):
    def setUp(self):
        self.c = app.test_client()

    def csrf(self, path):
        html = self.c.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def test_full_flow(self):
        tok = self.csrf("/signup")
        r = self.c.post("/signup", data={"csrf": tok, "email": "A@x.com", "password": "password123"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("No readings yet", self.c.get("/dashboard").get_data(as_text=True))
        self.c.post("/sample", data={"csrf": tok})
        html = self.c.get("/dashboard?days=7").get_data(as_text=True)
        self.assertIn("Time in range", html)
        self.assertIn("Chance of going below 70", html)
        self.assertIn("Worth a look", self.c.get("/insights").get_data(as_text=True))
        self.assertIn("in use", self.c.get("/how-it-works").get_data(as_text=True))
        self.c.post("/events", data={"csrf": tok, "ts": "2030-01-01T09:00", "kind": "meal", "note": "Bagel"})
        self.assertIn("Bagel", self.c.get("/readings").get_data(as_text=True))
        self.assertEqual(self.c.post("/events", data={"csrf": tok, "ts": "2030-01-01T09:00", "kind": "bogus"}).status_code, 302)
        self.c.post("/readings", data={"csrf": tok, "ts": "2030-01-01T09:30", "value": "142", "note": "test"})
        self.assertIn("2030-01-01 09:30", self.c.get("/readings").get_data(as_text=True))
        export = self.c.get("/export").get_data(as_text=True)
        self.assertIn("142", export)
        self.assertIn("meal,,Bagel", export)

        # second user can't see first user's data
        c2 = app.test_client()
        tok2 = re.search(r'name="csrf" value="([^"]+)"', c2.get("/signup").get_data(as_text=True)).group(1)
        c2.post("/signup", data={"csrf": tok2, "email": "b@x.com", "password": "password123"})
        self.assertNotIn("2030-01-01", c2.get("/readings").get_data(as_text=True))

    def test_csrf_required(self):
        r = self.c.post("/login", data={"email": "a@x.com", "password": "x"})
        self.assertEqual(r.status_code, 400)

    def test_requires_login(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 302)


if __name__ == "__main__":
    unittest.main()
