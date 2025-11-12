# server.py — ISS collector with Malaysian time (UTC+8)
from flask import Flask, jsonify, send_file, Response, request
import requests, csv, os
from threading import Thread, Event
from datetime import datetime, timedelta, timezone
import time

app = Flask(__name__)
DATA_FILE = 'iss_data.csv'
FETCH_INTERVAL = 60  # seconds
stop_event = Event()
MYT = timezone(timedelta(hours=8))  # Malaysia Time UTC+8

# Ensure CSV file exists
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id','timestamp','latitude','longitude','altitude','velocity','ts_myt'])

def safe_float(v):
    try:
        return float(v)
    except:
        return None

def fetch_and_save_iss_data():
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

            # Determine next ID
            next_id = 1
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE,'r') as f:
                    lines = list(csv.reader(f))
                    if len(lines) > 1:
                        last_id = lines[-1][0]
                        next_id = int(last_id) + 1

            with open(DATA_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([next_id, timestamp, latitude, longitude, altitude, velocity, ts_myt_excel])
            return True
    except Exception as e:
        print("Error fetching ISS data:", e)
    return False

def fetch_loop():
    while not stop_event.is_set():
        fetch_and_save_iss_data()
        stop_event.wait(FETCH_INTERVAL)

# Start background fetching thread (skip on Render)
if os.environ.get("RENDER") is None:
    t = Thread(target=fetch_loop, daemon=True)
    t.start()

# --- API Routes ---
@app.route('/api/fetch-now')
def api_fetch_now():
    success = fetch_and_save_iss_data()
    return jsonify({'success': success})

@app.route('/api/all-records')
def api_all_records():
    if not os.path.exists(DATA_FILE):
        return jsonify({"records": [], "total":0, "available_days":[]})

    day_filter = request.args.get('day', None)
    rows = []

    with open(DATA_FILE,'r') as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                ts = int(r['timestamp'])
            except:
                continue
            dt = datetime.fromtimestamp(ts, tz=MYT)
            day = dt.strftime('%Y-%m-%d')
            row = {
                "id": int(r['id']),
                "timestamp": ts,
                "ts_myt": r.get('ts_myt', dt.strftime('%Y-%m-%d %H:%M:%S')),
                "latitude": safe_float(r.get('latitude')),
                "longitude": safe_float(r.get('longitude')),
                "altitude": safe_float(r.get('altitude')),
                "velocity": safe_float(r.get('velocity')),
                "day": day
            }
            if day_filter is None or day_filter == day:
                rows.append(row)

    rows_sorted = sorted(rows, key=lambda x:x['timestamp'], reverse=True)
    available_days = sorted(list({r['day'] for r in rows}), reverse=True)

    return jsonify({
        "records": rows_sorted,
        "total": len(rows_sorted),
        "available_days": available_days
    })

# --- Download Routes ---
@app.route('/api/download')
def download_all_csv():
    path = os.path.join(os.getcwd(), DATA_FILE)
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name='iss_all_days.csv', mimetype='text/csv')
    return "CSV not found", 404

@app.route('/api/download/<day>')
def download_day_csv(day):
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID','Latitude','Longitude','Altitude','Velocity','Timestamp (MYT)'])

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['ts_myt'][:10] == day:
                    writer.writerow([row.get('id',''), row.get('latitude',''), row.get('longitude',''),
                                     row.get('altitude',''), row.get('velocity',''), row.get('ts_myt','')])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={"Content-Disposition": f"attachment;filename=iss_{day}.csv"})

# --- Serve Frontend ---
@app.route('/')
def serve_index(): return send_file('index.html')
@app.route('/database')
def serve_database(): return send_file('database.html')
@app.route('/<path:path>') 
def serve_static(path): return send_file(path)

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    finally:
        stop_event.set()
