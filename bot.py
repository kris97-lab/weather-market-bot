#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Weather Trading Bot for Polymarket
=====================================================
Blends 5 forecast models (ECMWF, GFS/HRRR, ICON, JMA, UKMO) with region weights,
calibrates bias and sigma per city from resolved markets, checks live METAR observations,
prices Polymarket temperature buckets, and sizes trades with fractional Kelly.
Paper mode by default (WEATHER_DRY_RUN=true); live CLOB orders only when explicitly enabled.

Usage:
    python bot.py          # main loop
    python bot.py report   # full report
    python bot.py status   # balance and open positions
"""

import os
import sys
import re
import math
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from ml_model import calculate_baseline, linear_regression_predict
except ImportError:
    calculate_baseline = None
    linear_regression_predict = None

# =============================================================================
# AI & TELEGRAM INTEGRATION
# =============================================================================
import os
from dotenv import load_dotenv
from ladder_engine import build_ladder_signals

load_dotenv()

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
TOMORROW_API_KEY = os.getenv("TOMORROW_API_KEY", "")


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def infer_temp_unit_from_bucket(bucket_low, bucket_high):
    probe = bucket_high if bucket_high != 999 else bucket_low
    return "F" if abs(float(probe)) > 45 else "C"


def intraday_temp_margin(unit):
    return 1.5 if unit == "F" else 0.8


def convert_temp_unit(value, from_unit, to_unit):
    if value is None or from_unit == to_unit:
        return value
    if from_unit == "C" and to_unit == "F":
        return value * 9.0 / 5.0 + 32.0
    if from_unit == "F" and to_unit == "C":
        return (value - 32.0) * 5.0 / 9.0
    return value


def check_tomorrow_io_nowcast(
    lat,
    lon,
    m_type,
    target_date_str=None,
    tz_name="UTC",
    side=None,
    bucket_low=None,
    bucket_high=None,
    forecast_temp=None,
):
    if not TOMORROW_API_KEY:
        return True, "No Tomorrow.io Key"
    try:
        market_unit = infer_temp_unit_from_bucket(bucket_low, bucket_high) if bucket_low is not None and bucket_high is not None else "C"
        tomorrow_units = "imperial" if market_unit == "F" else "metric"
        url = (
            f"https://api.tomorrow.io/v4/weather/forecast"
            f"?location={lat},{lon}&timesteps=1h&units={tomorrow_units}&apikey={TOMORROW_API_KEY}"
        )
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            hourly = data.get('timelines', {}).get('hourly', [])
            if not hourly:
                return True, ""

            try:
                local_tz = zoneinfo.ZoneInfo(tz_name)
            except Exception:
                local_tz = timezone.utc

            filtered = []
            for h in hourly:
                time_str = h.get("time")
                if not time_str:
                    continue
                try:
                    dt_utc = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                dt_local = dt_utc.astimezone(local_tz)

                if target_date_str and dt_local.strftime("%Y-%m-%d") != target_date_str:
                    continue

                hour = dt_local.hour
                if m_type == "highest":
                    if 10 <= hour <= 17:
                        filtered.append(h)
                elif m_type == "lowest":
                    if 0 <= hour <= 7:
                        filtered.append(h)
                else:
                    filtered.append(h)

            if not filtered:
                filtered = hourly[:3]
                label = "Fallback(3h)"
            else:
                label = "TargetWindow"
            
            max_clouds = max([h['values'].get('cloudCover', 0) for h in filtered])
            max_rain = max([h['values'].get('precipitationProbability', 0) for h in filtered])
            temps = []
            for h in filtered:
                val = h.get('values', {}).get('temperature')
                if val is not None:
                    temps.append(float(val))
            peak_temp = max(temps) if temps else None
            trough_temp = min(temps) if temps else None
            
            context = f"☁️ Clouds({label}): {max_clouds}% | 🌧 Rain({label}): {max_rain}%"
            if peak_temp is not None and m_type == "highest":
                context += f" | 🌡 Peak({label}): {peak_temp:.1f}"
            if trough_temp is not None and m_type == "lowest":
                context += f" | 🌡 Trough({label}): {trough_temp:.1f}"
            if forecast_temp is not None:
                context += f" | Model: {forecast_temp}"

            if bucket_low is not None and bucket_high is not None and side:
                unit = market_unit
                margin = intraday_temp_margin(unit)

                if m_type == "highest" and peak_temp is not None:
                    if side == "NO":
                        if peak_temp >= bucket_low - margin:
                            return False, context + f" ⚠️ Intraday heat path too close to strike for NO (margin {margin}{unit})."
                    elif side == "YES":
                        if peak_temp < bucket_low - margin:
                            return False, context + f" ⚠️ Intraday peak too weak for YES (margin {margin}{unit})."

                if m_type == "lowest" and trough_temp is not None:
                    if side == "NO":
                        if trough_temp <= bucket_high + margin:
                            return False, context + f" ⚠️ Intraday cooling path too close to strike for NO (margin {margin}{unit})."
                    elif side == "YES":
                        if trough_temp > bucket_high + margin:
                            return False, context + f" ⚠️ Intraday trough too warm for YES (margin {margin}{unit})."

            if m_type == "highest":
                if side == "NO" and max_clouds < 20 and max_rain < 10:
                    return False, context + " ⚠️ Clear sky: Sun heating risk for NO."
                if side == "YES" and max_clouds > 85 and max_rain > 50 and peak_temp is not None and forecast_temp is not None and peak_temp < forecast_temp:
                    return False, context + " ⚠️ Heavy cloud/rain suppresses upside for YES."
                return True, context + " ✅ Intraday daytime path looks consistent."
                
            if m_type == "lowest":
                if side == "NO" and max_clouds < 20:
                    return False, context + " ⚠️ Clear sky: Night freezing risk for NO."
                if side == "YES" and max_clouds > 85 and max_rain > 50:
                    return True, context + " ✅ Cloud blanket supports warmer low."
                return True, context + " ✅ Intraday overnight path looks consistent."
                
            return True, context
    except Exception as e:
        return True, f"Radar API Error: {e}"
    return True, ""

def send_telegram_message(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try: 
        res = requests.post(url, json=payload, timeout=5)
        if not res.ok:
            print(f"  [TG ERROR] {res.text}")
    except Exception as e:
        print(f"  [TG ERROR] {e}")

# =============================================================================
# EXECUTION LAYER (CLOB)
# =============================================================================
EXECUTE_REAL_TRADES = not env_flag("WEATHER_DRY_RUN", default=True)
PK = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.getenv("POLYMARKET_FUNDER", "") # usually your wallet address

def execute_sell_order(market_id, side, price, size_in_shares):
    if not EXECUTE_REAL_TRADES:
        return True # Paper trading success
        
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
    except ImportError:
        print("  [EXECUTE ERROR] py_clob_client_v2 not installed.")
        return False
        
    if not PK or not FUNDER:
        return False
        
    try:
        host = "https://clob.polymarket.com"
        chain_id = 137 # Polygon Mainnet
        
        client = ClobClient(host, key=PK, chain_id=chain_id, signature_type=2, funder=FUNDER)
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        
        # 1. Fetch market details to get Token IDs.
        import requests
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}")
        mdata = r.json()
        condition_id = mdata.get("conditionId")
        
        clob_r = requests.get(f"https://clob.polymarket.com/markets/{condition_id}")
        clob_mdata = clob_r.json()
        tokens = clob_mdata.get("tokens", [])
        
        if not tokens or len(tokens) < 2:
            return False
            
        token_id = str(tokens[0]["token_id"]) if side == "YES" else str(tokens[1]["token_id"])
        sell_price = round(max(0.01, price - 0.005), 3)

        order_args = OrderArgsV2(
            token_id=token_id,
            price=sell_price,
            size=size_in_shares,
            side="SELL",
        )
        
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp and resp.get("success"):
            print(f"  [EXECUTE SUCCESS] Placed {side} SELL order at {sell_price} for {size_in_shares} shares.")
            return True
        else:
            print(f"  [EXECUTE FAILED] API Response: {resp}")
            return False
    except Exception as e:
        print(f"  [EXECUTE EXCEPTION] {e}")
        return False


def execute_limit_order(market_id, side, price, size_in_shares):
    if not EXECUTE_REAL_TRADES:
        return True # Paper trading success
        
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
    except ImportError:
        print("  [EXECUTE ERROR] py_clob_client_v2 not installed.")
        return False
        
    if not PK or not FUNDER:
        print("  [EXECUTE ERROR] Missing POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER in .env")
        return False
        
    try:
        host = "https://clob.polymarket.com"
        chain_id = 137 # Polygon Mainnet
        
        # signature_type=2 (Polymarket Safe / Proxy wallet)
        # signature_type=1 is for standard EOA wallets
        # signature_type=0 is EIP712
        client = ClobClient(host, key=PK, chain_id=chain_id, signature_type=2, funder=FUNDER)
        creds = client.derive_api_key()
        client.set_api_creds(creds)
        

        
        # 1. Fetch market details to get Token IDs.
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}")
        mdata = r.json()
        condition_id = mdata.get("conditionId")
        
        # Get REAL token ID directly from CLOB API instead of Gamma API
        clob_r = requests.get(f"https://clob.polymarket.com/markets/{condition_id}")
        clob_mdata = clob_r.json()
        
        tokens = clob_mdata.get("tokens", [])
        
        if not tokens or len(tokens) < 2:
            print("  [EXECUTE ERROR] Cannot find token IDs for market from CLOB API.")
            return False
            
        token_id = str(tokens[0]["token_id"]) if side == "YES" else str(tokens[1]["token_id"])
        
        # 2. Build the order
        order_args = OrderArgsV2(
            token_id=token_id,
            price=price,
            size=size_in_shares,
            side="BUY",
        )
        
        # 3. Sign and post
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp and resp.get("success"):
            print(f"  [EXECUTE SUCCESS] Placed {side} limit order at {price} for {size_in_shares} shares.")
            return True
        else:
            print(f"  [EXECUTE FAILED] API Response: {resp}")
            return False
            
    except Exception as e:
        print(f"  [EXECUTE EXCEPTION] {e}")
        return False

def evaluate_trade_with_math(loc, city_name, station_name, question, bucket, price, src, temp, prob, ev, m_type, prob_below, prob_above, region, side, sigma, hours_left, balance):
    """
    Pure Quantitative Evaluation without AI.
    Executes in 0.0001s, 100% reliable.
    """
    tier = "SKIP"
    reasoning = "Сделка не прошла фильтры риска."
    is_approved = False
    expected_multiplier = 0.0
    
    edge = prob - price
    no_prob = 1.0 - prob
    no_edge = no_prob - price
    
    # Red flags
    if "sa" in region or "oc" in region: # Volatile (Sao Paulo, Buenos Aires, Wellington)
        if (side == "YES" and edge < 0.25) or (side == "NO" and no_edge < 0.05):
            return {"is_approved": False, "reasoning": "Слишком волатильный регион, edge недостаточен.", "confidence": 0, "tier": "SKIP"}
            
    # Time decay requirements
    if hours_left > 48:
        if (side == "YES" and edge < 0.25) or (side == "NO" and no_edge < 0.05):
            return {"is_approved": False, "reasoning": "Событие слишком далеко (>48ч), edge недостаточен.", "confidence": 0, "tier": "SKIP"}
    
    # We DO NOT apply strict edge decay if event is within 48 hours. 
    # Even if it's >24h (e.g. tomorrow), we allow trades if they meet basic EV/Edge criteria.
    
    if side == "YES":
        if price > 0.75:
            reasoning = f"Цена YES ({price:.3f}) слишком высокая для входа."
        elif prob < 0.42:
            reasoning = f"Вероятность YES ({prob*100:.1f}%) слишком низкая."
        elif edge < 0.08:
            reasoning = f"Недостаточный перевес YES (edge: {edge:.3f} < 0.08)."
        elif ev < 0.05:
            reasoning = f"Недостаточное мат. ожидание YES (ev: {ev:.3f} < 0.05)."
        else:
            tier = "YES_LADDER"
            is_approved = True
            expected_multiplier = round(1.0 / price, 2)
            reasoning = (
                f"YES_LADDER: модель дает {prob*100:.1f}% против цены {price*100:.1f}%. "
                f"Это похоже на stale exact-strike в центре распределения."
            )
    
    elif side == "NO":
        # TIER 2 - NO_SHORT
        # Мы переделали логику на покупку 85-94.6 NO
        # Вероятность исхода (YES) по нашей модели должна быть близка к нулю (< 0.02)
        
        # Детализируем причину отказа для отладки
        if price < 0.85:
            reasoning = f"Цена NO ({price:.3f}) ниже минимального порога 0.85."
        elif price > 0.96:
            reasoning = f"Цена NO ({price:.3f}) выше максимального порога 0.96."
        elif prob >= 0.07:
            reasoning = f"Вероятность исхода ({prob*100:.1f}%) слишком высока (нужно < 7.0%)."
        elif no_edge < 0.01:
            reasoning = f"Недостаточный перевес (edge: {no_edge:.3f} < 0.01)."
        elif ev < 0.01:
            reasoning = f"Отрицательное мат. ожидание (ev: {ev:.3f} < 0.01)."
            
        if 0.85 <= price <= 0.96 and prob < 0.07 and no_edge >= 0.01 and ev >= 0.01:
            tier = "NO_SHORT"
            is_approved = True
            expected_multiplier = round(1.0 / price, 2)
            reasoning = f"NO_SHORT (90+ Strategy): Сбор копеек. Рынок дает этому исходу шанс {(1.0-price)*100:.1f}%, но модель дает меньше 7% ({prob*100:.1f}%). Покупаем NO за {price*100:.1f} центов."

    return {
        "is_approved": is_approved, 
        "reasoning": reasoning, 
        "confidence": 95 if is_approved else 0,
        "tier": tier,
        "expected_multiplier": expected_multiplier
    }


# =============================================================================
# CONFIG
# =============================================================================


BALANCE          = 100.0
MAX_BET          = 15.0   # 15% от 100
MIN_EV           = 0.02   # Снижено для стратегии сбора копеек на NO (90+ центов)
MAX_PRICE        = 0.96
KELLY_FRACTION   = 0.40   # агрессивнее — маленький депозит, нужен разгон
MIN_VOLUME       = 200
MIN_HOURS        = 2.0
MAX_HOURS        = 72.0
MAX_SLIPPAGE     = 0.20
MIN_SIGMA_TROPICAL = 1.5
SCAN_INTERVAL    = int(os.getenv("WEATHER_SCAN_INTERVAL_MINUTES", 10)) * 60
CALIBRATION_MIN  = 5
VC_KEY           = ""


SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "tz": "America/Chicago", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "tz": "America/New_York", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "tz": "America/Chicago", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "tz": "America/Los_Angeles", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "tz": "Europe/London", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "tz": "Europe/Paris", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "tz": "Europe/Berlin", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "tz": "Europe/Istanbul", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "tz": "Asia/Seoul", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "tz": "Asia/Tokyo", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "tz": "Asia/Singapore", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "tz": "America/Toronto", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "tz": "America/Sao_Paulo", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "tz": "America/Argentina/Buenos_Aires", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "tz": "Pacific/Auckland", "region": "oc"},
    "hong-kong":    {"lat": 22.3080, "lon": 113.9185, "name": "Hong Kong",    "station": "VHHH", "unit": "C", "region": "tropical"},
    "beijing":      {"lat": 40.0799, "lon": 116.5879, "name": "Beijing",      "station": "ZBAA", "unit": "C", "tz": "Asia/Shanghai", "region": "asia"},
    "wuhan":        {"lat": 30.7831, "lon": 114.2079, "name": "Wuhan",        "station": "ZHHH", "unit": "C", "tz": "Asia/Shanghai", "region": "asia"},
    "jakarta":      {"lat": -6.1256, "lon": 106.6559, "name": "Jakarta",      "station": "WIII", "unit": "C", "tz": "Etc/GMT-7", "region": "tropical"},
    "madrid":       {"lat": 40.4719, "lon":  -3.5626, "name": "Madrid",       "station": "LEMD", "unit": "C", "tz": "Europe/Madrid", "region": "eu"},
    "milan":        {"lat": 45.6306, "lon":   8.7231, "name": "Milan",        "station": "LIMC", "unit": "C", "region": "eu"},
    "moscow":       {"lat": 55.9736, "lon":  37.4125, "name": "Moscow",       "station": "UUEE", "unit": "C", "tz": "Europe/Moscow", "region": "eu"},
    "panama-city":  {"lat":  9.0714, "lon": -79.3836, "name": "Panama City",  "station": "MPMG", "unit": "C", "tz": "America/Panama", "region": "tropical"},
    "karachi":      {"lat": 24.9008, "lon":  67.1681, "name": "Karachi",      "station": "OPKC", "unit": "C", "tz": "Asia/Karachi", "region": "asia"},
    "austin":       {"lat": 30.1975, "lon": -97.6664, "name": "Austin",       "station": "KAUS", "unit": "F", "tz": "America/Chicago", "region": "us"},
    "denver":       {"lat": 39.8561, "lon": -104.6737,"name": "Denver",       "station": "KDEN", "unit": "F", "tz": "America/Denver", "region": "us"},
    "cape-town":    {"lat": -33.9249, "lon": 18.4241, "name": "Cape Town",    "station": "FACT", "unit": "C", "region": "af"},
    "busan":        {"lat": 35.1796,  "lon": 129.0756, "name": "Busan",       "station": "RKPK", "unit": "C", "tz": "Asia/Seoul", "region": "asia"},
    "istanbul":     {"lat": 41.0082,  "lon": 28.9784,  "name": "Istanbul",    "station": "LTFM", "unit": "C", "tz": "Europe/Istanbul", "region": "eu"},
    "chengdu":      {"lat": 30.5728,  "lon": 104.0668, "name": "Chengdu",     "station": "ZUUU", "unit": "C", "tz": "Asia/Shanghai", "region": "asia"},
    "chongqing":    {"lat": 29.5628,  "lon": 106.5516, "name": "Chongqing",   "station": "ZUCK", "unit": "C", "tz": "Asia/Shanghai", "region": "asia"},
    "guangzhou":    {"lat": 23.1291,  "lon": 113.2644, "name": "Guangzhou",   "station": "ZGGG", "unit": "C", "tz": "Asia/Shanghai", "region": "tropical"},
    "helsinki":     {"lat": 60.1695,  "lon": 24.9354,  "name": "Helsinki",    "station": "EFHK", "unit": "C", "tz": "Europe/Helsinki", "region": "eu"},
    "taipei":       {"lat": 25.0330,  "lon": 121.5654, "name": "Taipei",      "station": "RCTP", "unit": "C", "tz": "Asia/Taipei", "region": "asia"},
    "houston":      {"lat": 29.7604,  "lon": -95.3698, "name": "Houston",     "station": "KIAH", "unit": "F", "tz": "America/Chicago", "region": "us"},
    "amsterdam":    {"lat": 52.3676,  "lon": 4.9041,   "name": "Amsterdam",   "station": "EHAM", "unit": "C", "tz": "Europe/Amsterdam", "region": "eu"},
    "shenzhen":     {"lat": 22.5431,  "lon": 114.0579, "name": "Shenzhen",    "station": "ZGSZ", "unit": "C", "region": "tropical"},
    "jeddah":       {"lat": 21.4858,  "lon": 39.1925,  "name": "Jeddah",      "station": "OEJN", "unit": "C", "region": "me"},
    "lagos":        {"lat": 6.5244,   "lon": 3.3792,   "name": "Lagos",       "station": "DNMM", "unit": "C", "tz": "Africa/Lagos", "region": "af"},
    "kuala-lumpur": {"lat": 3.1390,   "lon": 101.6869, "name": "Kuala Lumpur","station": "WMKK", "unit": "C", "region": "tropical"},
    "san-francisco":{"lat": 37.7749,  "lon": -122.4194,"name": "San Francisco","station": "KSFO", "unit": "F", "tz": "America/Los_Angeles", "region": "us"},
    "warsaw":       {"lat": 52.2297,  "lon": 21.0122,  "name": "Warsaw",      "station": "EPWA", "unit": "C", "tz": "Europe/Warsaw", "region": "eu"},
    "los-angeles":  {"lat": 34.0522,  "lon": -118.2437,"name": "Los Angeles", "station": "KLAX", "unit": "F", "tz": "America/Los_Angeles", "region": "us"},
    "manila":       {"lat": 14.5995,  "lon": 120.9842, "name": "Manila",      "station": "RPLL", "unit": "C", "tz": "Asia/Manila", "region": "tropical"},
    "mexico-city":  {"lat": 19.4326,  "lon": -99.1332, "name": "Mexico City", "station": "MMMX", "unit": "C", "tz": "America/Mexico_City", "region": "sa"}
}

# Start with the strongest observed cities from the analyzed wallet.
ACTIVE_CITIES = {
    "paris",
    "lucknow",
    "seoul",
}

CITY_RISK_PROFILES = {
    "paris": {
        "min_volume": 400,
        "max_slippage": 0.10,
        "yes_max_price": 0.68,
        "yes_min_prob": 0.48,
        "yes_min_edge": 0.10,
        "yes_min_ev": 0.06,
        "no_min_price": 0.87,
        "no_max_price": 0.96,
        "no_max_prob": 0.06,
        "no_min_edge": 0.015,
        "no_min_ev": 0.015,
        "position_shares": 12.0,
        "stop_loss_mult": 0.88,
        "take_profit_yes_fast": 0.78,
        "take_profit_no_fast": None,
    },
    "lucknow": {
        "min_volume": 300,
        "max_slippage": 0.12,
        "yes_max_price": 0.72,
        "yes_min_prob": 0.45,
        "yes_min_edge": 0.09,
        "yes_min_ev": 0.05,
        "no_min_price": 0.86,
        "no_max_price": 0.96,
        "no_max_prob": 0.06,
        "no_min_edge": 0.012,
        "no_min_ev": 0.012,
        "position_shares": 10.0,
        "stop_loss_mult": 0.84,
        "take_profit_yes_fast": 0.84,
        "take_profit_no_fast": None,
    },
    "seoul": {
        "min_volume": 300,
        "max_slippage": 0.09,
        "yes_max_price": 0.64,
        "yes_min_prob": 0.50,
        "yes_min_edge": 0.11,
        "yes_min_ev": 0.07,
        "no_min_price": 0.88,
        "no_max_price": 0.96,
        "no_max_prob": 0.05,
        "no_min_edge": 0.015,
        "no_min_ev": 0.015,
        "position_shares": 12.0,
        "stop_loss_mult": 0.90,
        "take_profit_yes_fast": 0.75,
        "take_profit_no_fast": None,
    },
}

DEFAULT_CITY_RISK = {
    "min_volume": 200,
    "max_slippage": 0.20,
    "yes_max_price": 0.75,
    "yes_min_prob": 0.42,
    "yes_min_edge": 0.08,
    "yes_min_ev": 0.05,
    "no_min_price": 0.85,
    "no_max_price": 0.96,
    "no_max_prob": 0.07,
    "no_min_edge": 0.01,
    "no_min_ev": 0.01,
    "position_shares": 10.0,
    "stop_loss_mult": 0.80,
    "take_profit_yes_fast": 0.80,
    "take_profit_no_fast": None,
}


def get_city_risk(city_slug):
    profile = dict(DEFAULT_CITY_RISK)
    profile.update(CITY_RISK_PROFILES.get(city_slug, {}))
    return profile

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

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability calculated via Normal Distribution CDF with continuity correction."""
    s = sigma or 2.0
    f = float(forecast)
    
    # Continuity correction for inclusive bounds
    low_bound = t_low - 0.5 if t_low != -999 else -999
    high_bound = t_high + 0.5 if t_high != 999 else 999
    
    if t_low == -999:
        prob = norm_cdf((high_bound - f) / s)
    elif t_high == 999:
        prob = 1.0 - norm_cdf((low_bound - f) / s)
    else:
        # P(t_low <= T <= t_high)
        p_high = norm_cdf((high_bound - f) / s)
        p_low  = norm_cdf((low_bound - f) / s)
        prob   = p_high - p_low

    return max(0.0, min(prob, 0.99))

