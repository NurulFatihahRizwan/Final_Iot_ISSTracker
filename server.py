#!/usr/bin/env python3
import os
import time
import sqlite3
import logging
import requests
import csv
import io
from datetime import datetime, timedelta
from threading import Thread, Event
from flask import Flask, jsonify, send_file, request, Response
from flask_cors import CORS

# ---------- Configuration ----------
DB_PATH = os.environ.get("DB_PATH", "iss_data.db")
API_URL = os.environ.get("ISS_API_URL", "https://api.wheretheiss.at/v1/satellites/25544")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL_SEC", "60"))
ENABLE_CLEANUP = os.environ.get("ENABLE_CLEANUP", "0") == "1"
MAX_RETENTION_DAYS = int(os.environ.get("MAX_RETENTION_DAYS", "3"))
SAMPLE_DATA = os.environ.get("SAMPLE_DATA", "0") == "1"
PORT = int(os.environ.get("PORT", "10000"))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("iss-tracker")

# ---------- Flask app ----------
app = Flask(__name__, static_folder=".")
CORS(app)


# ---------- DB utilities ----------
def get_conn():
    # Create a fresh connection per use (safer with threads)
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
    if not ENABLE_CLEANUP:
        return
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

def get_first_collection_date():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MIN(day) as first_day FROM iss_positions")
    row = cur.fetchone()
    conn.close()
    return row["first_day"] if row and row["first_day"] else None

# ---------- CSV Export Utilities ----------
def get_available_days():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day ASC")
    days = [r["day"] for r in cur.fetchall()]
    conn.close()
    return days

def generate_csv(day_filter=None):
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
        cur.execute("""
            SELECT id, latitude, longitude, altitude, timestamp AS ts_utc, day
            FROM iss_positions
            ORDER BY timestamp ASC
        """)
    rows = cur.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Latitude', 'Longitude', 'Altitude (km)', 'Timestamp (UTC)', 'Day'])
    for row in rows:
        writer.writerow([
            row['id'],
            row['latitude'],
            row['longitude'],
            row['altitude'] if row['altitude'] is not None else '',
            row['ts_utc'],
            row['day']
        ])
    output.seek(0)
    return output.getvalue()

