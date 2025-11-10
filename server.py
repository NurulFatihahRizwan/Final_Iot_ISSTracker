# server.py — short CSV-based ISS collector + preview + all-records
from flask import Flask, jsonify, send_from_directory, request
import requests
import csv
import os
from threading import Thread, Event
from datetime import datetime, timedelta
import time

app = Flask(__name__)
DATA_FILE = 'iss_data.csv'
FETCH_INTERVAL = 1  # seconds
stop_event = Event()

# Ensure CSV file exists with header (timestamp = unix seconds)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp','latitude','longitude','altitude','velocity'])

def safe_float(v):
    try:
        return float(v)
    except Exception:
        return None

def fetch_iss_data():
    while not stop_event.is_set():
        try:
            res = requests.get('https://api.wheretheiss.at/v1/satellites/25544', timeout=8)
            if res.status_code == 200:
                d = res.json()
                timestamp = int(d.get('timestamp', time.time()))
                latitude = safe_float(d.get('latitude'))
                longitude = safe_float(d.get('longitude'))
                altitude = safe_float(d.get('altitude'))
                velocity = safe_float(d.get('velocity'))
                with open(DATA_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([timestamp, latitude, longitude, altitude, velocity])
        except Exception as e:
            print("Error fetching ISS data:", e)
        stop_event.wait(FETCH_INTERVAL)

# Start background fetching thread (daemon)
t = Thread(target=fetch_iss_data, daemon=True)
t.start()

@app.route('/api/preview')
def api_preview():
    # day_index semantics: 0 = first day found in CSV
    try:
        day_index = int(request.args.get('day_index', 0))
    except Exception:
        day_index = 0

    records = []
    if not os.path.exists(DATA_FILE):
        return jsonify({'records': []})

    with open(DATA_FILE, 'r') as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        if not all_rows:
            return jsonify({'records': []})

        try:
            first_ts = int(all_rows[0]['timestamp'])
        except Exception:
            return jsonify({'records': []})

        start_of_day = datetime.utcfromtimestamp(first_ts).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_index)
        end_of_day = start_of_day + timedelta(days=1)

        for row in all_rows:
            try:
                ts = int(row['timestamp'])
            except Exception:
                continue
            dt = datetime.utcfromtimestamp(ts)
            if start_of_day <= dt < end_of_day:
                lat = safe_float(row.get('latitude'))
                lon = safe_float(row.get('longitude'))
                alt = safe_float(row.get('altitude'))
                vel = safe_float(row.get('velocity'))
                records.append({
                    'timestamp': ts,
                    'ts_utc': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'latitude': lat,
                    'longitude': lon,
                    'altitude': alt,
                    'velocity': vel
                })

    return jsonify({'records': records})

@app.route('/api/all-records')
def api_all_records():
    """
    Returns:
    {
      records: [...],
      total: n,
      page, per_page, total_pages,
      available_days: ['2025-11-10', ...]
    }
    Supports: page (1), per_page (1000), day (YYYY-MM-DD optional)
    """
    # read CSV once into list of dicts
    if not os.path.exists(DATA_FILE):
        return jsonify({"records": [], "total": 0, "page":1, "per_page":1, "total_pages":1, "available_days": []})

    # params
    try:
        page = max(1, int(request.args.get('page', 1)))
    except Exception:
        page = 1
    try:
        per_page = min(5000, max(1, int(request.args.get('per_page', 1000))))
    except Exception:
        per_page = 1000
    day_filter = request.args.get('day', None)

    rows = []
    with open(DATA_FILE, 'r') as f:
        reader = csv.DictReader(f)
        for i, r in enumerate(reader):
            try:
                ts = int(r.get('timestamp', 0))
            except Exception:
                continue
            dt = datetime.utcfromtimestamp(ts)
            day = dt.strftime('%Y-%m-%d')
            rows.append({
                "id": i+1,
                "timestamp": ts,
                "ts_utc": dt.strftime('%Y-%m-%d %H:%M:%S'),
                "latitude": safe_float(r.get('latitude')),
                "longitude": safe_float(r.get('longitude')),
                "altitude": safe_float(r.get('altitude')),
                "velocity": safe_float(r.get('velocity')),
                "day": day
            })

    # total and available days (distinct days from file, newest first)
    # rows are in file order (oldest first) — reverse to have newest first for response
    rows_sorted = sorted(rows, key=lambda x: x['timestamp'], reverse=True)
    days = sorted(list({r['day'] for r in rows}), reverse=True)

    # optional day filter
    if day_filter:
        filtered = [r for r in rows_sorted if r['day'] == day_filter]
    else:
        filtered = rows_sorted

    total = len(filtered)
    total_pages = (total + per_page - 1) // per_page if total else 1
    start = (page - 1) * per_page
    end = start + per_page
    page_records = filtered[start:end]

    # pack records with expected field names
    out_records = [{
        "id": r["id"],
        "timestamp_unix": r["timestamp"],
        "ts_utc": r["ts_utc"],
        "latitude": r["latitude"],
        "longitude": r["longitude"],
        "altitude": r["altitude"],
        "velocity": r["velocity"],
        "day": r["day"]
    } for r in page_records]

    return jsonify({
        "records": out_records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "available_days": days
    })

# Serve frontend files
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

# serve database at /database to match your frontend link
@app.route('/database')
def serve_database():
    return send_from_directory('.', 'database.html')

@app.route('/api/download')
def download_csv():
    if os.path.exists(DATA_FILE):
        return send_from_directory('.', DATA_FILE, as_attachment=True)
    return "CSV file not found", 404

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        stop_event.set()