def calc_ev_yes(p, ask_yes):
    if ask_yes <= 0 or ask_yes >= 1: return 0.0
    return round(p * (1.0 / ask_yes - 1.0) - (1.0 - p), 4)

def calc_ev_no(p, ask_no):
    # For NO, our probability of winning is (1 - p)
    if ask_no <= 0 or ask_no >= 1: return 0.0
    return round((1.0 - p) * (1.0 / ask_no - 1.0) - p, 4)

def calc_kelly_yes(p, ask_yes):
    if ask_yes <= 0 or ask_yes >= 1: return 0.0
    b = 1.0 / ask_yes - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def calc_kelly_no(p, ask_no):
    if ask_no <= 0 or ask_no >= 1: return 0.0
    p_no = 1.0 - p
    b = 1.0 / ask_no - 1.0
    f = (p_no * b - (1.0 - p_no)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="moe"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def get_bias(city_slug, source="moe"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key].get("bias", 0.0)
    return 0.0

def run_calibration(markets):
    """Recalculates sigma and bias from resolved markets."""
    resolved = [m for m in markets if m.get("resolved") and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["moe", "metar"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            abs_errors = []
            raw_errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s["source"] == source), None)
                if snap and snap.get("temp") is not None:
                    abs_errors.append(abs(snap["temp"] - m["actual_temp"]))
                    # raw_error = actual - forecast (if actual is 45 and forecast is 43, raw_error = +2)
                    raw_errors.append(m["actual_temp"] - snap["temp"])
            
            if len(abs_errors) < CALIBRATION_MIN:
                continue
            
            mae  = sum(abs_errors) / len(abs_errors)
            bias = sum(raw_errors) / len(raw_errors)
            
            key  = f"{city}_{source}"
            old_sigma = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            old_bias  = cal.get(key, {}).get("bias", 0.0)
            
            new_sigma = round(mae, 3)
            new_bias  = round(bias, 3)
            
            cal[key] = {
                "sigma": new_sigma, 
                "bias": new_bias,
                "n": len(abs_errors), 
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            
            if abs(new_sigma - old_sigma) > 0.05 or abs(new_bias - old_bias) > 0.1:
                updated.append(f"{LOCATIONS[city]['name']} {source}: Σ {old_sigma:.2f}->{new_sigma:.2f}, Bias {old_bias:.2f}->{new_bias:.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS (MoE)
# =============================================================================

def get_moe_forecast(city_slug, dates):
    """
    Mixture of Experts (MoE) forecast gathering data from 5 different global models:
    ECMWF, GFS (HRRR for US), ICON, JMA, UKMO.
    Blends them dynamically based on historical reliability for the region.
    """
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    
    models_str = "ecmwf_ifs025,gfs_seamless,icon_seamless,jma_seamless,ukmo_seamless"
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models={models_str}"
    )
    
    result = {}
    for attempt in range(5):
        try:
            data = requests.get(url, timeout=(10, 30)).json()
            if "error" not in data:
                daily = data.get("daily", {})
                times = daily.get("time", [])
                
                # Define weights based on region
                # Europe/RoW: ECMWF and ICON are very strong
                # US: GFS (which includes HRRR) and ECMWF are strongest
                # Asia: JMA and ECMWF
                if loc["region"] == "us":
                    w = {"ecmwf": 0.35, "gfs": 0.40, "icon": 0.15, "jma": 0.0, "ukmo": 0.10}
                elif loc["region"] == "eu":
                    w = {"ecmwf": 0.45, "gfs": 0.10, "icon": 0.25, "jma": 0.0, "ukmo": 0.20}
                elif loc["region"] == "asia":
                    w = {"ecmwf": 0.40, "gfs": 0.15, "icon": 0.10, "jma": 0.30, "ukmo": 0.05}
                else:
                    w = {"ecmwf": 0.45, "gfs": 0.25, "icon": 0.15, "jma": 0.05, "ukmo": 0.10}
                
                for i, date in enumerate(times):
                    if date not in dates:
                        continue
                        
                    def blend(prefix):
                        total_weight = 0
                        blended = 0
                        for model, weight in w.items():
                            key = f"{prefix}_ecmwf_ifs025" if model == "ecmwf" else f"{prefix}_{model}_seamless"
                            val_list = daily.get(key, [])
                            if i < len(val_list) and val_list[i] is not None:
                                blended += val_list[i] * weight
                                total_weight += weight
                        return blended / total_weight if total_weight > 0 else None

                    moe_max = blend("temperature_2m_max")
                    moe_min = blend("temperature_2m_min")
                    
                    bias = get_bias(city_slug, "moe")
                    if moe_max is not None: moe_max += bias
                    if moe_min is not None: moe_min += bias
                    
                    if moe_max is not None and moe_min is not None:
                        result[date] = {
                            "highest": round(moe_max, 1) if unit == "C" else round(moe_max),
                            "lowest": round(moe_min, 1) if unit == "C" else round(moe_min)
                        }
            break
        except Exception as e:
            if attempt < 4:
                time.sleep(5 + attempt * 2)  # backoff
            else:
                print(f"  [MoE] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    for attempt in range(3):
        try:
            url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
            data = requests.get(url, timeout=(10, 20)).json()
            if data and isinstance(data, list):
                temp_c = data[0].get("temp")
                if temp_c is not None:
                    val = round(float(temp_c) * 9/5 + 32) if unit == "F" else round(float(temp_c), 1)
                    return {"highest": val, "lowest": val}
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Actual temperature via Visual Crossing for closed markets."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(10, 20)).json()
            days = data.get("days", [])
            if days and days[0].get("tempmax") is not None:
                return round(float(days[0]["tempmax"]), 1)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year, m_type="highest"):
    slug = f"{m_type}-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# Each market is stored in a separate file: data/markets/{city}_{date}.json
# =============================================================================

def market_path(city_slug, date_str, m_type="highest"):
    return MARKETS_DIR / f"{city_slug}_{date_str}_{m_type}.json"

def load_market(city_slug, date_str, m_type="highest"):
    p = market_path(city_slug, date_str, m_type)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"], market.get("m_type", "highest"))
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours, m_type="highest"):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "m_type":             m_type,
        "slug":               event.get("slug", ""),
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
# =============================================================================

def load_state():
    defaults = {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = dict(defaults)
            state.update(loaded)
            return state
        except Exception:
            return dict(defaults)
    return dict(defaults)

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================


import zoneinfo

def get_metar_extremes(city_slug, target_date_str):
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    tz_name = loc.get("tz", "UTC")
    
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
        
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=24"
    try:
        data = requests.get(url, timeout=5).json()
    except Exception:
        return None
        
    if not data or not isinstance(data, list):
        return None
        
    temps = []
    for obs in data:
        obs_time = obs.get("obsTime")
        if not obs_time: continue
        dt_utc = datetime.fromtimestamp(obs_time, tz=timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        
        if dt_local.strftime("%Y-%m-%d") == target_date_str:
            temp_c = obs.get("temp")
            if temp_c is not None:
                val = float(temp_c) * 9/5 + 32 if unit == "F" else float(temp_c)
                temps.append(val)
                
    if temps:
        return {"max": round(max(temps), 1), "min": round(min(temps), 1)}
    return None

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot."""
    now_str = datetime.now(timezone.utc).isoformat()
    moe     = get_moe_forecast(city_slug, dates)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "moe":   moe.get(date),
            "metar": get_metar(city_slug) if date == today else None,
        }
        
        if snap["moe"] is not None:
            snap["best"] = snap["moe"]
            snap["best_source"] = "moe"
        else:
            snap["best"] = None
            snap["best_source"] = None
            
        snapshots[date] = snap
    return snapshots

import concurrent.futures
import threading

state_lock = threading.Lock()
print_lock = threading.Lock()

def process_city(city_slug, loc, now):
    local_new_pos = 0
    city_risk = get_city_risk(city_slug)
    unit = loc["unit"]
    unit_sym = "F" if unit == "F" else "C"
    
    with print_lock:
        print(f"  -> {loc['name']}...", end=" ", flush=True)

    try:
        
        tz_name = loc.get("tz", "UTC")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now_local = datetime.now(tz)
        dates = [(now_local + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]

        snapshots = take_forecast_snapshot(city_slug, dates)
        time.sleep(0.3)
    except Exception as e:
        with print_lock:
            print(f"skipped ({e})")
        return 0

    with print_lock:
        print("ok")

    for i, date in enumerate(dates):
        dt    = datetime.strptime(date, "%Y-%m-%d")
        
        for m_type in ["highest", "lowest"]:
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year, m_type)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            mkt = load_market(city_slug, date, m_type)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours, m_type)

            if mkt["status"] == "resolved":
                continue

            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    import json
                    prices = json.loads(market.get("outcomePrices", "[\"0.5\",\"0.5\"]"))
                    price_yes = float(prices[0])
                    price_no  = float(prices[1]) if len(prices) > 1 else (1.0 - price_yes)
                except Exception:
                    continue
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "range":     rng,
                    "bid":       round(price_yes, 4),
                    "ask":       round(price_yes, 4),
                    "price":     round(price_yes, 4),
                    "price_no":  round(price_no, 4),
                    "spread":    0.0,
                    "volume":    round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "moe":         snap.get("moe"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            best_dict = snap.get("best")
            forecast_temp = best_dict.get(m_type) if isinstance(best_dict, dict) else None
            best_source   = snap.get("best_source")

            raw_forecast_temp = forecast_temp
            baseline_val = None
            
            if forecast_temp is not None and calculate_baseline is not None:
                mean_max, mean_min, sigma_max, sigma_min = calculate_baseline(city_slug, date, unit=loc["unit"])
                if mean_max is not None and mean_min is not None:
                    baseline_temp = mean_max if m_type == "highest" else mean_min
                    baseline_val = baseline_temp
                    
                    hist_sigma = sigma_max if m_type == "highest" else sigma_min
                    if hist_sigma and 0.5 <= hist_sigma <= 6.0:
                        current_sigma = get_sigma(city_slug, best_source or "moe")
                        dynamic_sigma = round((current_sigma + hist_sigma) / 2.0, 2)
                    else:
                        dynamic_sigma = get_sigma(city_slug, best_source or "moe")
                        
                    adjusted_temp = linear_regression_predict(forecast_temp, baseline_temp, momentum_weight=0.15)
                    adjusted_temp = round(adjusted_temp, 1)
                    
                    if forecast_temp != adjusted_temp:
                        with print_lock:
                            print(f"      [ML] {city_slug} | Forecast: {forecast_temp}° -> ML Adjusted: {adjusted_temp}° | Baseline: {baseline_temp:.1f}° | Sigma: {dynamic_sigma}")
                    forecast_temp = adjusted_temp
                    sigma = dynamic_sigma
                else:
                    sigma = get_sigma(city_slug, best_source or "moe")
            else:
                sigma = get_sigma(city_slug, best_source or "moe")

            if loc.get("region") == "tropical" and sigma is not None and sigma < MIN_SIGMA_TROPICAL:
                sigma = MIN_SIGMA_TROPICAL

            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                best_signal = None
                ladder_signals = build_ladder_signals(outcomes, forecast_temp, sigma, unit_sym)
                for signal in ladder_signals:
                    volume = next((o["volume"] for o in outcomes if o["market_id"] == signal["market_id"]), 0)
                    if volume < city_risk["min_volume"]:
                        continue

                    current_source = best_source
                    if i == 0:
                        extremes = get_metar_extremes(city_slug, date)
                        if extremes:
                            if m_type == "highest" and extremes["max"] > signal["bucket_high"]:
                                signal["p"] = 0.0001
                                signal["forecast_src"] = f"METAR(>{extremes['max']})"
                            elif m_type == "lowest" and extremes["min"] < signal["bucket_low"]:
                                signal["p"] = 0.0001
                                signal["forecast_src"] = f"METAR(<{extremes['min']})"
                            else:
                                signal["forecast_src"] = current_source
                        else:
                            signal["forecast_src"] = current_source
                    else:
                        signal["forecast_src"] = current_source

                    signal["sigma"] = sigma
                    signal["forecast_temp"] = forecast_temp
                    signal["raw_forecast"] = raw_forecast_temp
                    signal["baseline"] = baseline_val
                    signal["city_risk"] = city_risk
                    signal["status"] = "open"
                    signal["bid_at_entry"] = round(max(signal["entry_price"] - signal.get("spread", 0.0), 0.001), 4)

                    if signal["side"] == "YES":
                        signal["ev"] = round(calc_ev_yes(signal["p"], signal["entry_price"]), 4)
                        if (
                            signal["entry_price"] <= city_risk["yes_max_price"]
                            and signal["p"] >= city_risk["yes_min_prob"]
                            and signal["edge"] >= city_risk["yes_min_edge"]
                            and signal["ev"] >= city_risk["yes_min_ev"]
                        ):
                            best_signal = signal
                            break
                    else:
                        signal["ev"] = round(calc_ev_no(signal["p"], signal["entry_price"]), 4)
                        if (
                            city_risk["no_min_price"] <= signal["entry_price"] <= city_risk["no_max_price"]
                            and signal["p"] < city_risk["no_max_prob"]
                            and signal["edge"] >= city_risk["no_min_edge"]
                            and signal["ev"] >= city_risk["no_min_ev"]
                        ):
                            best_signal = signal
                            break

                if best_signal:
                    skip_position = False
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        mdata = r.json()
                        yes_ask_str = mdata.get("bestAsk")
                        yes_bid_str = mdata.get("bestBid")
                        
                        if not yes_ask_str or not yes_bid_str:
                            yes_ask = best_signal["entry_price"]
                            yes_bid = best_signal.get("bid_at_entry", best_signal["entry_price"])
                        else:
                            try:
                                yes_ask = float(yes_ask_str)
                                yes_bid = float(yes_bid_str)
                            except ValueError:
                                yes_ask = best_signal["entry_price"]
                                yes_bid = best_signal.get("bid_at_entry", best_signal["entry_price"])
                        
                        if best_signal["side"] == "YES":
                            real_ask = yes_ask
                            real_bid = yes_bid
                        else:
                            real_ask = round(1.0 - yes_bid, 4)
                            real_bid = round(1.0 - yes_ask, 4)

                        real_spread = round(real_ask - real_bid, 4)
                        if best_signal["side"] == "YES" and real_ask > city_risk["yes_max_price"]:
                            skip_position = True
                        elif best_signal["side"] == "NO" and real_ask > city_risk["no_max_price"]:
                            skip_position = True
                        elif real_spread > city_risk["max_slippage"]:
                            with print_lock:
                                print(f"  [SKIP {best_signal['side']}] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            best_signal["entry_price"]  = real_ask
                            best_signal["bid_at_entry"] = real_bid
                            best_signal["spread"]       = real_spread
                            best_signal["shares"]       = round(best_signal["cost"] / real_ask, 2)
                            if best_signal["side"] == "YES":
                                best_signal["ev"]       = round(calc_ev_yes(best_signal["p"], real_ask), 4)
                            else:
                                best_signal["ev"]       = round(calc_ev_no(best_signal["p"], real_ask), 4)
                    except Exception as e:
                        with print_lock:
                            print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")

                    if not skip_position:
                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        with print_lock:
                            print(f"  [MATH CHECK] Evaluating {loc['name']} {m_type.capitalize()} {date} | {bucket_label} | {best_signal['side']} | EV {best_signal['ev']:+.2f}")
                        
                        ai_res = evaluate_trade_with_math(
                            loc=loc, city_name=loc['name'], station_name=loc['station'],
                            question=best_signal['question'], bucket=bucket_label,
                            price=best_signal['entry_price'], src=best_signal['forecast_src'],
                            temp=best_signal['forecast_temp'], prob=best_signal['p'],
                            ev=best_signal['ev'], m_type=m_type, prob_below=best_signal['prob_below'],
                            prob_above=best_signal['prob_above'], region=loc['region'],
                            side=best_signal['side'], sigma=best_signal['sigma'],
                            hours_left=hours, balance=999.0
                        )
                        
                        if ai_res["is_approved"]:
                            is_safe, radar_context = check_tomorrow_io_nowcast(
                                loc["lat"],
                                loc["lon"],
                                m_type,
                                target_date_str=date,
                                tz_name=loc.get("tz", "UTC"),
                                side=best_signal["side"],
                                bucket_low=best_signal["bucket_low"],
                                bucket_high=best_signal["bucket_high"],
                                forecast_temp=best_signal["forecast_temp"],
                            )
                            if not is_safe:
                                with print_lock:
                                    print(f"  [REJECT RADAR] {loc['name']} {date} | {radar_context}")
                                continue
                            
                            ai_res["reasoning"] += f"\n📡 Radar: {radar_context}"
                            
                            best_signal["shares"] = city_risk["position_shares"]
                            best_signal["cost"] = round(best_signal["shares"] * best_signal["entry_price"], 2)
                            best_signal["stop_price"] = round(best_signal["entry_price"] * city_risk["stop_loss_mult"], 4)
                            
                            with state_lock:
                                current_state = load_state()
                                balance = current_state["balance"]
                                
                                if balance < best_signal["cost"]:
                                    print(f"  [REJECT] {loc['name']} {date} | Reason: Insufficient balance. Need ${best_signal['cost']:.2f}, have ${balance:.2f}")
                                else:
                                    success = execute_limit_order(
                                        market_id=best_signal["market_id"], side=best_signal["side"],
                                        price=best_signal["entry_price"], size_in_shares=best_signal["shares"]
                                    )
                                    
                                    if not success:
                                        print(f"  [REJECT] {loc['name']} {date} | Reason: Real execution failed.")
                                    else:
                                        current_state["balance"] -= best_signal["cost"]
                                        current_state["total_trades"] += 1
                                        current_state["peak_balance"] = max(current_state.get("peak_balance", current_state["balance"]), current_state["balance"])
                                        save_state(current_state)
                                        
                                        mkt["position"] = best_signal
                                        local_new_pos += 1
                                        
                                        print(f"  [BUY {best_signal['side']}]  {loc['name']} {m_type.capitalize()} {horizon} {date} | {bucket_label} | "
                                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper() if best_signal['forecast_src'] else 'N/A'})")
                                        
                                        m_slug = mkt.get("slug", "")
                                        market_url = f"https://polymarket.com/event/{m_slug}" if m_slug else "https://polymarket.com/"
                                        
                                        tg_msg = f"🌡 <b>WeatherBet: {loc['name']} ({m_type.capitalize()})</b>\n"
                                        tg_msg += f"❓ <a href='{market_url}'>{best_signal['question']}</a>\n"
                                        tg_msg += f"🎯 <b>{bucket_label}</b>\n\n"
                                        tg_msg += f"📊 <b>Данные:</b>\n"
                                        tg_msg += f"• MoE Raw ({best_signal['forecast_src'] if best_signal['forecast_src'] else 'N/A'}): <b>{best_signal.get('raw_forecast', 'N/A')}°</b>\n"
                                        tg_msg += f"• Baseline (Norm): <b>{best_signal.get('baseline', 'N/A')}°</b> (Σ {best_signal['sigma']})\n"
                                        tg_msg += f"• ML Adjusted: <b>{best_signal['forecast_temp']}°</b>\n"
                                        tg_msg += f"• Вероятность YES: {best_signal['p']*100:.1f}%\n"
                                        tg_msg += f"• Цена {best_signal['side']}: {best_signal['entry_price']*100:.1f}%\n"
                                        tg_msg += f"  <i>(Ниже: {best_signal['prob_below']}, Выше: {best_signal['prob_above']})</i>\n"
                                        tg_msg += f"• EV: <b>{best_signal['ev']:+.2f}</b>\n\n"
                                        tg_msg += f"🤖 <b>Math Валидатор ({ai_res.get('tier', 'UNKNOWN')}):</b>\n<i>{ai_res['reasoning']}</i>\n"
                                        tg_msg += f"🎯 <b>Ожидаемый мультипликатор:</b> x{ai_res.get('expected_multiplier', 1.0)}\n"
                                        
                                        send_telegram_message(tg_msg)
                        else:
                            with print_lock:
                                print(f"  [REJECT] {loc['name']} {date} | Reason: {ai_res['reasoning']}")

            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            # Writing to city-specific file is thread-safe
            save_market(mkt)

    return local_new_pos

def scan_and_update():
    global _cal
    now = datetime.now(timezone.utc)
    state = load_state()
    new_pos = 0
    closed = 0
    resolved = 0

    active_items = [(city_slug, loc) for city_slug, loc in LOCATIONS.items() if city_slug in ACTIVE_CITIES]

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_city, city_slug, loc, now) for city_slug, loc in active_items]
        for future in concurrent.futures.as_completed(futures):
            try:
                new_pos += future.result()
            except Exception as e:
                print(f"Error in city thread: {e}")

    # --- AUTO-RESOLUTION ---
    def resolve_market(mkt, now):
        if mkt["status"] == "resolved": return None
        pos = mkt.get("position")
        if not pos or pos.get("status") != "open": return None
        market_id = pos.get("market_id")
        if not market_id: return None

        won = check_market_resolved(market_id)
        if won is None: return None

        we_won = (won == True) if pos.get("side", "YES") == "YES" else (won == False)
        
        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if we_won else round(-size, 2)

        pos["exit_price"]   = 1.0 if we_won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if we_won else "loss"

        with state_lock:
            state = load_state()
            state["balance"] += size + pnl
            if we_won:
                state["wins"] += 1
            else:
                state["losses"] += 1
            state["peak_balance"] = max(state.get("peak_balance", state["balance"]), state["balance"])
            save_state(state)

        result = "WIN" if we_won else "LOSS"
        with print_lock:
            print(f"  [{result} {pos.get('side', 'YES')}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

        save_market(mkt)
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(resolve_market, mkt, now) for mkt in load_all_markets()]
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                if res:
                    resolved += 1
            except Exception as e:
                print(f"Error resolving: {e}")

    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved


def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 60  # monitor positions every 10 minutes

def monitor_positions():
    """Quick stop check on open positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]
        is_yes = pos.get("side", "YES") == "YES"
        city_risk = get_city_risk(mkt["city"])

        # Fetch real bestBid from Polymarket API — actual sell price
        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            # If we hold YES, we want to sell YES (look at YES bid)
            # If we hold NO, we want to sell NO (look at NO bid = 1 - YES ask)
            if is_yes:
                best_bid = mdata.get("bestBid")
                if best_bid is not None:
                    current_price = float(best_bid)
            else:
                best_ask = mdata.get("bestAsk")
                if best_ask is not None:
                    current_price = 1.0 - float(best_ask)
        except Exception:
            pass

        # Fallback to cached price if API failed
        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    if is_yes:
                        current_price = o.get("bid", o["price"])
                    else:
                        current_price = 1.0 - o.get("ask", 1.0 - o["price_no"])
                    break

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * city_risk["stop_loss_mult"])
        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])

        # Hours left to resolution
        end_date = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        # Take-profit threshold based on hours to resolution
        if not is_yes:
            # Стратегия сбора копеек на NO: держим до конца, так как профит и так мал
            take_profit = city_risk.get("take_profit_no_fast")
        elif hours_left <= 8:
            take_profit = city_risk.get("take_profit_yes_fast")
        elif entry < 0.15:
            # Снайперские ставки — держим до резолюции, НЕ продаём досрочно
            take_profit = None if hours_left > 6 else 0.95
        elif entry < 0.20:
            take_profit = min(entry * 2.5, 0.90)  # было entry*2.0
        elif entry < 0.35:
            take_profit = 0.80  # было 0.70
        else:
            if hours_left < 24:
                take_profit = None
            elif hours_left < 48:
                take_profit = 0.88
            else:
                take_profit = 0.78

        # Check take-profit
        take_triggered = take_profit is not None and current_price >= take_profit
        
        # Check Stop Loss (only for NO_SHORT 90+ Strategy)
        stop_triggered = False
        if entry >= 0.85 and current_price <= 0.64:
            stop_triggered = True

        if take_triggered or stop_triggered:
            # TRY TO SELL ON REAL POLYMARKET FIRST
            sell_success = execute_sell_order(mid, pos.get("side", "YES"), current_price, pos["shares"])
            
            if not sell_success:
                with print_lock:
                    print(f"  [SELL FAILED] Could not execute sell for {city_name} {mkt['date']}. Keeping position open.")
                continue

            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            
            if stop_triggered:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
            else:
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
                
            pos["exit_price"]   = current_price
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            closed += 1
            print(f"  [{reason} {pos.get('side', 'YES')}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(ACTIVE_CITIES)} active / {len(LOCATIONS)} total")
    print(f"  Universe:   {', '.join(LOCATIONS[c]['name'] for c in sorted(ACTIVE_CITIES))}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    MoE (5 Models) + METAR(D+0)")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except Exception as e:
                print(f"  Error in scan: {e}")
        else:
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    else:
        print("Usage: python bot.py [run|status|report]")
