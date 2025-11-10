# iss_tracker_combined.py
import os
import time
import sqlite3
import logging
import requests
import csv
import io
from datetime import datetime, timedelta
from threading import Thread, Event
from flask import Flask, jsonify, send_file, request, Response, send_from_directory
from flask_cors import CORS

# Force UTC timezone for consistency
os.environ['TZ'] = 'UTC'
if hasattr(time, 'tzset'):
    time.tzset()

# ---------- Configuration (env override) ----------
DB_PATH = os.environ.get("DB_PATH", "iss_data.db")
CSV_FILE = os.environ.get("CSV_FILE", "iss_data.csv")
API_URL = os.environ.get("ISS_API_URL", "https://api.wheretheiss.at/v1/satellites/25544")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL_SEC", "1"))
SAMPLE_DATA = os.environ.get("SAMPLE_DATA", "0") == "1"
PORT = int(os.environ.get("PORT", "10000"))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("iss-tracker")

# ---------- Flask app ----------
app = Flask(__name__, static_folder=".")
CORS(app)

# ---------- DB utilities ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS iss_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_unix INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        altitude REAL,
        velocity REAL,
        day TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
      )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp_unix ON iss_positions(timestamp_unix)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_day ON iss_positions(day)")
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)

def save_db_record(unix_ts, ts_text, latitude, longitude, altitude, velocity):
    day = ts_text.split(" ")[0]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO iss_positions (timestamp_unix, timestamp, latitude, longitude, altitude, velocity, day)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (unix_ts, ts_text, latitude, longitude, altitude, velocity, day))
    conn.commit()
    conn.close()

def get_record_count():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM iss_positions")
    count = cur.fetchone()[0]
    conn.close()
    return count

# ---------- CSV utilities ----------
def ensure_csv_exists():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            # unix_ts, ts_utc, latitude, longitude, altitude, velocity
            writer.writerow(['timestamp_unix', 'ts_utc', 'latitude', 'longitude', 'altitude_km', 'velocity_km_s'])

