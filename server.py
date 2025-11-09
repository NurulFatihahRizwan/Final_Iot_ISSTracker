#!/usr/bin/env python3
import os
import time
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from threading import Thread, Event
from flask import Flask, jsonify, send_file, request, Response, stream_with_context
from flask_cors import CORS
import csv
import io

# ---------- Configuration ----------
DB_PATH = os.environ.get("DB_PATH", "iss_data.db")
API_URL = os.environ.get("ISS_API_URL", "https://api.wheretheiss.at/v1/satellites/25544")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL_SEC", "60"))  # seconds
MAX_RETENTION_DAYS = int(os.environ.get("MAX_RETENTION_DAYS", "3"))
SAMPLE_DATA = os.environ.get("SAMPLE_DATA", "0") == "1"
PORT = int(os.environ.get("PORT", "10000"))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ISS-Tracker")

# ---------- Flask app ----------
app = Flask(__name__, static_folder=".")
CORS(app)

# ---------- Database ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_day ON iss_positions(day)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON iss_positions(timestamp)")
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)

def save_position(lat, lon, alt, ts_utc):
    day = ts_utc.split(" ")[0]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
        VALUES (?, ?, ?, ?, ?)
    """, (lat, lon, alt, ts_utc, day))
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

# ---------- Fetch ISS ----------
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

# ---------- Background collector ----------
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

# ---------- Routes ----------
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/database")
def database_view():
    return send_file("database.html")

@app.route("/api/current")
def api_current():
    pos = fetch_iss_position()
    if pos:
        save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
        return jsonify(pos)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT latitude, longitude, altitude, timestamp AS ts_utc, day FROM iss_positions ORDER BY timestamp DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({"error": "No data"}), 404

@app.route("/api/last3days")
def api_last3days():
    conn = get_conn()
    cur = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=MAX_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("SELECT latitude, longitude, altitude, timestamp AS ts_utc, day FROM iss_positions WHERE timestamp >= ? ORDER BY timestamp ASC", (cutoff,))
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/all-records")
def api_all_records():
    try:
        per_page = int(request.args.get("per_page", 1000))
        day_filter = request.args.get("day")
        conn = get_conn()
        cur = conn.cursor()
        if day_filter:
            cur.execute("SELECT * FROM iss_positions WHERE day=? ORDER BY timestamp ASC LIMIT ?", (day_filter, per_page))
        else:
            cur.execute("SELECT * FROM iss_positions ORDER BY timestamp ASC LIMIT ?", (per_page,))
        rows = cur.fetchall()
        cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day ASC")
        available_days = [r["day"] for r in cur.fetchall()]
        conn.close()
        return jsonify({
            "records": [dict(r) for r in rows],
            "available_days": available_days
        })
    except Exception as e:
        logger.exception("Error fetching records: %s", e)
        return jsonify({"error": "Unable to fetch records"}), 500

@app.route("/api/download-csv")
def api_download_csv():
    day = request.args.get("day")
    all_days = request.args.get("all") == "1"

    conn = get_conn()
    cur = conn.cursor()
    if all_days:
        cur.execute("SELECT * FROM iss_positions ORDER BY timestamp ASC")
    elif day:
        cur.execute("SELECT * FROM iss_positions WHERE day=? ORDER BY timestamp ASC", (day,))
    else:
        cur.execute("SELECT * FROM iss_positions ORDER BY timestamp ASC")
    rows = cur.fetchall()
    conn.close()

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID","Timestamp","Day","Latitude","Longitude","Altitude"])
        for r in rows:
            writer.writerow([r["id"], r["timestamp"], r["day"], r["latitude"], r["longitude"], r["altitude"]])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    filename = "iss_data.csv" if all_days else f"iss_day_{day}.csv"
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={filename}"})

# ---------- Startup ----------
if __name__ == "__main__":
    logger.info("Starting ISS Tracker (DB=%s) FETCH_INTERVAL=%ss", DB_PATH, FETCH_INTERVAL)
    init_db()

    # --- Immediately fetch first record ---
    first_pos = fetch_iss_position()
    if first_pos:
        save_position(first_pos["latitude"], first_pos["longitude"], first_pos["altitude"], first_pos["ts_utc"])
        logger.info("Saved first ISS record: %s", first_pos)

    # --- Sample data if requested ---
    if SAMPLE_DATA and get_record_count() == 0:
        now = datetime.utcnow()
        conn = get_conn()
        cur = conn.cursor()
        logger.info("Generating sample data (1000 records)...")
        for i in range(1000):
            tp = now - timedelta(seconds=i)
            cur.execute("""
                INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
                VALUES (?, ?, ?, ?, ?)
            """, (45.0 + (i % 180) - 90, -180.0 + (i * 0.72) % 360, 408.0 + (i % 20) * 0.3, tp.strftime("%Y-%m-%d %H:%M:%S"), tp.strftime("%Y-%m-%d")))
        conn.commit()
        conn.close()
        logger.info("Sample data generated")

    # --- Start background fetch loop ---
    t = Thread(target=background_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
