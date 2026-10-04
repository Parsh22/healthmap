# HealthMap

A small web app that helps people with diabetes visualize their blood sugar, forecast where it's heading, and spot patterns.

- **Log**: glucose readings by hand or by CSV import (Dexcom Clarity, FreeStyle Libre, or any `timestamp,glucose` CSV), plus meals, activity, and insulin.
- **Dashboard**:
  - Time in range, average, GMI (estimated A1C), CV (variability), and % below 70, each compared with last week
  - The last 24 hours with event markers, a 2-hour forecast with an uncertainty band, and an estimated chance of going low
  - Daily-pattern and time-in-range-by-day charts
  - Printable report
- **Forecast**: four methods compete on each user's own history:
  - Personal ARX-style ridge regression
  - Damped-trend Holt smoothing blended toward the user's daily rhythm
  - An average of the two
  - A "no change" baseline

  They're trained, selected, and scored on a time-ordered 60/20/20 split.
- **Insights**: suggestions based on your readings and events, a table of how each food or activity affects you, and time in range by weekday.
- **How it works**: a public page explaining the math, with each user's own model scoreboard.

Built with Flask, SQLite and Chart.js. The only Python dependencies are Flask and gunicorn.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py              # http://localhost:5000
.venv/bin/python -m unittest tests/test_app.py -v
```

## Deploy on PythonAnywhere (free)

**One-time setup**
1. Create a free "Beginner" account at pythonanywhere.com. Your site will be at `https://<username>.pythonanywhere.com`.
2. Build the upload bundle locally:
   ```bash
   zip -qr healthmap.zip . -x '.venv/*' 'data/*' '*__pycache__*' '*.pyc' 'healthmap.zip' '.DS_Store'
   ```
3. On the **Files** tab, upload `healthmap.zip` to your home directory (`/home/<username>/`).
4. On the **Consoles** tab, start a **Bash** console and run:
   ```bash
   mkdir -p ~/health-map && cd ~/health-map && unzip -o ~/healthmap.zip
   python3.12 -m venv ~/.virtualenvs/healthmap
   ~/.virtualenvs/healthmap/bin/pip install -r requirements.txt
   ```
5. On the **Web** tab, click **Add a new web app**, choose **Manual configuration** (not the "Flask" option), then **Python 3.12**.
6. Still on the Web tab:
   - **Virtualenv:** `/home/<username>/.virtualenvs/healthmap`
   - **WSGI configuration file:** click the link and replace its contents with `pythonanywhere_wsgi.py` from this repo.
   - **Static files:** URL `/static/` maps to directory `/home/<username>/health-map/static`.
   - **Security:** turn on **Force HTTPS**.
7. Click **Reload**, then open your site.

The secret key is generated automatically on first start and saved in `data/secret_key`. The database lives at `data/healthmap.db`.

**Running it for 6 months**

| Task | How |
|------|-----|
| Keep it online | Free apps get switched off unless you renew them. Click **"Run until … from today"** on the Web tab before the date shown there. Set a calendar reminder. |
| Deploy a code change | Rebuild the zip, upload it over the old one, run `cd ~/health-map && unzip -o ~/healthmap.zip`, then **Reload** on the Web tab. Your data is never in the zip, so it's untouched. |
| See errors | Web tab, **Error log** link |
| Back up the database | In a Bash console run `cd ~/health-map && python3 -c "import sqlite3; s=sqlite3.connect('data/healthmap.db'); d=sqlite3.connect('data/backup.db'); s.backup(d)"`, then download `data/backup.db` from the Files tab |
| Restrict signups later | Uncomment the `INVITE_CODE` line in the WSGI file, then **Reload** |
| Shut down | Web tab, **Delete**. Also delete `~/health-map` to remove all data. |

The free plan has a daily CPU allowance, which is plenty for 10–20 users. The site doesn't need outbound internet access (charts load from a CDN in the user's browser), so the free plan's network restrictions don't matter.

## Alternative: Fly.io (about $2–5/month)

`Dockerfile` and `fly.toml` are included. Run `fly apps create <name>`, set `app` in `fly.toml`, then run `fly volumes create healthmap_data --size 1 --region iad` and `fly deploy`.

## Notes

- Timestamps are stored as local wall-clock time, matching CGM exports. Forecasts start from the latest reading, so the server's timezone doesn't matter.
- This is not a medical device and its output is not medical advice. Every page says so.
