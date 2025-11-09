# server.py
import os
import time
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from threading import Thread, Event
from flask import Flask, jsonify, send_file, request, Response
from flask_cors import CORS
import csv
from io import StringIO

# ---------- Configuration ----------
DB_PATH = os.environ.get("DB_PATH", "iss_data.db")
API_URL = os.environ.get("ISS_API_URL", "https://api.wheretheiss.at/v1/satellites/25544")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL_SEC", "60"))  # default 1 minute
MAX_RETENTION_DAYS = int(os.environ.get("MAX_RETENTION_DAYS", "3"))
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
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        altitude REAL,
        timestamp TEXT NOT NULL,
        day TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
      )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON iss_positions(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_day ON iss_positions(day)")
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)

def save_position(latitude, longitude, altitude, ts_utc):
    day = ts_utc.split(" ")[0]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
      VALUES (?, ?, ?, ?, ?)
    """, (latitude, longitude, altitude, ts_utc, day))
    conn.commit()
    conn.close()

def cleanup_old_data():
    cutoff = (datetime.utcnow() - timedelta(days=MAX_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM iss_positions WHERE timestamp < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info("Cleaned up %d old records older than %s", deleted, cutoff)

def get_record_count():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM iss_positions")
    count = cur.fetchone()[0]
    conn.close()
    return count

# ---------- Fetching ISS ----------
def parse_wther_resp(data):
    ts = datetime.utcfromtimestamp(int(data.get("timestamp", time.time())))
    return {
        "latitude": float(data.get("latitude")),
        "longitude": float(data.get("longitude")),
        "altitude": float(data.get("altitude", 0.0)),
        "ts_utc": ts.strftime("%Y-%m-%d %H:%M:%S")
    }

def parse_open_notify(data):
    ts = datetime.utcfromtimestamp(int(data.get("timestamp", time.time())))
    pos = data.get("iss_position", {})
    return {
        "latitude": float(pos.get("latitude")),
        "longitude": float(pos.get("longitude")),
        "altitude": None,
        "ts_utc": ts.strftime("%Y-%m-%d %H:%M:%S")
    }

def fetch_iss_position():
    try:
        resp = requests.get(API_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("iss_position"):
            return parse_open_notify(data)
        else:
            return parse_wther_resp(data)
    except Exception as e:
        logger.warning("Fetch error: %s", e)
        return None

# ---------- Background collection ----------
stop_event = Event()
def background_loop():
    cleanup_counter = 0
    while not stop_event.is_set():
        pos = fetch_iss_position()
        if pos:
            save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
        cleanup_counter += 1
        if cleanup_counter >= max(1, int(3600 / max(1, FETCH_INTERVAL))):
            cleanup_old_data()
            cleanup_counter = 0
        stop_event.wait(FETCH_INTERVAL)

# ---------- CSV export ----------
def generate_csv(rows):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "latitude", "longitude", "altitude", "timestamp", "day"])
    for r in rows:
        writer.writerow([r["id"], r["latitude"], r["longitude"], r["altitude"], r["ts_utc"], r["day"]])
    output.seek(0)
    return output

@app.route("/api/download")
def download_csv():
    day_filter = request.args.get("day")  # optional, e.g., ?day=2025-11-09
    conn = get_conn()
    cur = conn.cursor()
    if day_filter:
        cur.execute("""
            SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day
            FROM iss_positions
            WHERE day = ?
            ORDER BY timestamp ASC
        """, (day_filter,))
    else:
        cutoff = (datetime.utcnow() - timedelta(days=MAX_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day
            FROM iss_positions
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (cutoff,))
    rows = cur.fetchall()
    conn.close()
    csv_file = generate_csv(rows)
    filename = f"iss_positions_{day_filter or 'last3days'}.csv"
    return Response(
        csv_file,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

# ---------- Other API endpoints remain unchanged ----------
# ... you can keep all /api/current, /api/all-records, /api/stats, etc.

# ---------- startup ----------
if __name__ == "__main__":
    logger.info("Starting ISS Tracker (DB=%s) FETCH_INTERVAL=%ss", DB_PATH, FETCH_INTERVAL)
    init_database()

    if SAMPLE_DATA and get_record_count() == 0:
        now = datetime.utcnow()
        conn = get_conn()
        cur = conn.cursor()
        logger.info("Generating sample data (1000 records)...")
        for i in range(1000):
            tp = now - timedelta(seconds=i*60)  # 1 record per minute
            cur.execute("""
              INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
              VALUES (?, ?, ?, ?, ?)
            """, (45.0 + (i % 180) - 90, -180.0 + (i * 0.72) % 360, 408.0 + (i % 20) * 0.3, tp.strftime("%Y-%m-%d %H:%M:%S"), tp.strftime("%Y-%m-%d")))
        conn.commit()
        conn.close()
        logger.info("Sample data generated")

    t = Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
