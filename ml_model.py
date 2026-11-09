import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "history.json"

_history = None

def load_history():
    global _history
    # Reload from file every time if needed, or cache it
    # We reload it if we don't have it or if we want to ensure latest
    if HISTORY_FILE.exists():
        try:
            _history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            _history = {}
    else:
        _history = {}
    return _history

def calculate_baseline(city_slug, target_date_str, unit="C"):
    """
    Calculate the historical baseline (mean) and standard deviation (sigma)
    for a specific date (e.g. '04-26') across all available historical years.
    Returns: (mean_max, mean_min, sigma_max, sigma_min)
    """
    hist = load_history()
    if city_slug not in hist:
        return None, None, None, None
        
    data = hist[city_slug]
    times = data.get("time", [])
    tmaxs = data.get("temperature_2m_max", [])
    tmins = data.get("temperature_2m_min", [])
    
    if not times or len(times) != len(tmaxs) or len(times) != len(tmins):
        return None, None, None, None

    target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
    target_month_day = f"{target_dt.month:02d}-{target_dt.day:02d}"
    
    # Collect all historical values for this specific Month-Day
    # We expand the window to +/- 3 days to smooth the baseline and get more samples (e.g. ~140 samples for 20 years)
    collected_max = []
    collected_min = []
    
    for i, t_str in enumerate(times):
        try:
            dt = datetime.strptime(t_str, "%Y-%m-%d")
            # Check if within 3 days of target month-day
            # Simplest way: create a datetime for current year and check timedelta
            # Handle leap years gracefully by using a fixed non-leap year like 2023
            ref_target = datetime(2023, target_dt.month, target_dt.day)
            try:
                ref_hist = datetime(2023, dt.month, dt.day)
            except ValueError:
                # Handle leap year Feb 29
                ref_hist = datetime(2023, 2, 28)
            
            diff = abs((ref_target - ref_hist).days)
            # if we are around New Year, diff might be 360+, so adjust
            if diff > 300:
                diff = 365 - diff
                
            if diff <= 3: # 7-day window centered on target date
                if tmaxs[i] is not None:
                    collected_max.append(tmaxs[i])
                if tmins[i] is not None:
                    collected_min.append(tmins[i])
        except Exception:
            continue
            
    if not collected_max or not collected_min:
        return None, None, None, None
        
    # Open-Meteo archive returns Celsius. If city uses F, convert now.
    if unit == "F":
        collected_max = [c * 9/5 + 32 for c in collected_max]
        collected_min = [c * 9/5 + 32 for c in collected_min]
        
    mean_max = sum(collected_max) / len(collected_max)
    mean_min = sum(collected_min) / len(collected_min)
    
    # Calculate Standard Deviation (Sigma)
    variance_max = sum((x - mean_max) ** 2 for x in collected_max) / len(collected_max)
    variance_min = sum((x - mean_min) ** 2 for x in collected_min) / len(collected_min)
    
    sigma_max = math.sqrt(variance_max)
    sigma_min = math.sqrt(variance_min)
    
    return mean_max, mean_min, sigma_max, sigma_min

def linear_regression_predict(forecast, baseline, momentum_weight=0.3):
    """
    Very simple linear adjustment (Exponential Moving Average / Momentum logic).
    If forecast is wildly different from baseline, pull it slightly towards the baseline (mean reversion).
    Or if we have a momentum weight, we trust the forecast more.
    Here we do a simple mean reversion: adjusted = forecast * (1-w) + baseline * w
    """
    if baseline is None:
        return forecast
        
    # If the forecast predicts 15C but historical norm is 10C.
    # Pull it back towards the norm by momentum_weight (e.g. 30%).
    # So adjusted = 15 * 0.7 + 10 * 0.3 = 10.5 + 3.0 = 13.5C
    adjusted = forecast * (1.0 - momentum_weight) + baseline * momentum_weight
    return adjusted
