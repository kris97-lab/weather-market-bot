import json
import glob
import os
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
FUNDER = os.getenv("POLYMARKET_FUNDER", "")

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Use Cloudflare or public rpc that isn't blocking us
RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic"
]

def get_usdc_balance(address):
    if not address: return 0.0
    addr_padded = address.replace("0x", "").zfill(64)
    data = "0x70a08231" + addr_padded
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDC_E, "data": data}, "latest"],
        "id": 1
    }
    
    for rpc in RPC_URLS:
        try:
            r = requests.post(rpc, json=payload, timeout=5)
            res = r.json()
            if "result" in res and not res.get("error"):
                return int(res["result"], 16) / 1_000_000
        except:
            continue
            
    return 0.0

def get_real_positions():
    if not FUNDER: return {}
    try:
        url = f"https://data-api.polymarket.com/positions?user={FUNDER}"
        r = requests.get(url, timeout=5)
        data = r.json()
        # Return a dict mapping title -> position data
        return { p["title"]: p for p in data }
    except:
        return {}

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            with open('dashboard.html', 'rb') as f:
                self.wfile.write(f.read())
        elif self.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            DATA_DIR = Path("data")
            state_file = DATA_DIR / "state.json"
            state = {"balance": 0, "total_trades": 0}
            if state_file.exists():
                try:
                    with open(state_file) as f:
                        state = json.load(f)
                except Exception:
                    pass
            
            # OVERRIDE BALANCE WITH REAL USDC.e BALANCE
            real_balance = get_usdc_balance(FUNDER)
            state["balance"] = real_balance
                    
            markets = []
            for f in glob.glob("data/markets/*.json"):
                try:
                    with open(f) as file:
                        markets.append(json.load(file))
                except Exception:
                    pass
                    
            real_positions_map = get_real_positions()
            
            open_positions = []
            closed_positions = []
            matched_real_titles = set()
            
            for m in markets:
                pos = m.get("position")
                if pos:
                    pos['city'] = m.get('city', '')
                    pos['event_date'] = m.get('date', '')
                    pos['m_type'] = m.get('type', '')
                    
                    title = pos.get("question", "")
                    
                    # Try to find this position in real data
                    real_pos = real_positions_map.get(title)
                    
                    if pos.get('status', 'open') == 'open':
                        if real_pos and real_pos.get("size", 0) > 0:
                            # Update with real data
                            matched_real_titles.add(title)
                            pos['real_shares'] = real_pos["size"]
                            pos['real_pnl'] = real_pos.get("cashPnl", 0)
                            pos['real_current_price'] = real_pos.get("curPrice", 0)
                            pos['cost'] = real_pos.get("initialValue", pos['cost'])
                            open_positions.append(pos)
                        else:
                            # It's "open" locally but not on polymarket (maybe limit order not filled or we sold it manually)
                            # The user said "позиции которые равны 0 не отображать"
                            pass
                    else:
                        closed_positions.append(pos)
                        
            # 2. Add real positions from Polymarket that aren't in local markets
            for title, real_pos in real_positions_map.items():
                if real_pos.get("size", 0) > 0 and title not in matched_real_titles:
                    
                    # You only want to see weather bets, hide irrelevant markets (like Dota 2, LoL, Crypto)
                    if "temperature in" not in title.lower():
                        continue
                        
                    # This is a position we bought manually or before the bot started
                    pos = {
                        'city': 'Unknown',
                        'event_date': 'Unknown',
                        'm_type': 'manual',
                        'question': title,
                        'side': 'UNKNOWN',
                        'bucket_low': '?',
                        'bucket_high': '?',
                        'entry_price': real_pos.get("avgPrice", 0),
                        'real_shares': real_pos.get("size", 0),
                        'real_current_price': real_pos.get("curPrice", 0),
                        'real_pnl': real_pos.get("cashPnl", 0),
                        'cost': real_pos.get("initialValue", 0),
                        'status': 'open'
                    }
                    
                    # Try to parse city and date from title if it's a weather market
                    if "temperature in" in title:
                        try:
                            parts = title.split("temperature in ")[1].split(" be between ")
                            pos['city'] = parts[0].strip()
                            
                            bucket_parts = parts[1].split(" on ")
                            bucket_str = bucket_parts[0].replace("°F", "").replace("°C", "").strip()
                            pos['bucket_low'] = bucket_str.split("-")[0]
                            pos['bucket_high'] = bucket_str.split("-")[1]
                            pos['event_date'] = bucket_parts[1].replace("?", "").strip()
                            
                            # API returns "outcome": "No" or "Yes"
                            pos['side'] = real_pos.get("outcome", "UNKNOWN").upper()
                        except:
                            pass
                            
                    open_positions.append(pos)
                    
            open_positions.sort(key=lambda x: x.get('opened_at', ''), reverse=True)
            closed_positions.sort(key=lambda x: x.get('closed_at', ''), reverse=True)
            
            data = json.dumps({
                "state": state,
                "open_positions": open_positions,
                "closed_positions": closed_positions
            })
            self.wfile.write(data.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run(port=8002):
    server_address = ('', port)
    httpd = HTTPServer(server_address, SimpleHandler)
    print(f"Starting dashboard on http://localhost:{port}")
    httpd.serve_forever()

if __name__ == '__main__':
    run()

