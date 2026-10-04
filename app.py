import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import analytics
import recommendations
import sample_data

TS_FMT = "%Y-%m-%d %H:%M:%S"

DATABASE = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "healthmap.db"))


def load_secret_key():
    """Use SECRET_KEY if set; otherwise persist a generated key next to the database so sessions survive restarts."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(os.path.dirname(DATABASE), "secret_key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    DATABASE=DATABASE,
    INVITE_CODE=os.environ.get("INVITE_CODE", ""),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    UNIQUE (user_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_user_ts ON readings(user_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts);
"""
EVENT_KINDS = ("meal", "exercise", "insulin", "other")


def connect():
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


_attempts = {}
RATE_LIMIT, RATE_WINDOW = 10, 300  # per IP, per 5 minutes; in-memory is fine with a single worker


@app.before_request
def rate_limit_auth():
    if request.method != "POST" or request.endpoint not in ("login", "signup"):
        return
    # Hosting proxies (PythonAnywhere, Fly) put the real client IP in these headers.
    ip = request.headers.get("X-Real-IP") or request.headers.get("Fly-Client-IP") or request.remote_addr
    now = datetime.now().timestamp()
    recent = [t for t in _attempts.get(ip, []) if now - t < RATE_WINDOW]
    if len(recent) >= RATE_LIMIT:
        abort(429, "Too many attempts. Please wait a few minutes and try again.")
    recent.append(now)
    _attempts[ip] = recent


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf")
        if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
            abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf"], "user_email": session.get("email"), "current_year": datetime.now().year}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def start_session(user):
    csrf = session.get("csrf")
    session.clear()
    session.permanent = True
    session.update(user_id=user["id"], email=user["email"], csrf=csrf or secrets.token_hex(16))


def insert_readings(user_id, rows, source):
    cur = db().executemany(
        "INSERT OR IGNORE INTO readings (user_id, ts, value, note, source) VALUES (?, ?, ?, ?, ?)",
        [(user_id, t.strftime(TS_FMT), v, note, source) for t, v, note in rows],
    )
    db().commit()
    return cur.rowcount


def load_readings(user_id, days=None):
    if days is None:
        rows = db().execute("SELECT ts, value FROM readings WHERE user_id = ? ORDER BY ts", (user_id,)).fetchall()
    else:
        last = db().execute("SELECT MAX(ts) FROM readings WHERE user_id = ?", (user_id,)).fetchone()[0]
        if not last:
            return []
        since = (datetime.strptime(last, TS_FMT) - timedelta(days=days)).strftime(TS_FMT)
        rows = db().execute("SELECT ts, value FROM readings WHERE user_id = ? AND ts > ? ORDER BY ts",
                            (user_id, since)).fetchall()
    return [(datetime.strptime(r["ts"], TS_FMT), r["value"]) for r in rows]


def insert_events(user_id, rows):
    db().executemany("INSERT INTO events (user_id, ts, kind, note) VALUES (?, ?, ?, ?)",
                     [(user_id, t.strftime(TS_FMT), kind, note) for t, kind, note in rows])
    db().commit()


def load_events(user_id):
    """Logged events plus notes attached to readings (treated as 'other' events)."""
    rows = db().execute(
        "SELECT ts, kind, note FROM events WHERE user_id = ? UNION ALL "
        "SELECT ts, 'other', note FROM readings WHERE user_id = ? AND note IS NOT NULL ORDER BY ts",
        (user_id, user_id)).fetchall()
    return [(datetime.strptime(r["ts"], TS_FMT), r["kind"], r["note"]) for r in rows]


# ---------- CSV parsing ----------

TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
              "%m-%d-%Y %I:%M %p", "%m-%d-%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
              "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M")