# ---------- Fetching ISS ----------
def parse_wther_resp(data):
    ts = datetime.utcfromtimestamp(int(data.get("timestamp", time.time())))
    # altitude sometimes None; coerce safely
    alt = data.get("altitude")
    try:
        alt_val = None if alt is None else float(alt)
    except Exception:
        alt_val = None
    return {
        "latitude": float(data.get("latitude")),
        "longitude": float(data.get("longitude")),
        "altitude": alt_val,
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
    # initial immediate fetch to seed db quickly
    pos = fetch_iss_position()
    if pos:
        try:
            save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
            logger.info("Initial fetch saved: %s", pos["ts_utc"])
        except Exception as e:
            logger.exception("Failed to save initial fetch: %s", e)

    while not stop_event.is_set():
        pos = fetch_iss_position()
        if pos:
            try:
                save_position(pos["latitude"], pos["longitude"], pos["altitude"], pos["ts_utc"])
            except Exception as e:
                logger.exception("Failed to save fetched position: %s", e)
            count = get_record_count()
            if count and count % 3600 == 0:
                logger.info("Collected %d records (~%0.2f days)", count, count / 86400.0)
        if ENABLE_CLEANUP:
            cleanup_counter += 1
            if cleanup_counter >= max(1, int(3600 / max(1, FETCH_INTERVAL))):
                cleanup_old_data()
                cleanup_counter = 0
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
    try:
        pos = fetch_iss_position()
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
    except Exception as e:
        logger.exception("Error in /api/current: %s", e)
        return jsonify({"error": "Server error"}), 500

@app.route("/api/last3days")
def api_last3days():
    try:
        conn = get_conn()
        cur = conn.cursor()
        if ENABLE_CLEANUP:
            # when cleanup enabled, use timestamp cutoff
            cutoff = (datetime.utcnow() - timedelta(days=MAX_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
              SELECT latitude, longitude, altitude, timestamp AS ts_utc, day
              FROM iss_positions
              WHERE timestamp >= ?
              ORDER BY timestamp ASC
            """, (cutoff,))
        else:
            # pick up to the 3 most recent days by day value (descending)
            cur.execute("SELECT DISTINCT day FROM iss_positions ORDER BY day DESC LIMIT 3")
            recent_rows = cur.fetchall()
            recent_days = [r["day"] for r in recent_rows if r["day"] is not None]
            if recent_days:
                # use placeholders for safety
                placeholders = ",".join("?" for _ in recent_days)
                params = tuple(recent_days)
                cur.execute(f"""
                  SELECT latitude, longitude, altitude, timestamp AS ts_utc, day
                  FROM iss_positions
                  WHERE day IN ({placeholders})
                  ORDER BY timestamp ASC
                """, params)
            else:
                # fallback: return everything (empty DB case)
                cur.execute("""
                  SELECT latitude, longitude, altitude, timestamp AS ts_utc, day
                  FROM iss_positions
                  ORDER BY timestamp ASC
                """)
        rows = cur.fetchall()
        conn.close()

        data = [{
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "altitude": r["altitude"],
            "ts_utc": r["ts_utc"],
            "day": r["day"]
        } for r in rows]
        return jsonify(data)
    except Exception as e:
        logger.exception("Error in /api/last3days: %s", e)
        return jsonify({"error": "Unable to fetch last3days"}), 500

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

@app.route("/api/stats")
def api_stats():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM iss_positions")
        total = cur.fetchone()[0]
        cur.execute("SELECT day, COUNT(*) AS cnt FROM iss_positions GROUP BY day ORDER BY day ASC")
        per_day = {r["day"]: r["cnt"] for r in cur.fetchall()}
        first_day = get_first_collection_date()
        conn.close()
        total_hours = total / 3600.0
        total_days = total_hours / 24.0
        return jsonify({
            "total_records": total,
            "total_hours": round(total_hours, 2),
            "total_days": round(total_days, 2),
            "records_per_day": per_day,
            "collection_interval_seconds": FETCH_INTERVAL,
            "cleanup_enabled": ENABLE_CLEANUP,
            "max_retention_days": MAX_RETENTION_DAYS if ENABLE_CLEANUP else "Unlimited",
            "first_collection_date": first_day,
            "total_unique_days": len(per_day)
        })
    except Exception as e:
        logger.exception("Error in /api/stats: %s", e)
        return jsonify({"error": "Unable to fetch stats"}), 500

@app.route("/api/export/csv")
def export_csv():
    try:
        day_param = request.args.get("day", None)
        day_number = request.args.get("day_number", None)
        available_days = get_available_days()
        if not available_days:
            return jsonify({"error": "No data available"}), 404
        if day_number:
            try:
                day_idx = int(day_number) - 1
                if 0 <= day_idx < len(available_days):
                    day_param = available_days[day_idx]
                else:
                    return jsonify({"error": f"Day {day_number} not found. Available days: {len(available_days)}"}), 404
            except ValueError:
                return jsonify({"error": "Invalid day_number parameter"}), 400
        csv_data = generate_csv(day_param)
        if day_param:
            filename = f"iss_data_{day_param}.csv"
        else:
            first_day = available_days[0] if available_days else "unknown"
            last_day = available_days[-1] if available_days else "unknown"
            filename = f"iss_data_{first_day}_to_{last_day}.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.exception("Error in CSV export: %s", e)
        return jsonify({"error": "Unable to export CSV"}), 500

@app.route("/api/export/days")
def export_days_info():
    try:
        available_days = get_available_days()
        conn = get_conn()
        cur = conn.cursor()
        day_info = []
        for idx, day in enumerate(available_days, 1):
            cur.execute("SELECT COUNT(*) FROM iss_positions WHERE day = ?", (day,))
            count = cur.fetchone()[0]
            cur.execute("SELECT MIN(timestamp) as first_time, MAX(timestamp) as last_time FROM iss_positions WHERE day = ?", (day,))
            times = cur.fetchone()
            day_info.append({
                "day_number": idx,
                "date": day,
                "record_count": count,
                "first_record": times["first_time"],
                "last_record": times["last_time"]
            })
        conn.close()
        first_collection = get_first_collection_date()
        return jsonify({
            "total_days": len(available_days),
            "first_collection_date": first_collection,
            "cleanup_enabled": ENABLE_CLEANUP,
            "days": day_info
        })
    except Exception as e:
        logger.exception("Error getting days info: %s", e)
        return jsonify({"error": "Unable to fetch days information"}), 500

# ---------- startup ----------
if __name__ == "__main__":
    cleanup_status = "ENABLED" if ENABLE_CLEANUP else "DISABLED"
    logger.info("Starting ISS Tracker (DB=%s) FETCH_INTERVAL=%ss", DB_PATH, FETCH_INTERVAL)
    logger.info("Data cleanup: %s (Retention: %s days)", cleanup_status, MAX_RETENTION_DAYS if ENABLE_CLEANUP else "Unlimited")
    init_database()
    if SAMPLE_DATA and get_record_count() == 0:
        now = datetime.utcnow()
        conn = get_conn()
        cur = conn.cursor()
        logger.info("Generating sample data (3000 records over 3 days)...")
        for i in range(3000):
            tp = now - timedelta(seconds=i)
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
