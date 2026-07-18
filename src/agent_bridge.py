#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error

API_BASE = "http://127.0.0.1:18910/api"

def print_help():
    print("Antigravity Predictor Agent Bridge")
    print("Usage: python3 agent_bridge.py <command> [args]")
    print("\nCommands:")
    print("  status            Fetch live predictor server status, current prediction & active signal.")
    print("  trades [limit]    Fetch simulated trades history log (default limit: 5).")
    print("  candles [limit]   Fetch the latest candle buffers (default limit: 5).")
    print("  config            Show the active config.json parameters.")
    print("  set-param <k> <v> Update a parameter in config.json (e.g. buy_threshold 0.33).")
    print("  help              Show this help menu.")

def fetch_json(endpoint):
    url = f"{API_BASE}/{endpoint}"
    try:
        with urllib.request.urlopen(url, timeout=3) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.URLError as e:
        print(f"Error: Failed to connect to Predictor Server at {url}. Is it running?")
        print(f"Details: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

def cmd_status():
    status = fetch_json("status")
    print(json.dumps(status, indent=2))

def cmd_trades(limit=5):
    trades = fetch_json("trades")
    # Latest first
    trades.reverse()
    print(json.dumps(trades[:limit], indent=2))

def cmd_candles(limit=5):
    candles = fetch_json("candles")
    # Latest first
    candles.reverse()
    print(json.dumps(candles[:limit], indent=2))

def cmd_config():
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
            print(json.dumps(cfg, indent=2))
    except Exception as e:
        print(f"Error reading config.json: {e}")

def cmd_set_param(key, value):
    try:
        # Load existing config
        with open("config.json", "r") as f:
            cfg = json.load(f)
            
        # Parse value to float/int/bool if possible
        if value.lower() == 'true':
            parsed_val = True
        elif value.lower() == 'false':
            parsed_val = False
        else:
            try:
                if '.' in value:
                    parsed_val = float(value)
                else:
                    parsed_val = int(value)
            except ValueError:
                parsed_val = value # Keep as string
                
        # Handle nested server config if key starts with server.
        if key.startswith("server."):
            sub_key = key.split(".")[1]
            cfg["server"][sub_key] = parsed_val
        else:
            cfg[key] = parsed_val
            
        with open("config.json", "w") as f:
            json.dump(cfg, f, indent=2)
            
        print(f"Success: Parameter '{key}' updated to {parsed_val} in config.json.")
        print("Note: You may need to restart the predictor_server.py process for changes to take effect.")
    except Exception as e:
        print(f"Error updating config.json: {e}")

def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)
        
    cmd = sys.argv[1].lower()
    
    if cmd == "status":
        cmd_status()
    elif cmd == "trades":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_trades(limit)
    elif cmd == "candles":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        cmd_candles(limit)
    elif cmd == "config":
        cmd_config()
    elif cmd == "set-param":
        if len(sys.argv) < 4:
            print("Error: Missing key and value arguments. Usage: set-param <key> <value>")
            sys.exit(1)
        cmd_set_param(sys.argv[2], sys.argv[3])
    elif cmd in ("help", "-h", "--help"):
        print_help()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
