from flask import Flask, request
import qbittorrentapi
import json
import os
import requests
import threading
import time

app = Flask(__name__)

# Grab environment variables
QBT_HOST = os.getenv('QB_HOST', '192.168.1.69:8080')
QBT_USER = os.getenv('QB_USER', 'Andreas')
QBT_PASS = os.getenv('QB_PASS', 'redacted')

TRACEARR_URL = os.getenv('TRACEARR_URL', 'https://tracearr.nikolaisen.xyz').rstrip('/')
TRACEARR_TOKEN = os.getenv('TRACEARR_TOKEN', '')

SAB_HOST = os.getenv('SAB_HOST', '192.168.1.69:8081').rstrip('/')
SAB_API_KEY = os.getenv('SAB_API_KEY', '')
SAB_THROTTLE_SPEED = os.getenv('SAB_THROTTLE_SPEED', '20M') # 20 MB/s
SAB_FULL_SPEED = os.getenv('SAB_FULL_SPEED', '0')           # 0 means unlimited

# Connect to qBittorrent
qbt_client = qbittorrentapi.Client(host=QBT_HOST, username=QBT_USER, password=QBT_PASS)
active_sessions = set()

def update_speeds():
    """Handles throttling for both qBittorrent and SABnzbd"""
    throttle_enabled = len(active_sessions) > 0
    
    if throttle_enabled:
        print(f"\n[ACTION] Active streams: {len(active_sessions)}. Engaging throttles!", flush=True)
    else:
        print("\n[ACTION] No active streams. Releasing throttles (Full speed!).", flush=True)

    # 1. Update qBittorrent
    try:
        qbt_client.auth_log_in()
        qbt_client.transfer.set_speed_limits_mode(throttle_enabled)
        print(f"[SUCCESS] qBittorrent throttle set to: {throttle_enabled}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to communicate with qBittorrent: {e}", flush=True)
        
    # 2. Update SABnzbd
    if SAB_API_KEY:
        try:
            target_speed = SAB_THROTTLE_SPEED if throttle_enabled else SAB_FULL_SPEED
            # Ensure host has http://
            base_url = f"http://{SAB_HOST}" if not SAB_HOST.startswith('http') else SAB_HOST
            sab_url = f"{base_url}/api?mode=config&name=speedlimit&value={target_speed}&apikey={SAB_API_KEY}&output=json"
            
            response = requests.get(sab_url, timeout=5)
            if response.status_code == 200:
                speed_str = "Unlimited" if str(target_speed) == "0" else target_speed
                print(f"[SUCCESS] SABnzbd speed set to: {speed_str}", flush=True)
            else:
                print(f"[ERROR] SABnzbd HTTP {response.status_code}: {response.text}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to communicate with SABnzbd: {e}", flush=True)


def sync_with_tracearr():
    """Background thread that checks Tracearr every 5 minutes to fix missed webhooks."""
    if not TRACEARR_TOKEN:
        print("[TRACEARR] No API token provided. Background sync disabled.", flush=True)
        return
        
    print("[TRACEARR] Background sync started. Polling every 5 minutes.", flush=True)
    
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
                
                # Scenario 1: We missed a 'Stop' webhook
                if total_streams == 0 and len(active_sessions) > 0:
                    print(f"\n[TRACEARR SYNC] Mismatch! Tracearr says 0 streams, but we have {len(active_sessions)}. Clearing stale sessions.", flush=True)
                    active_sessions.clear()
                    update_speeds()
                    
                # Scenario 2: We missed a 'Start' webhook
                elif total_streams > 0 and len(active_sessions) == 0:
                    print(f"\n[TRACEARR SYNC] Mismatch! Tracearr says {total_streams} streams, but we show 0. Engaging throttle.", flush=True)
                    active_sessions.add('tracearr_fallback_session')
                    update_speeds()
            else:
                print(f"[TRACEARR SYNC] Error HTTP {response.status_code}: {response.text}", flush=True)
                
        except Exception as e:
            print(f"[TRACEARR SYNC] Failed to connect to Tracearr: {e}", flush=True)
            
        time.sleep(300)

@app.route('/plex', methods=['POST'])
def plex_webhook():
    payload = request.form.get('payload')
    if not payload:
        return "No payload", 400
    
    try:
        data = json.loads(payload)
        event = data.get('event')
        session_id = data.get('Player', {}).get('uuid', 'unknown_plex_session')
        
        print(f"[PLEX] Event: {event} | Session ID: {session_id}", flush=True)
        
        if event in ['media.play', 'media.resume']:
            active_sessions.add(session_id)
            update_speeds()
        elif event in ['media.pause', 'media.stop']:
            active_sessions.discard(session_id)
            active_sessions.discard('tracearr_fallback_session')
            update_speeds()
            
    except Exception as e:
        print(f"[PLEX ERROR] Failed to parse payload: {e}", flush=True)
        
    return "OK", 200

@app.route('/jellyfin', methods=['POST'])
def jellyfin_webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "No payload", 400
        
    event = data.get('NotificationType')
    session_id = data.get('DeviceId', 'unknown_jf_session')
    
    print(f"[JELLYFIN] Event: {event} | Session ID: {session_id}", flush=True)
    
    if event in ['PlaybackStart', 'PlaybackUnpause']:
        active_sessions.add(session_id)
        update_speeds()
    elif event in ['PlaybackStop', 'PlaybackPause']:
        active_sessions.discard(session_id)
        active_sessions.discard('tracearr_fallback_session')
        update_speeds()
        
    return "OK", 200

def start_background_threads():
    print("[SYSTEM] Initializing background sync thread...", flush=True)
    thread = threading.Thread(target=sync_with_tracearr, daemon=True)
    thread.start()

# Call it immediately
start_background_threads()

if __name__ == '__main__':
    # This only runs if you do 'python app.py' manually
    app.run(host='0.0.0.0', port=5000)