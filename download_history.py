import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland"
}

def download_history_for_city(city_slug, years_back=20):
    loc = LOCATIONS[city_slug]
    tz = TIMEZONES.get(city_slug, "UTC")
    
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    
    # Always request in Celsius from Open-Meteo archive, we can convert to F later if needed
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone={tz}"
    )
    
    print(f"Downloading 20 years of history for {loc['name']} ({city_slug})...")
    
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=(10, 30))
            if r.status_code == 200:
                data = r.json()
                if "daily" in data:
                    return data["daily"]
            print(f"  Attempt {attempt+1} failed: {r.status_code} {r.text}")
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
        time.sleep(2)
        
    return None

def main():
    history_data = {}
    
    if HISTORY_FILE.exists():
        print(f"Loading existing history from {HISTORY_FILE}...")
        try:
            history_data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error loading history file: {e}")
            history_data = {}

    for city_slug in LOCATIONS.keys():
        if city_slug in history_data and len(history_data[city_slug].get("time", [])) > 365 * 19:
            print(f"Skip {city_slug}, already has {len(history_data[city_slug]['time'])} records.")
            continue
            
        daily_data = download_history_for_city(city_slug, years_back=20)
        if daily_data:
            history_data[city_slug] = daily_data
            print(f"  Success: {len(daily_data['time'])} days of data downloaded.")
            
            # Save incrementally
            HISTORY_FILE.write_text(json.dumps(history_data, indent=2), encoding="utf-8")
            time.sleep(5) # rate limit protection
        else:
            print(f"  Failed to get data for {city_slug}, sleeping 60s...")
            time.sleep(60)

    print(f"\nDone! History saved to {HISTORY_FILE}")
    print(f"Total size: {HISTORY_FILE.stat().st_size / (1024*1024):.2f} MB")

if __name__ == "__main__":
    main()