import os
import csv
import json
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

import requests

app = Flask(__name__)

# --- Configuration ---
PER_DAY_RECORDS = 1000
ISS_API = "https://api.wheretheiss.at/v1/satellites/25544"
DATA_FILE = "iss_data.json"
FETCH_INTERVAL = 1  # seconds

# --- In-memory storage ---
data_history = {}  # {"2025-11-09": [records], "2025-11-10": [...], ...}

# Load existing data if exists
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        data_history = json.load(f)

# --- Helper functions ---
def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data_history, f)

def fetch_iss():
    while True:
        try:
            res = requests.get(ISS_API, timeout=5)
            if res.status_code == 200:
                iss_data = res.json()
                ts = datetime.utcfromtimestamp(iss_data["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                today = get_today()

                # Initialize day list
                if today not in data_history:
                    data_history[today] = []

                # Limit to PER_DAY_RECORDS per day
                if len(data_history[today]) >= PER_DAY_RECORDS:
                    data_history[today].pop(0)

                # Add new record
                record = {
                    "id": len(data_history[today]) + 1,
                    "ts_utc": ts,
                    "day": today,
                    "latitude": iss_data.get("latitude"),
                    "longitude": iss_data.get("longitude"),
                    "altitude": iss_data.get("altitude")
                }
                data_history[today].append(record)
                save_data()
        except Exception as e:
            print("Error fetching ISS:", e)
        time.sleep(FETCH_INTERVAL)

# Start background thread for data fetching
threading.Thread(target=fetch_iss, daemon=True).start()

# --- API Routes ---

@app.route("/api/last3days")
def last_3_days():
    """Return all records from last 3 days in chronological order"""
    today = datetime.utcnow().date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(2, -1, -1)]
    result = []
    for d in days:
        result.extend(data_history.get(d, []))
    return jsonify(result)

@app.route("/api/all-records")
def all_records():
    """Return records for a specific day or default to first available day"""
    per_page = int(request.args.get("per_page", PER_DAY_RECORDS))
    day = request.args.get("day", "")

    available_days = sorted(data_history.keys())
    target_day = day if day in data_history else (available_days[0] if available_days else "")

    records = data_history.get(target_day, [])[:per_page]

    return jsonify({
        "records": records,
        "available_days": available_days
    })

@app.route("/api/download-csv")
def download_csv():
    """Download CSV of selected day or all days"""
    all_days = request.args.get("all", "0") == "1"
    day = request.args.get("day", "")
    filename = "iss_data.csv"

    if all_days:
        records = []
        for d in sorted(data_history.keys()):
            records.extend(data_history[d])
    elif day in data_history:
        records = data_history[day]
    else:
        return "No data for this day", 404

    # Create CSV
    csv_path = os.path.join(".", filename)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id","ts_utc","day","latitude","longitude","altitude"])
        writer.writeheader()
        for r in records:
            writer.writerow(r)

    return send_from_directory(".", filename, as_attachment=True)

# --- Serve HTML pages ---
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/database")
def database():
    return send_from_directory(".", "database.html")

# Serve any other static files (CSS, JS)
@app.route("/<path:path>")
def static_files(path):
    if os.path.exists(path):
        return send_from_directory(".", path)
    return "File not found", 404

# --- Run Server ---
if __name__ == "__main__":
    app.run(debug=True)