def append_csv(unix_ts, ts_text, latitude, longitude, altitude, velocity):
    ensure_csv_exists()
    with open(CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([unix_ts, ts_text, latitude, longitude, altitude, velocity])

# ---------- Parse API responses ----------
def parse_wther_resp(data):
    # wheretheiss.at style response: timestamp, latitude, longitude, altitude, velocity
    ts_unix = int(data.get("timestamp", int(time.time())))
    ts = datetime.utcfromtimestamp(ts_unix)
    return {
        "timestamp_unix": ts_unix,
        "ts_utc": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "latitude": float(data.get("latitude")),
        "longitude": float(data.get("longitude")),
        "altitude": float(data.get("altitude", 0.0)),  # km
        "velocity": float(data.get("velocity", 0.0))   # km/s
    }

def parse_open_notify(data):
    ts_unix = int(data.get("timestamp", int(time.time())))
    ts = datetime.utcfromtimestamp(ts_unix)
    pos = data.get("iss_position", {})
    return {
        "timestamp_unix": ts_unix,
        "ts_utc": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "latitude": float(pos.get("latitude")),
        "longitude": float(pos.get("longitude")),
        "altitude": None,
        "velocity": None
    }

def fetch_iss_position_once():
    try:
        resp = requests.get(API_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        # detect type
        if isinstance(data, dict) and data.get("iss_position"):
            return parse_open_notify(data)
        else:
            return parse_wther_resp(data)
    except Exception as e:
        logger.warning("Fetch error: %s", e)
        return None

# ---------- Background collector ----------
stop_event = Event()

def background_loop():
    logger.info("Background fetcher started (interval %ss)", FETCH_INTERVAL)
    while not stop_event.is_set():
        pos = fetch_iss_position_once()
        if pos:
            try:
                # Save to DB
                save_db_record(
                    pos["timestamp_unix"],
                    pos["ts_utc"],
                    pos["latitude"],
                    pos["longitude"],
                    pos.get("altitude"),
                    pos.get("velocity")
                )
            except Exception as e:
                logger.exception("Error saving DB record: %s", e)
            try:
                # Append to CSV
                append_csv(
                    pos["timestamp_unix"],
                    pos["ts_utc"],
                    pos["latitude"],
                    pos["longitude"],
                    pos.get("altitude"),
                    pos.get("velocity")
                )
            except Exception as e:
                logger.exception("Error appending CSV: %s", e)

            # occasional logging
            count = get_record_count()
            if count and count % 3600 == 0:
                logger.info("Collected %d records (~%0.2f days)", count, count / 86400.0)
        stop_event.wait(FETCH_INTERVAL)

# ---------- API endpoints ----------
@app.route("/")
def index():
    try:
        return send_file("index.html")
    except Exception:
        return "ISS Tracker API", 200

@app.route("/database")
def database_view():
    try:
        return send_file("database.html")
    except Exception:
        return "Database viewer not found", 404

@app.route("/api/current")
def api_current():
    pos = fetch_iss_position_once()
    if pos:
        # store but don't block on failure
        try:
            save_db_record(
                pos["timestamp_unix"],
                pos["ts_utc"],
                pos["latitude"],
                pos["longitude"],
                pos.get("altitude"),
                pos.get("velocity")
            )
            append_csv(
                pos["timestamp_unix"],
                pos["ts_utc"],
                pos["latitude"],
                pos["longitude"],
                pos.get("altitude"),
                pos.get("velocity")
            )
        except Exception:
            pass
        return jsonify(pos)
    # fallback to last saved
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp_unix, timestamp AS ts_utc, latitude, longitude, altitude, velocity, day
        FROM iss_positions
        ORDER BY timestamp_unix DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({
            "timestamp_unix": row["timestamp_unix"],
            "ts_utc": row["ts_utc"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "altitude": row["altitude"],
            "velocity": row["velocity"],
            "day": row["day"]
        })
    return jsonify({"error": "No data available"}), 404

@app.route("/api/preview")
def api_preview():
    """
    Returns recent records for the dashboard - supports:
      - limit to last N (default 500)
      - day_index param: 0-based index relative to first record's day (like the CSV example)
    """
    try:
        limit = min(5000, int(request.args.get("limit", 500)))
        day_index = int(request.args.get("day_index", None)) if request.args.get("day_index") is not None else None

        conn = get_conn()
        cur = conn.cursor()

        if day_index is None:
            # return last `limit` records in chronological order
            cur.execute("""
              SELECT timestamp_unix, timestamp AS ts_utc, latitude, longitude, altitude, velocity, day
              FROM iss_positions
              ORDER BY timestamp_unix DESC
              LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            conn.close()
            records = [{
                "timestamp_unix": r["timestamp_unix"],
                "ts_utc": r["ts_utc"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "altitude": r["altitude"],
                "velocity": r["velocity"],
                "day": r["day"]
            } for r in reversed(rows)]
            return jsonify({"records": records})
        else:
            # compute start_of_day based on the earliest record's day + day_index
            cur.execute("SELECT MIN(timestamp_unix) as first_ts FROM iss_positions")
            first_row = cur.fetchone()
            if not first_row or not first_row["first_ts"]:
                conn.close()
                return jsonify({"records": []})
            first_day_start = datetime.utcfromtimestamp(first_row["first_ts"]).replace(hour=0, minute=0, second=0, microsecond=0)
            start_of_day = first_day_start + timedelta(days=day_index)
            end_of_day = start_of_day + timedelta(days=1)
            start_unix = int(start_of_day.timestamp())
            end_unix = int(end_of_day.timestamp())
            cur.execute("""
              SELECT timestamp_unix, timestamp AS ts_utc, latitude, longitude, altitude, velocity, day
              FROM iss_positions
              WHERE timestamp_unix >= ? AND timestamp_unix < ?
              ORDER BY timestamp_unix ASC
              LIMIT ?
            """, (start_unix, end_unix, limit))
            rows = cur.fetchall()
            conn.close()
            records = [{
                "timestamp_unix": r["timestamp_unix"],
                "ts_utc": r["ts_utc"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "altitude": r["altitude"],
                "velocity": r["velocity"],
                "day": r["day"]
            } for r in rows]
            return jsonify({"records": records})
    except Exception as e:
        logger.exception("Error in /api/preview: %s", e)
        return jsonify({"error": "Unable to fetch preview data"}), 500

@app.route("/api/all-records")
def api_all_records():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(5000, max(1, int(request.args.get("per_page", 1000))))
        day_filter = request.args.get("day", None)
        conn = get_conn()
        cur = conn.cursor()

        if day_filter:
            cur.execute("SELECT COUNT(*) FROM iss_positions WHERE day = ?", (day_filter,))
            total = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day DESC")
            days = [r["day"] for r in cur.fetchall()]
            cur.execute("""
              SELECT id, timestamp_unix, timestamp AS ts_utc, latitude, longitude, altitude, velocity, day
              FROM iss_positions
              WHERE day = ?
              ORDER BY timestamp_unix DESC
              LIMIT ? OFFSET ?
            """, (day_filter, per_page, (page - 1) * per_page))
        else:
            cur.execute("SELECT COUNT(*) FROM iss_positions")
            total = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day DESC")
            days = [r["day"] for r in cur.fetchall()]
            cur.execute("""
              SELECT id, timestamp_unix, timestamp AS ts_utc, latitude, longitude, altitude, velocity, day
              FROM iss_positions
              ORDER BY timestamp_unix DESC
              LIMIT ? OFFSET ?
            """, (per_page, (page - 1) * per_page))

        rows = cur.fetchall()
        conn.close()

        records = [{
            "id": r["id"],
            "timestamp_unix": r["timestamp_unix"],
            "ts_utc": r["ts_utc"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "altitude": r["altitude"],
            "velocity": r["velocity"],
            "day": r["day"]
        } for r in rows]

        total_pages = (total + per_page - 1) // per_page if total else 1
        return jsonify({
            "records": records,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "available_days": days
        })
    except Exception as e:
        logger.exception("Error in /api/all-records: %s", e)
        return jsonify({"error": "Unable to fetch records"}), 500

@app.route("/api/download")
def api_download_csv():
    """Generate CSV from DB and return as attachment (fresh snapshot)."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
          SELECT id, timestamp_unix, timestamp, latitude, longitude, altitude, velocity, day, created_at
          FROM iss_positions
          ORDER BY timestamp_unix ASC
        """)
        rows = cur.fetchall()
        conn.close()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'timestamp_unix', 'ts_utc', 'Latitude', 'Longitude', 'Altitude_km', 'Velocity_km_s', 'Day', 'Created At'])
        for row in rows:
            writer.writerow([
                row["id"],
                row["timestamp_unix"],
                row["timestamp"],
                row["latitude"],
                row["longitude"],
                row["altitude"],
                row["velocity"],
                row["day"],
                row["created_at"]
            ])
        output.seek(0)
        filename = f"iss_positions_db_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={filename}'})
    except Exception as e:
        logger.exception("Error in /api/download: %s", e)
        return jsonify({"error": "Unable to generate CSV"}), 500

@app.route("/csv/latest")
def download_raw_csv():
    """Return the rolling CSV file appended by the collector (if exists)."""
    if os.path.exists(CSV_FILE):
        return send_from_directory('.', CSV_FILE, as_attachment=True)
    return "CSV file not found", 404

@app.route("/api/stats")
def api_stats():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM iss_positions")
        total = cur.fetchone()[0]
        cur.execute("SELECT day, COUNT(*) AS cnt FROM iss_positions GROUP BY day ORDER BY day DESC")
        per_day = {r["day"]: r["cnt"] for r in cur.fetchall()}
        cur.execute("SELECT MIN(timestamp) as first, MAX(timestamp) as last FROM iss_positions")
        date_range = cur.fetchone()
        conn.close()

        total_hours = total / 3600.0 if total else 0.0
        total_days = total_hours / 24.0 if total else 0.0

        return jsonify({
            "total_records": total,
            "total_hours": round(total_hours, 2),
            "total_days": round(total_days, 2),
            "records_per_day": per_day,
            "collection_interval_seconds": FETCH_INTERVAL,
            "first_record": date_range["first"] if date_range["first"] else None,
            "last_record": date_range["last"] if date_range["last"] else None
        })
    except Exception as e:
        logger.exception("Error in /api/stats: %s", e)
        return jsonify({"error": "Unable to fetch stats"}), 500

# Serve other static files if present
@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

# ---------- Start up ----------
if __name__ == "__main__":
    logger.info("Starting ISS Tracker (DB=%s CSV=%s) FETCH_INTERVAL=%ss", DB_PATH, CSV_FILE, FETCH_INTERVAL)
    init_database()
    ensure_csv_exists()

    # Optionally generate sample data
    if SAMPLE_DATA and get_record_count() == 0:
        now = datetime.utcnow().replace(microsecond=0)
        conn = get_conn()
        cur = conn.cursor()
        logger.info("Generating sample data (1000 records)...")
        for i in range(1000):
            tp = now - timedelta(seconds=i)
            unix_ts = int(tp.timestamp())
            cur.execute("""
              INSERT INTO iss_positions (timestamp_unix, timestamp, latitude, longitude, altitude, velocity, day)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (unix_ts, tp.strftime("%Y-%m-%d %H:%M:%S"), 45.0 + (i % 180) - 90, -180.0 + (i * 0.72) % 360, 408.0 + (i % 20) * 0.3, 7.66, tp.strftime("%Y-%m-%d")))
            append_csv(unix_ts, tp.strftime("%Y-%m-%d %H:%M:%S"), 45.0 + (i % 180) - 90, -180.0 + (i * 0.72) % 360, 408.0 + (i % 20) * 0.3, 7.66)
        conn.commit()
        conn.close()
        logger.info("Sample data generated")

    # start background collector thread (daemon)
    t = Thread(target=background_loop, daemon=True)
    t.start()

    try:
        app.run(host="0.0.0.0", port=PORT, debug=False)
    finally:
        stop_event.set()
        logger.info("Shutting down background fetcher")
