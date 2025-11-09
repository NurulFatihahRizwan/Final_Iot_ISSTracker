from flask import Flask, jsonify, request, send_from_directory, Response
import requests, threading, time, csv, io
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURATION ---
RECORDS_PER_DAY = 1000
MAX_DAYS = 3
FETCH_INTERVAL = 1.0  # seconds, do not exceed API limit
ISS_API_URL = "https://api.wheretheiss.at/v1/satellites/25544"

# --- DATA STORAGE ---
# Structure: { "1": [records], "2": [records], "3": [records] }
data_store = {str(day): [] for day in range(1, MAX_DAYS + 1)}
start_date = datetime.utcnow().date()  # day 1 start
lock = threading.Lock()

# --- HELPER FUNCTIONS ---
def get_current_day_number():
    """Return 1,2,3 based on start_date and UTC date."""
    delta_days = (datetime.utcnow().date() - start_date).days
    return min(delta_days + 1, MAX_DAYS)

def fetch_iss_data():
    """Background thread to fetch ISS telemetry continuously."""
    while True:
        try:
            resp = requests.get(ISS_API_URL)
            if resp.status_code == 200:
                iss = resp.json()
                record = {
                    "id": None,  # will assign below
                    "ts_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                    "day": str(get_current_day_number()),
                    "latitude": iss.get("latitude"),
                    "longitude": iss.get("longitude"),
                    "altitude": iss.get("altitude")
                }
                day = record["day"]
                with lock:
                    # Assign ID
                    record["id"] = len(data_store[day]) + 1
                    # Append if below RECORDS_PER_DAY
                    if len(data_store[day]) < RECORDS_PER_DAY:
                        data_store[day].append(record)
        except Exception as e:
            print("Error fetching ISS data:", e)
        time.sleep(FETCH_INTERVAL)

# --- START BACKGROUND THREAD ---
threading.Thread(target=fetch_iss_data, daemon=True).start()

# --- ROUTES ---
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/database")
def database():
    return send_from_directory(".", "database.html")

@app.route("/api/last3days")
def api_last3days():
    """Return all stored data for last 3 days (for dashboard)."""
    with lock:
        all_data = []
        for day in range(1, MAX_DAYS+1):
            all_data.extend(data_store[str(day)])
    return jsonify(all_data)

@app.route("/api/all-records")
def api_all_records():
    """Return records for selected day, include available days."""
    per_page = int(request.args.get("per_page", RECORDS_PER_DAY))
    day = request.args.get("day")
    with lock:
        available_days = [d for d in data_store if len(data_store[d]) > 0]
        if day not in available_days:
            day = available_days[0] if available_days else None
        records = data_store.get(day, [])[:per_page] if day else []
    return jsonify({
        "available_days": available_days,
        "records": records
    })

@app.route("/api/download-csv")
def api_download_csv():
    """Download CSV for selected day or all days."""
    all_days = request.args.get("all") == "1"
    day = request.args.get("day")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","ts_utc","day","latitude","longitude","altitude"])

    with lock:
        if all_days:
            for d in data_store:
                for r in data_store[d]:
                    writer.writerow([r["id"], r["ts_utc"], r["day"], r["latitude"], r["longitude"], r["altitude"]])
            filename = f"iss_all_days.csv"
        else:
            if day not in data_store:
                return "Invalid day", 400
            for r in data_store[day]:
                writer.writerow([r["id"], r["ts_utc"], r["day"], r["latitude"], r["longitude"], r["altitude"]])
            filename = f"iss_day_{day}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    app.run(debug=True)
