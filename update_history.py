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
    "hong-kong":    {"lat": 22.3080, "lon": 113.9185, "name": "Hong Kong",    "station": "VHHH", "unit": "C", "region": "tropical"},
    "beijing":      {"lat": 40.0799, "lon": 116.5879, "name": "Beijing",      "station": "ZBAA", "unit": "C", "region": "asia"},
    "wuhan":        {"lat": 30.7831, "lon": 114.2079, "name": "Wuhan",        "station": "ZHHH", "unit": "C", "region": "asia"},
    "jakarta":      {"lat": -6.1256, "lon": 106.6559, "name": "Jakarta",      "station": "WIII", "unit": "C", "region": "tropical"},
    "madrid":       {"lat": 40.4719, "lon":  -3.5626, "name": "Madrid",       "station": "LEMD", "unit": "C", "region": "eu"},
    "milan":        {"lat": 45.6306, "lon":   8.7231, "name": "Milan",        "station": "LIMC", "unit": "C", "region": "eu"},
    "moscow":       {"lat": 55.9736, "lon":  37.4125, "name": "Moscow",       "station": "UUEE", "unit": "C", "region": "eu"},
    "panama-city":  {"lat":  9.0714, "lon": -79.3836, "name": "Panama City",  "station": "MPMG", "unit": "C", "region": "tropical"},
    "karachi":      {"lat": 24.9008, "lon":  67.1681, "name": "Karachi",      "station": "OPKC", "unit": "C", "region": "asia"},
    "austin":       {"lat": 30.1975, "lon": -97.6664, "name": "Austin",       "station": "KAUS", "unit": "F", "region": "us"},
    "denver":       {"lat": 39.8561, "lon": -104.6737,"name": "Denver",       "station": "KDEN", "unit": "F", "region": "us"},
    "cape-town":    {"lat": -33.9249, "lon": 18.4241, "name": "Cape Town",    "station": "FACT", "unit": "C", "region": "af"},
    "busan":        {"lat": 35.1796,  "lon": 129.0756, "name": "Busan",       "station": "RKPK", "unit": "C", "region": "asia"},
    "istanbul":     {"lat": 41.0082,  "lon": 28.9784,  "name": "Istanbul",    "station": "LTFM", "unit": "C", "region": "eu"},
    "chengdu":      {"lat": 30.5728,  "lon": 104.0668, "name": "Chengdu",     "station": "ZUUU", "unit": "C", "region": "asia"},
    "chongqing":    {"lat": 29.5628,  "lon": 106.5516, "name": "Chongqing",   "station": "ZUCK", "unit": "C", "region": "asia"},
    "guangzhou":    {"lat": 23.1291,  "lon": 113.2644, "name": "Guangzhou",   "station": "ZGGG", "unit": "C", "region": "tropical"},
    "helsinki":     {"lat": 60.1695,  "lon": 24.9354,  "name": "Helsinki",    "station": "EFHK", "unit": "C", "region": "eu"},
    "taipei":       {"lat": 25.0330,  "lon": 121.5654, "name": "Taipei",      "station": "RCTP", "unit": "C", "region": "asia"},
    "houston":      {"lat": 29.7604,  "lon": -95.3698, "name": "Houston",     "station": "KIAH", "unit": "F", "region": "us"},
    "amsterdam":    {"lat": 52.3676,  "lon": 4.9041,   "name": "Amsterdam",   "station": "EHAM", "unit": "C", "region": "eu"},
    "shenzhen":     {"lat": 22.5431,  "lon": 114.0579, "name": "Shenzhen",    "station": "ZGSZ", "unit": "C", "region": "tropical"},
    "jeddah":       {"lat": 21.4858,  "lon": 39.1925,  "name": "Jeddah",      "station": "OEJN", "unit": "C", "region": "me"},
    "lagos":        {"lat": 6.5244,   "lon": 3.3792,   "name": "Lagos",       "station": "DNMM", "unit": "C", "region": "af"},
    "kuala-lumpur": {"lat": 3.1390,   "lon": 101.6869, "name": "Kuala Lumpur","station": "WMKK", "unit": "C", "region": "tropical"},
    "san-francisco":{"lat": 37.7749,  "lon": -122.4194,"name": "San Francisco","station": "KSFO", "unit": "F", "region": "us"},
    "warsaw":       {"lat": 52.2297,  "lon": 21.0122,  "name": "Warsaw",      "station": "EPWA", "unit": "C", "region": "eu"},
    "los-angeles":  {"lat": 34.0522,  "lon": -118.2437,"name": "Los Angeles", "station": "KLAX", "unit": "F", "region": "us"},
    "manila":       {"lat": 14.5995,  "lon": 120.9842, "name": "Manila",      "station": "RPLL", "unit": "C", "region": "tropical"},
    "mexico-city":  {"lat": 19.4326,  "lon": -99.1332, "name": "Mexico City", "station": "MMMX", "unit": "C", "region": "sa"}
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
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
    "hong-kong": "Asia/Hong_Kong", "beijing": "Asia/Shanghai",
    "wuhan": "Asia/Shanghai", "jakarta": "Asia/Jakarta",
    "madrid": "Europe/Madrid", "milan": "Europe/Rome",
    "moscow": "Europe/Moscow", "panama-city": "America/Panama",
    "karachi": "Asia/Karachi", "austin": "America/Chicago",
    "denver": "America/Denver",
    "cape-town": "Africa/Johannesburg", "busan": "Asia/Seoul",
    "istanbul": "Europe/Istanbul", "chengdu": "Asia/Shanghai",
    "chongqing": "Asia/Shanghai", "guangzhou": "Asia/Shanghai",
    "helsinki": "Europe/Helsinki", "taipei": "Asia/Taipei",
    "houston": "America/Chicago", "amsterdam": "Europe/Amsterdam",
    "shenzhen": "Asia/Shanghai", "jeddah": "Asia/Riyadh",
    "lagos": "Africa/Lagos", "kuala-lumpur": "Asia/Kuala_Lumpur",
    "san-francisco": "America/Los_Angeles", "warsaw": "Europe/Warsaw",
    "los-angeles": "America/Los_Angeles", "manila": "Asia/Manila",
    "mexico-city": "America/Mexico_City"
}

def download_history_for_city(city_slug, years_back=20):
    loc = LOCATIONS[city_slug]
    tz = TIMEZONES.get(city_slug, "UTC")
    
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    
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
            elif r.status_code == 429:
                print(f"  Attempt {attempt+1} Rate limited. Sleeping 60s...")
                time.sleep(60)
                continue
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
            time.sleep(10) # Open-Meteo has strict rate limits
        else:
            print(f"  Failed to get data for {city_slug}, sleeping 60s...")
            time.sleep(60)

    print(f"\nDone! History saved to {HISTORY_FILE}")
    print(f"Total size: {HISTORY_FILE.stat().st_size / (1024*1024):.2f} MB")

if __name__ == "__main__":
    main()
