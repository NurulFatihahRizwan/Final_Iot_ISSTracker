# server.py — ISS collector with Malaysian time (UTC+8), daily CSV files
from flask import Flask, jsonify, send_from_directory, request
import requests, csv, os
from threading import Thread, Event
from datetime import datetime, timedelta, timezone
import time

app = Flask(__name__)
FETCH_INTERVAL = 60  # seconds
stop_event = Event()

MYT = timezone(timedelta(hours=8))  # Malaysia Time UTC+8
DATA_DIR = 'data'  # folder to store daily CSVs
os.makedirs(DATA_DIR, exist_ok=True)

def safe_float(v):
    try:
        return float(v)
    except Exception:
        return None

def get_today_filename():
    """Return today's CSV filename (e.g. data/iss_data_2025-11-12.csv)."""
    date_str = datetime.now(MYT).strftime('%Y-%m-%d')
    return os.path.join(DATA_DIR, f"iss_data_{date_str}.csv")

def ensure_file_exists(path):
    """Create file with header if not exists."""
    if not os.path.exists(path):
        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp','latitude','longitude','altitude','velocity','ts_myt'])

def fetch_and_save_iss_data():
    """Fetch ISS data and save to today's CSV."""
    try:
        res = requests.get('https://api.wheretheiss.at/v1/satellites/25544', timeout=8)
        if res.status_code == 200:
            d = res.json()
            timestamp = int(d.get('timestamp', time.time()))
            latitude = safe_float(d.get('latitude'))
            longitude = safe_float(d.get('longitude'))
            altitude = safe_float(d.get('altitude'))
            velocity = safe_float(d.get('velocity'))
            ts_myt = datetime.fromtimestamp(timestamp, tz=MYT).strftime('%Y-%m-%d %H:%M:%S')
            ts_myt_excel = "'" + ts_myt

            file_path = get_today_filename()
            ensure_file_exists(file_path)
            with open(file_path, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, latitude, longitude, altitude, velocity, ts_myt_excel])
            return True
    except Exception as e:
        print("Error fetching ISS data:", e)
    return False

def fetch_loop():
    while not stop_event.is_set():
        fetch_and_save_iss_data()
        stop_event.wait(FETCH_INTERVAL)

if os.environ.get("RENDER") is None:
    t = Thread(target=fetch_loop, daemon=True)
    t.start()

@app.route('/api/fetch-now')
def api_fetch_now():
    success = fetch_and_save_iss_data()
    return jsonify({'success': success})

@app.route('/api/all-records')
def api_all_records():
    """Return all records grouped by date."""
    all_data = []
    available_days = []

    for file in sorted(os.listdir(DATA_DIR), reverse=True):
        if not file.startswith('iss_data_') or not file.endswith('.csv'):
            continue
        date = file.replace('iss_data_', '').replace('.csv', '')
        available_days.append(date)
        file_path = os.path.join(DATA_DIR, file)
        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for i, r in enumerate(reader):
                try:
                    ts = int(r.get('timestamp', 0))
                except Exception:
                    continue
                all_data.append({
                    "id": i+1,
                    "timestamp_unix": ts,
                    "ts_myt": r.get('ts_myt'),
                    "latitude": safe_float(r.get('latitude')),
                    "longitude": safe_float(r.get('longitude')),
                    "altitude": safe_float(r.get('altitude')),
                    "velocity": safe_float(r.get('velocity')),
                    "day": date
                })

    all_data_sorted = sorted(all_data, key=lambda x: x['timestamp_unix'], reverse=True)
    return jsonify({
        "records": all_data_sorted,
        "available_days": available_days,
        "total": len(all_data_sorted)
    })

@app.route('/api/download/<day>')
def download_day_csv(day):
    """Download specific day's CSV."""
    filename = f"iss_data_{day}.csv"
    file_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(file_path):
        return send_from_directory(DATA_DIR, filename, as_attachment=True)
    return "CSV file not found", 404

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/database')
def serve_database():
    return send_from_directory('.', 'database.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        stop_event.set()
