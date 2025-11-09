from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import requests, sqlite3, os, csv, io, time
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

DB_FILE = "iss_data.db"
ISS_API = "https://api.wheretheiss.at/v1/satellites/25544"
RECORDS_PER_DAY = 1000
DAYS_TO_KEEP = 3  # store 3 days of data

# --- Database setup ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT,
        day TEXT,
        latitude REAL,
        longitude REAL,
        altitude REAL
    )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Helper functions ---
def current_day_str():
    return datetime.utcnow().strftime("%Y-%m-%d")

def insert_record(lat, lon, alt):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    day = current_day_str()
    c.execute("INSERT INTO telemetry (ts_utc, day, latitude, longitude, altitude) VALUES (?, ?, ?, ?, ?)",
              (ts, day, lat, lon, alt))
    conn.commit()
    conn.close()
    cleanup_old_data()

def cleanup_old_data():
    """Keep only last 3 days"""
    cutoff = (datetime.utcnow() - timedelta(days=DAYS_TO_KEEP)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM telemetry WHERE day < ?", (cutoff,))
    conn.commit()
    conn.close()

def fetch_last3days():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"SELECT id, ts_utc, day, latitude, longitude, altitude FROM telemetry ORDER BY ts_utc ASC")
    rows = c.fetchall()
    conn.close()
    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "ts_utc": r[1],
            "day": r[2],
            "latitude": r[3],
            "longitude": r[4],
            "altitude": r[5]
        })
    return data

def fetch_records(day=None, per_page=1000):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if day:
        c.execute("SELECT id, ts_utc, day, latitude, longitude, altitude FROM telemetry WHERE day=? ORDER BY ts_utc ASC LIMIT ?",
                  (day, per_page))
    else:
        c.execute("SELECT id, ts_utc, day, latitude, longitude, altitude FROM telemetry ORDER BY ts_utc ASC LIMIT ?", (per_page,))
    rows = c.fetchall()
    conn.close()
    records = []
    days_set = set()
    for r in rows:
        records.append({
            "id": r[0],
            "ts_utc": r[1],
            "day": r[2],
            "latitude": r[3],
            "longitude": r[4],
            "altitude": r[5]
        })
        days_set.add(r[2])
    available_days = sorted(list(days_set))
    return records, available_days

def generate_csv(records):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "ts_utc", "day", "latitude", "longitude", "altitude"])
    for r in records:
        writer.writerow([r["id"], r["ts_utc"], r["day"], r["latitude"], r["longitude"], r["altitude"]])
    output.seek(0)
    return output

# --- Routes ---
@app.route("/api/last3days")
def api_last3days():
    data = fetch_last3days()
    return jsonify(data)

@app.route("/api/all-records")
def api_all_records():
    day = request.args.get("day")
    per_page = int(request.args.get("per_page", RECORDS_PER_DAY))
    records, available_days = fetch_records(day=day, per_page=per_page)
    return jsonify({"records": records, "available_days": available_days})

@app.route("/api/download-csv")
def api_download_csv():
    day = request.args.get("day")
    all_days = request.args.get("all")
    if all_days == "1":
        records, _ = fetch_records(per_page=100000)
        filename = "iss_all_days.csv"
    elif day:
        records, _ = fetch_records(day=day, per_page=100000)
        filename = f"iss_{day}.csv"
    else:
        return "Specify ?day=YYYY-MM-DD or ?all=1", 400
    csv_file = generate_csv(records)
    return send_file(io.BytesIO(csv_file.read().encode('utf-8')),
                     mimetype="text/csv",
                     download_name=filename,
                     as_attachment=True)

# --- Background task to fetch ISS every second ---
def fetch_iss_loop():
    while True:
        try:
            r = requests.get(ISS_API, timeout=5)
            if r.status_code == 200:
                data = r.json()
                lat = data.get("latitude")
                lon = data.get("longitude")
                alt = data.get("altitude")
                if lat is not None and lon is not None:
                    insert_record(lat, lon, alt)
        except Exception as e:
            print("Error fetching ISS:", e)
        time.sleep(1)

import threading
threading.Thread(target=fetch_iss_loop, daemon=True).start()

# --- Run server ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
