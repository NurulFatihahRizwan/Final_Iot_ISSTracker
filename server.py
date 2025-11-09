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

# ---------- Configuration ----------
DB_PATH = os.environ.get("DB_PATH", "iss_data.db")
API_URL = os.environ.get("ISS_API_URL", "https://api.wheretheiss.at/v1/satellites/25544")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL_SEC", "86"))
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
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, check_same_thread=False)
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
        "altitude": None if data.get("altitude") is None else float(data.get("altitude")),
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

def fetch_iss_position_once():
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
collector_started = False

def background_loop():
    logger.info("Background collector started (interval %ss)", FETCH_INTERVAL)
    cleanup_counter = 0
    while not stop_event.is_set():
        pos = fetch_iss_position_once()
        if pos:
            try:
                save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
            except Exception as e:
                logger.exception("Failed to save position: %s", e)
            count = get_record_count()
            if count and count % 3600 == 0:
                logger.info("Collected %d records (~%0.2f days)", count, count / 86400.0)
        else:
            logger.debug("No position returned this cycle")

        cleanup_counter += 1
        try:
            threshold = max(1, int(3600 / max(1, FETCH_INTERVAL)))
        except Exception:
            threshold = 3600
        if cleanup_counter >= threshold:
            try:
                cleanup_old_data()
            except Exception:
                logger.exception("Cleanup failed")
            cleanup_counter = 0

        stop_event.wait(FETCH_INTERVAL)

def start_collector_thread():
    global collector_started
    if collector_started:
        return
    collector_started = True
    t = Thread(target=background_loop, daemon=True)
    t.start()

# ---------- API endpoints ----------
@app.route("/")
def index():
    index_path = os.path.join(os.getcwd(), "index.html")
    if os.path.exists(index_path):
        return send_file(index_path)
    return "ISS Tracker API - index not found", 200

@app.route("/database")
def database_view():
    db_path = os.path.join(os.getcwd(), "database.html")
    if os.path.exists(db_path):
        return send_file(db_path)
    return "Database viewer not found", 404

@app.route("/api/current")
def api_current():
    pos = fetch_iss_position_once()
    if pos:
        try:
            save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
        except Exception:
            logger.exception("saving live sample failed")
        return jsonify(pos)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT latitude, longitude, altitude, timestamp AS ts_utc, day FROM iss_positions ORDER BY timestamp DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "altitude": row["altitude"],
            "ts_utc": row["ts_utc"],
            "day": row["day"]
        })
    return jsonify({"error": "No data available"}), 404

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
              SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day
              FROM iss_positions
              WHERE day = ?
              ORDER BY timestamp DESC
              LIMIT ? OFFSET ?
            """, (day_filter, per_page, (page - 1) * per_page))
        else:
            cur.execute("SELECT COUNT(*) FROM iss_positions")
            total = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day DESC")
            days = [r["day"] for r in cur.fetchall()]
            cur.execute("""
              SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day
              FROM iss_positions
              ORDER BY timestamp DESC
              LIMIT ? OFFSET ?
            """, (per_page, (page - 1) * per_page))
        rows = cur.fetchall()
        conn.close()
        records = [{
            "id": r["id"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "altitude": r["altitude"],
            "ts_utc": r["ts_utc"],
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

@app.route("/api/download-csv")
def download_csv():
    day_filter = request.args.get("day", None)
    all_days = request.args.get("all", "0") == "1"

    def generator():
        conn = get_conn()
        cur = conn.cursor()
        if day_filter and not all_days:
            cur.execute("""
              SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day 
              FROM iss_positions 
              WHERE day = ? 
              ORDER BY timestamp ASC
            """, (day_filter,))
        else:
            cur.execute("""
              SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day 
              FROM iss_positions 
              ORDER BY timestamp ASC
            """)
        yield "id,latitude,longitude,altitude,ts_utc,day\n"
        for row in cur:
            alt = "" if row["altitude"] is None else row["altitude"]
            yield f"{row['id']},{row['latitude']},{row['longitude']},{alt},{row['ts_utc']},{row['day']}\n"
        conn.close()

    filename = f"iss_data_{day_filter if day_filter else 'all'}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return Response(stream_with_context(generator()), mimetype="text/csv", headers=headers)

@app.route("/api/stats")
def api_stats():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM iss_positions")
        total = cur.fetchone()[0]
        cur.execute("SELECT day, COUNT(*) AS cnt FROM iss_positions GROUP BY day ORDER BY day DESC")
        per_day = {r["day"]: r["cnt"] for r in cur.fetchall()}
        conn.close()
        total_hours = total / 3600.0
        total_days = total_hours / 24.0
        return jsonify({
            "total_records": total,
            "total_hours": round(total_hours, 2),
            "total_days": round(total_days, 2),
            "records_per_day": per_day,
            "collection_interval_seconds": FETCH_INTERVAL,
            "max_retention_days": MAX_RETENTION_DAYS
        })
    except Exception as e:
        logger.exception("Error in /api/stats: %s", e)
        return jsonify({"error": "Unable to fetch stats"}), 500

# ---------- lifecycle hook for Flask 3.x ----------
@app.before_serving
def ensure_started():
    init_database()
    if SAMPLE_DATA and get_record_count() == 0:
        now = datetime.utcnow()
        conn = get_conn()
        cur = conn.cursor()
        logger.info("Generating sample data (3000 records across 3 days)...")
        for day_off in range(3):
            day_ts = now - timedelta(days=day_off)
            base_date = day_ts.strftime("%Y-%m-%d")
            for i in range(1000):
                tp = day_ts - timedelta(seconds=i)
                lat = 45.0 + ((i % 180) - 90)
                lon = -180.0 + ((i * 0.72) % 360)
                alt = 408.0 + (i % 20) * 0.3
                cur.execute("""
                  INSERT INTO iss_positions (latitude, longitude, altitude, timestamp, day)
                  VALUES (?, ?, ?, ?, ?)
                """, (lat, lon, alt, tp.strftime("%Y-%m-%d %H:%M:%S"), base_date))
        conn.commit()
        conn.close()
        logger.info("Sample data generated")
    start_collector_thread()

# ---------- main ----------
if __name__ == "__main__":
    logger.info("Starting ISS Tracker (DB=%s) FETCH_INTERVAL=%ss", DB_PATH, FETCH_INTERVAL)
    init_database()
    start_collector_thread()
    app.run(host="0.0.0.0", port=PORT, debug=False)
