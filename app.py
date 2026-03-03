from flask import Flask, request
import qbittorrentapi
import json
import os
import requests
import threading
import time

app = Flask(__name__)

# --- URL SANITIZER ---
def fix_url(url):
    """Ensures URLs have the http:// prefix and no trailing slash."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url.rstrip('/')

# --- ENVIRONMENT VARIABLES ---
QBT_HOST = fix_url(os.getenv('QB_HOST', 'torrent:8080'))
QBT_USER = os.getenv('QB_USER', 'Andreas')
QBT_PASS = os.getenv('QB_PASS', 'redacted')

TRACEARR_URL = fix_url(os.getenv('TRACEARR_URL', 'tracearr:3000'))
TRACEARR_TOKEN = os.getenv('TRACEARR_TOKEN', '')

SAB_HOST = fix_url(os.getenv('SAB_HOST', 'sabnzbd:8080'))
SAB_API_KEY = os.getenv('SAB_API_KEY', '')
SAB_THROTTLE_SPEED = os.getenv('SAB_THROTTLE_SPEED', '20M')
SAB_FULL_SPEED = os.getenv('SAB_FULL_SPEED', '0')

# Safely parse the sync interval (defaults to 300 seconds / 5 minutes)
try:
    TRACEARR_SYNC_INTERVAL = int(os.getenv('TRACEARR_SYNC_INTERVAL', '300'))
except ValueError:
    print("[WARNING] Invalid TRACEARR_SYNC_INTERVAL provided. Defaulting to 300 seconds.", flush=True)
    TRACEARR_SYNC_INTERVAL = 300

# --- GLOBALS ---
is_throttled = False

# Connect to qBittorrent
qbt_client = qbittorrentapi.Client(host=QBT_HOST, username=QBT_USER, password=QBT_PASS)

# --- CORE LOGIC ---
def set_throttles(enable_throttle: bool, reason: str):
    """Engages or releases throttles, preventing duplicate API calls."""
    global is_throttled
    
    # If the state isn't changing, do nothing!
    if enable_throttle == is_throttled:
        return
        
    is_throttled = enable_throttle
    
    if is_throttled:
        print(f"\n[ACTION] Engaging throttles! (Trigger: {reason})", flush=True)
    else:
        print(f"\n[ACTION] Releasing throttles (Full speed!). (Trigger: {reason})", flush=True)

    # 1. Update qBittorrent
    try:
        qbt_client.auth_log_in()
        qbt_client.transfer.set_speed_limits_mode(is_throttled)
        print(f"[SUCCESS] qBittorrent throttle set to: {is_throttled}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to communicate with qBittorrent: {e}", flush=True)
        
    # 2. Update SABnzbd
    if SAB_API_KEY:
        try:
            target_speed = SAB_THROTTLE_SPEED if is_throttled else SAB_FULL_SPEED
            sab_url = f"{SAB_HOST}/api?mode=config&name=speedlimit&value={target_speed}&apikey={SAB_API_KEY}&output=json"
            
            response = requests.get(sab_url, timeout=5)
            if response.status_code == 200:
                speed_str = "Unlimited" if str(target_speed) == "0" else target_speed
                print(f"[SUCCESS] SABnzbd speed set to: {speed_str}", flush=True)
            else:
                print(f"[ERROR] SABnzbd HTTP {response.status_code}: {response.text}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to communicate with SABnzbd: {e}", flush=True)

def sync_with_tracearr():
    """Background thread that acts as source of truth."""
    if not TRACEARR_TOKEN:
        print("[TRACEARR] No API token provided. Background sync disabled.", flush=True)
        return
        
    print(f"[TRACEARR] Background sync started. Polling every {TRACEARR_SYNC_INTERVAL} seconds.", flush=True)
    
    while True:
        try:
            headers = {
                'accept': 'application/json',
                'Authorization': f'Bearer {TRACEARR_TOKEN}'
            }
            url = f"{TRACEARR_URL}/api/v1/public/streams?summary=true"
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                total_streams = data.get('summary', {}).get('total', 0)
                
                if total_streams == 0:
                    set_throttles(False, reason="Tracearr reports 0 streams")
                else:
                    set_throttles(True, reason=f"Tracearr reports {total_streams} streams")
            else:
                print(f"[TRACEARR SYNC] Error HTTP {response.status_code}: {response.text}", flush=True)
                
        except Exception as e:
            print(f"[TRACEARR SYNC] Failed to connect to Tracearr: {e}", flush=True)
            
        time.sleep(TRACEARR_SYNC_INTERVAL)

# --- WEBHOOK ENDPOINTS ---
@app.route('/plex', methods=['POST'])
def plex_webhook():
    payload = request.form.get('payload')
    if not payload:
        return "No payload", 400
    
    try:
        data = json.loads(payload)
        event = data.get('event')
        
        if event in ['media.play', 'media.resume']:
            set_throttles(True, reason=f"Plex Webhook ({event})")
            
    except Exception as e:
        print(f"[PLEX ERROR] Failed to parse payload: {e}", flush=True)
        
    return "OK", 200

@app.route('/jellyfin', methods=['POST'])
def jellyfin_webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "No payload", 400
        
    event = data.get('NotificationType')
    
    if event in ['PlaybackStart', 'PlaybackUnpause']:
        set_throttles(True, reason=f"Jellyfin Webhook ({event})")
        
    return "OK", 200

@app.route('/emby', methods=['POST'])
def emby_webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "No payload", 400
        
    event = data.get('Event')
    
    if event in ['playback.start', 'playback.unpause']:
        set_throttles(True, reason=f"Emby Webhook ({event})")
        
    return "OK", 200

# --- INITIALIZATION ---
def start_background_threads():
    print("[SYSTEM] Initializing background sync thread...", flush=True)
    thread = threading.Thread(target=sync_with_tracearr, daemon=True)
    thread.start()

start_background_threads()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