def parse_ts(s):
    s = s.strip()
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def parse_glucose_csv(text):
    """Supports generic `timestamp,glucose` files plus Dexcom Clarity and FreeStyle Libre exports."""
    rows = list(csv.reader(io.StringIO(text)))
    for hi, header in enumerate(rows[:5]):
        lower = [c.strip().lower() for c in header]
        ts_idx = next((i for i, c in enumerate(lower) if any(k in c for k in ("timestamp", "time", "date"))), None)
        glucose_cols = [i for i, c in enumerate(lower) if "glucose" in c or c in ("value", "bg", "sgv", "mg/dl")]
        glucose_cols.sort(key=lambda i: (0 if "historic" in lower[i] else 1 if "scan" in lower[i] else 2))
        if ts_idx is not None and glucose_cols:
            break
    else:
        raise ValueError("Couldn't find timestamp and glucose columns in the first rows of the file.")

    out, skipped = [], 0
    for row in rows[hi + 1:]:
        if len(row) <= ts_idx:
            continue
        t = parse_ts(row[ts_idx])
        val = None
        for i in glucose_cols:
            raw = row[i].strip() if i < len(row) else ""
            if not raw:
                continue
            if raw.lower() == "low":
                val = 40.0
            elif raw.lower() == "high":
                val = 400.0
            else:
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if "mmol" in lower[i]:
                    val *= 18.0
            break
        if t is None or val is None or not 20 <= val <= 600:
            skipped += 1
            continue
        out.append((t, round(val, 1), None))
    return out, skipped


# ---------- routes ----------

