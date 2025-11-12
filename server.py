# server.py — Full ISS Tracker with live fetching, analytics, and CSV downloads
from flask import Flask, jsonify, send_from_directory, request, make_response
import requests, csv, os, time
from threading import Thread, Event
from datetime import datetime, timedelta, timezone
import io

app = Flask(__name__)
DATA_FILE = 'iss_data.csv'
FETCH_INTERVAL = 60  # seconds
stop_event = Event()
MYT = timezone(timedelta(hours=8))  # Malaysia Time UTC+8

# Ensure CSV exists with header
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp','latitude','longitude','altitude','velocity','ts_myt'])

def safe_float(v):
    try: return float(v)
    except: return None

def fetch_and_save_iss_data():
    """Fetch ISS data once and save to CSV."""
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
            ts_myt_excel = "'" + ts_myt  # Excel-friendly

            with open(DATA_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, latitude, longitude, altitude, velocity, ts_myt_excel])
            return True
    except Exception as e:
        print("Error fetching ISS data:", e)
    return False

def background_fetch():
    while not stop_event.is_set():
        fetch_and_save_iss_data()
        stop_event.wait(FETCH_INTERVAL)

# Start background thread only locally
if os.environ.get("RENDER") is None:
    t = Thread(target=background_fetch, daemon=True)
    t.start()

# --- API: Manual fetch ---
@app.route('/api/fetch-now')
def api_fetch_now():
    success = fetch_and_save_iss_data()
    return jsonify({'success': success})

# --- API: Preview records per day ---
@app.route('/api/preview')
def api_preview():
    day_index = int(request.args.get('day_index', 0))
    records = []
    if not os.path.exists(DATA_FILE):
        return jsonify({'records': []})
    
    with open(DATA_FILE, 'r') as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)
        if not all_rows:
            return jsonify({'records': []})
        
        try: first_ts = int(all_rows[0]['timestamp'])
        except: return jsonify({'records': []})
        
        start_of_day = datetime.fromtimestamp(first_ts, tz=MYT).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_index)
        end_of_day = start_of_day + timedelta(days=1)
        
        for row in all_rows:
            try: ts = int(row['timestamp'])
            except: continue
            dt = datetime.fromtimestamp(ts, tz=MYT)
            if start_of_day <= dt < end_of_day:
                lat = safe_float(row.get('latitude'))
                lon = safe_float(row.get('longitude'))
                alt = safe_float(row.get('altitude'))
                vel = safe_float(row.get('velocity'))
                records.append({
                    'timestamp': ts,
                    'ts_myt': row.get('ts_myt', dt.strftime('%Y-%m-%d %H:%M:%S')),
                    'latitude': lat,
                    'longitude': lon,
                    'altitude': alt,
                    'velocity': vel
                })
    return jsonify({'records': records})

# --- API: All records, optional day filter ---
@app.route('/api/all-records')
def api_all_records():
    if not os.path.exists(DATA_FILE):
        return jsonify({"records": [], "total": 0, "available_days": []})
    
    day_filter = request.args.get('day', None)
    rows = []
    with open(DATA_FILE, 'r') as f:
        reader = csv.DictReader(f)
        for i, r in enumerate(reader):
            try: ts = int(r.get('timestamp',0))
            except: continue
            dt = datetime.fromtimestamp(ts, tz=MYT)
            day = dt.strftime('%Y-%m-%d')
            rows.append({
                "id": i+1,
                "timestamp_unix": ts,
                "ts_myt": r.get('ts_myt', dt.strftime('%Y-%m-%d %H:%M:%S')),
                "latitude": safe_float(r.get('latitude')),
                "longitude": safe_float(r.get('longitude')),
                "altitude": safe_float(r.get('altitude')),
                "velocity": safe_float(r.get('velocity')),
                "day": day
            })
    rows_sorted = sorted(rows, key=lambda x: x['timestamp_unix'], reverse=True)
    days = sorted(list({r['day'] for r in rows}), reverse=True)
    filtered = [r for r in rows_sorted if day_filter is None or r['day']==day_filter]
    return jsonify({"records": filtered, "total": len(filtered), "available_days": days})

# --- CSV download: all data ---
@app.route('/api/download')
def download_all_csv():
    if os.path.exists(DATA_FILE):
        return send_from_directory('.', DATA_FILE, as_attachment=True)
    return "CSV not found", 404

# --- CSV download: per day ---
@app.route('/api/download/<day>')
def download_csv_by_day(day):
    if not os.path.exists(DATA_FILE):
        return "CSV not found", 404
    rows = []
    with open(DATA_FILE, 'r') as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts = int(r.get('timestamp',0))
            dt = datetime.fromtimestamp(ts, tz=MYT)
            if dt.strftime('%Y-%m-%d') == day:
                rows.append(r)
    if not rows:
        return f"No records for {day}", 404
    
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename=ISS_data_{day}.csv"
    resp.headers["Content-type"] = "text/csv"
    return resp

# --- Serve HTML pages ---
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/database')
def serve_database():
    return send_from_directory('.', 'database.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

# --- Run ---
if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        stop_event.set()
