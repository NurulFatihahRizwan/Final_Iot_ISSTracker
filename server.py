from flask import Flask, jsonify, send_from_directory, request
import requests
import csv
import os
from threading import Thread
from datetime import datetime, timedelta
import time

app = Flask(_name_)

DATA_FILE = 'iss_data.csv'

# Ensure CSV file exists with header
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp','latitude','longitude','altitude','velocity'])

# Background function to fetch ISS data every second
def fetch_iss_data():
    while True:
        try:
            res = requests.get('https://api.wheretheiss.at/v1/satellites/25544')
            if res.status_code == 200:
                d = res.json()
                timestamp = int(d['timestamp'])
                latitude = d['latitude']
                longitude = d['longitude']
                altitude = d['altitude']
                velocity = d['velocity']
                with open(DATA_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([timestamp, latitude, longitude, altitude, velocity])
        except Exception as e:
            print("Error fetching ISS data:", e)
        time.sleep(1)  # respect API rate limit

# Start background data fetching
Thread(target=fetch_iss_data, daemon=True).start()

# API endpoint to serve ISS data with day_index
@app.route('/api/preview')
def api_preview():
    day_index = int(request.args.get('day_index', 0))
    records = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)

            if not all_rows:
                return jsonify({'records': []})

            # Compute start of day 0 (first record timestamp)
            first_ts = int(all_rows[0]['timestamp'])
            start_of_day = datetime.utcfromtimestamp(first_ts) + timedelta(days=day_index)
            end_of_day = start_of_day + timedelta(days=1)

            # Filter rows for this day
            for row in all_rows:
                ts = int(row['timestamp'])
                dt = datetime.utcfromtimestamp(ts)
                if start_of_day <= dt < end_of_day:
                    records.append({
                        'timestamp': ts,
                        'ts_utc': dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'latitude': float(row['latitude']),
                        'longitude': float(row['longitude']),
                        'altitude': float(row['altitude']),
                        'velocity': float(row['velocity'])
                    })
    return jsonify({'records': records})

# Serve frontend files
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/database.html')
def serve_database():
    return send_from_directory('.', 'database.html')

@app.route('/api/download')
def download_csv():
    if os.path.exists(DATA_FILE):
        return send_from_directory('.', DATA_FILE, as_attachment=True)
    return "CSV file not found", 404

# Optional: static files (JS/CSS)
@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('.', path)

if _name_ == '_main_':
    app.run(debug=True, host='0.0.0.0', port=5000)