@app.route("/")
def index():
    return redirect(url_for("dashboard" if "user_id" in session else "login"))


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        invite = app.config["INVITE_CODE"]
        if invite and not secrets.compare_digest(invite, request.form.get("invite", "").strip()):
            flash("Invalid invite code.", "error")
        elif "@" not in email or len(email) > 254:
            flash("Enter a valid email.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            try:
                cur = db().execute("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                                   (email, generate_password_hash(password), datetime.now(timezone.utc).strftime(TS_FMT)))
                db().commit()
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
            else:
                start_session({"id": cur.lastrowid, "email": email})
                return redirect(url_for("dashboard"))
    return render_template("signup.html", invite_required=bool(app.config["INVITE_CODE"]))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            start_session(user)
            return redirect(url_for("dashboard"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    days = request.args.get("days", 14, type=int)
    if days not in (7, 14, 30, 90):
        days = 14
    history = load_readings(session["user_id"], days=max(days, 30))
    if not history:
        return render_template("dashboard.html", empty=True, days=days)

    last_t = history[-1][0]
    period = [r for r in history if r[0] > last_t - timedelta(days=days)]
    recent = [r for r in history if r[0] > last_t - timedelta(hours=24)]
    events = load_events(session["user_id"])
    model = analytics.build_model(history, events)
    fc = analytics.forecast(history, model, events)
    stats = analytics.summary(period)
    profile = analytics.hourly_profile(period)
    recent_events = [e for e in events if recent[0][0] <= e[0] <= last_t]
    ev_vals = analytics.resample(recent, [e[0] for e in recent_events])
    payload = {
        "recent": [{"t": t.isoformat(), "v": v} for t, v in recent],
        "forecast": fc["points"],
        "events": [{"t": e[0].isoformat(), "kind": e[1], "note": e[2] or e[1], "v": round(v)}
                   for e, v in zip(recent_events, ev_vals) if v is not None],
        "profile": profile,
        "daily": analytics.daily_breakdown(period),
    }
    return render_template(
        "dashboard.html", empty=False, days=days, stats=stats, model=model, payload=payload, fc=fc,
        last_t=last_t, last_v=round(history[-1][1]), trend=analytics.trend(history),
        wow=analytics.week_over_week(history), insights=analytics.insights(stats, profile, period),
    )


@app.route("/insights")
@login_required
def insights():
    history = load_readings(session["user_id"], days=90)
    return render_template("insights.html", **recommendations.build(history, load_events(session["user_id"])))


@app.route("/how-it-works")
def how_it_works():
    model = None
    if "user_id" in session:
        history = load_readings(session["user_id"], days=30)
        model = analytics.build_model(history, load_events(session["user_id"])) if history else None
    return render_template("how.html", model=model)


@app.route("/readings", methods=["GET", "POST"])
@login_required
def readings():
    uid = session["user_id"]
    if request.method == "POST":
        t = parse_ts(request.form.get("ts", "").replace("T", " "))
        try:
            v = float(request.form.get("value", ""))
        except ValueError:
            v = None
        note = request.form.get("note", "").strip()[:200] or None
        if t is None or v is None or not 20 <= v <= 600:
            flash("Enter a valid time and a glucose value between 20 and 600 mg/dL.", "error")
        elif insert_readings(uid, [(t, v, note)], "manual"):
            flash("Reading saved.", "ok")
        else:
            flash("A reading already exists at that exact time.", "error")
        return redirect(url_for("readings"))

    rows = db().execute("SELECT id, ts, value, note, source FROM readings WHERE user_id = ? ORDER BY ts DESC LIMIT 300",
                        (uid,)).fetchall()
    total = db().execute("SELECT COUNT(*) FROM readings WHERE user_id = ?", (uid,)).fetchone()[0]
    events = db().execute("SELECT id, ts, kind, note FROM events WHERE user_id = ? ORDER BY ts DESC LIMIT 100",
                          (uid,)).fetchall()
    return render_template("readings.html", rows=rows, total=total, events=events, kinds=EVENT_KINDS)


@app.post("/events")
@login_required
def add_event():
    t = parse_ts(request.form.get("ts", "").replace("T", " "))
    kind = request.form.get("kind", "")
    note = request.form.get("note", "").strip()[:100] or None
    if t is None or kind not in EVENT_KINDS:
        flash("Enter a valid time and event type.", "error")
    else:
        insert_events(session["user_id"], [(t, kind, note)])
        flash(f"{kind.capitalize()} logged.", "ok")
    return redirect(url_for("readings"))


@app.post("/events/<int:eid>/delete")
@login_required
def delete_event(eid):
    db().execute("DELETE FROM events WHERE id = ? AND user_id = ?", (eid, session["user_id"]))
    db().commit()
    return redirect(url_for("readings"))


@app.post("/readings/<int:rid>/delete")
@login_required
def delete_reading(rid):
    db().execute("DELETE FROM readings WHERE id = ? AND user_id = ?", (rid, session["user_id"]))
    db().commit()
    return redirect(url_for("readings"))


@app.post("/readings/delete-all")
@login_required
def delete_all_readings():
    db().execute("DELETE FROM readings WHERE user_id = ?", (session["user_id"],))
    db().execute("DELETE FROM events WHERE user_id = ?", (session["user_id"],))
    db().commit()
    flash("All readings and events deleted.", "ok")
    return redirect(url_for("readings"))


@app.post("/import")
@login_required
def import_csv():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("readings"))
    try:
        rows, skipped = parse_glucose_csv(f.read().decode("utf-8-sig", errors="replace"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("readings"))
    added = insert_readings(session["user_id"], rows, "import")
    flash(f"Imported {added} readings ({len(rows) - added} duplicates, {skipped} rows skipped).", "ok")
    return redirect(url_for("dashboard"))


@app.post("/sample")
@login_required
def load_sample():
    readings_, events = sample_data.generate(days=14)
    added = insert_readings(session["user_id"], [(t, v, None) for t, v in readings_], "sample")
    insert_events(session["user_id"], events)
    flash(f"Loaded {added} sample readings and {len(events)} meal/exercise events (14 days of simulated data).", "ok")
    return redirect(url_for("dashboard"))


@app.route("/export")
@login_required
def export_csv():
    rows = db().execute(
        "SELECT ts, 'reading', value, note, source FROM readings WHERE user_id = ? UNION ALL "
        "SELECT ts, kind, NULL, note, 'event' FROM events WHERE user_id = ? ORDER BY 1",
        (session["user_id"], session["user_id"])).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "type", "glucose_mg_dl", "note", "source"])
    w.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=healthmap-readings.csv"})


@app.post("/account/delete")
@login_required
def delete_account():
    user = db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
        flash("Incorrect password; account not deleted.", "error")
        return redirect(url_for("readings"))
    db().execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db().commit()
    session.clear()
    flash("Your account and all data were deleted.", "ok")
    return redirect(url_for("login"))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
