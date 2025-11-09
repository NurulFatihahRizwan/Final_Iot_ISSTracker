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
API_URL = "https://api.wheretheiss.at/v1/satellites/25544"
FETCH_INTERVAL = 60  # seconds
MAX_DAYS = 3  # Keep only 3 days of data
PORT = int(os.environ.get("PORT", 10000))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ISS-Tracker")

# ---------- Flask app ----------
app = Flask(__name__, static_folder=".")
CORS(app)

# ---------- Database ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, check_same_thread=False)
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
    logger.info("Database initialized")

def save_position(lat, lon, alt, ts_utc):
    day = ts_utc.split(" ")[0]  # YYYY-MM-DD
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
        VALUES (?, ?, ?, ?, ?)
    """, (lat, lon, alt, ts_utc, day))
    conn.commit()
    conn.close()

def cleanup_old_days():
    cutoff_day = (datetime.utcnow() - timedelta(days=MAX_DAYS)).strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM iss_positions WHERE day < ?", (cutoff_day,))
    conn.commit()
    conn.close()

# ---------- ISS Fetching Thread ----------
stop_event = Event()

def fetch_iss_loop():
    while not stop_event.is_set():
        try:
            r = requests.get(API_URL, timeout=10)
            r.raise_for_status()
            data = r.json()
            lat = data.get("latitude")
            lon = data.get("longitude")
            alt = data.get("altitude")  # in km
            ts_utc = datetime.utcfromtimestamp(data.get("timestamp")).strftime("%Y-%m-%d %H:%M:%S")
            save_position(lat, lon, alt, ts_utc)
            cleanup_old_days()
            logger.info("Saved ISS data: %s,%s,%s at %s", lat, lon, alt, ts_utc)
        except Exception as e:
            logger.warning("Failed to fetch/save ISS data: %s", e)
        stop_event.wait(FETCH_INTERVAL)

# ---------- Routes ----------
@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/database")
def database_view():
    return app.send_static_file("database.html")

@app.route("/api/last3days")
def api_last3days():
    conn = get_conn()
    cur = conn.cursor()
    cutoff_day = (datetime.utcnow() - timedelta(days=MAX_DAYS)).strftime("%Y-%m-%d")
    cur.execute("SELECT * FROM iss_positions WHERE day >= ? ORDER BY timestamp ASC", (cutoff_day,))
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/all-records")
def api_all_records():
    day = request.args.get("day")
    per_page = int(request.args.get("per_page", 1000))
    conn = get_conn()
    cur = conn.cursor()

    # If day specified, only that day's data
    if day:
        cur.execute("SELECT * FROM iss_positions WHERE day=? ORDER BY id ASC LIMIT ?", (day, per_page))
    else:
        cur.execute("SELECT * FROM iss_positions ORDER BY id ASC LIMIT ?", (per_page,))
    
    rows = [dict(r) for r in cur.fetchall()]

    # Available days
    cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day ASC")
    available_days = [r["day"] for r in cur.fetchall()]

    conn.close()
    return jsonify({"records": rows, "available_days": available_days})

@app.route("/api/download-csv")
def api_download_csv():
    all_days = request.args.get("all") == "1"
    day = request.args.get("day")

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
    init_db()
    thread = Thread(target=fetch_iss_loop, daemon=True)
    thread.start()
    try:
        app.run(host="0.0.0.0", port=PORT)
    finally:
        stop_event.set()
