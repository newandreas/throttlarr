from flask import Flask, request
import json
import os
import re
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
QBT_USER = os.getenv('QB_USER', 'user')
QBT_PASS = os.getenv('QB_PASS', 'password')

TRACEARR_URL = fix_url(os.getenv('TRACEARR_URL', 'tracearr:3000'))
TRACEARR_TOKEN = os.getenv('TRACEARR_TOKEN', '')

SAB_HOST = fix_url(os.getenv('SAB_HOST', 'sabnzbd:8080'))
SAB_API_KEY = os.getenv('SAB_API_KEY', '')
THROTTLE_SPEED = os.getenv('THROTTLE_SPEED', os.getenv('SAB_THROTTLE_SPEED', '20M'))
FULL_SPEED = os.getenv('FULL_SPEED', os.getenv('SAB_FULL_SPEED', '0'))

# Safely parse the sync interval (defaults to 300 seconds / 5 minutes)
try:
    TRACEARR_SYNC_INTERVAL = int(os.getenv('TRACEARR_SYNC_INTERVAL', '300'))
except ValueError:
    print("[WARNING] Invalid TRACEARR_SYNC_INTERVAL provided. Defaulting to 300 seconds.", flush=True)
    TRACEARR_SYNC_INTERVAL = 300

# --- CONSTANTS ---
MAX_RECENT_SECONDS = 60 * 60 * 2  # 2 hours

# --- GLOBALS ---
is_throttled = False
historical_peak_speed = 0
queue_lock = threading.Lock()

# --- HELPERS ---

def parse_size_to_bytes(value):
    if value is None:
        return 0

    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().upper()
    if text in ('', '0', '0B'):
        return 0

    match = re.match(r'^([\d,.]+)\s*([KMG]?)B?/?S?$', text)
    if not match:
        try:
            return int(float(text))
        except ValueError:
            return 0

    number = float(match.group(1).replace(',', ''))
    unit = match.group(2)
    if unit == 'K':
        return int(number * 1024)
    if unit == 'M':
        return int(number * 1024 * 1024)
    if unit == 'G':
        return int(number * 1024 * 1024 * 1024)
    return int(number)


def format_speed_limit_bytes(bytes_per_sec):
    if bytes_per_sec == float('inf'):
        return '0'
    if bytes_per_sec <= 0:
        return '0'
    megabytes = bytes_per_sec / (1024 * 1024)
    if megabytes >= 1:
        text = f"{megabytes:.2f}".rstrip('0').rstrip('.')
        return f"{text}M"
    kilobytes = bytes_per_sec / 1024
    text = f"{kilobytes:.2f}".rstrip('0').rstrip('.')
    return f"{text}K"


def parse_priority(name):
    text = str(name).replace('_', ' ').replace('.', ' ').replace('-', ' ')

    match = re.search(r'[sS](\d{1,2})\s*[eE](\d{1,2})', text)
    if match:
        return int(match.group(1)), int(match.group(2)), 1

    match = re.search(r'(\d{1,2})[xX](\d{1,2})', text)
    if match:
        return int(match.group(1)), int(match.group(2)), 1

    match = re.search(r'SEASON\s*(\d{1,2})', text, re.IGNORECASE)
    if match:
        return int(match.group(1)), 0, 0

    match = re.search(r'^(\d{1,2})\s*of\s*\d{1,2}', text, re.IGNORECASE)
    if match:
        return int(match.group(1)), 0, 0

    return 999, 999, 2


def parse_sab_added(slot):
    for key in ('added', 'age', 'age_seconds'):
        value = slot.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            if value > 1e9:
                return int(value)
            return int(time.time() - int(value))
        if isinstance(value, str):
            if value.isdigit():
                return int(value)
            parts = value.split(':')
            if len(parts) == 3:
                try:
                    hours, minutes, seconds = [int(p) for p in parts]
                    return int(time.time() - (hours * 3600 + minutes * 60 + seconds))
                except ValueError:
                    pass
    return None


def qbt_login_session():
    session = requests.Session()
    try:
        response = session.post(
            f"{QBT_HOST}/api/v2/auth/login",
            data={'username': QBT_USER, 'password': QBT_PASS},
            timeout=10,
        )
        
        # Accept v5.2.0+ (204 No Content) and older versions (200 OK)
        if response.status_code not in (200, 204):
            raise RuntimeError(f'qBittorrent login failed: {response.status_code} {response.text}')
            
        # If it's the older version, ensure it actually says 'Ok.'
        if response.status_code == 200 and 'Ok.' not in response.text:
            raise RuntimeError(f'qBittorrent login failed: {response.status_code} {response.text}')
            
    except Exception as exc:
        raise RuntimeError(f'Failed to authenticate with qBittorrent: {exc}') from exc
    return session


def qbt_get_downloads():
    try:
        session = qbt_login_session()
        response = session.get(f"{QBT_HOST}/api/v2/torrents/info?filter=all", timeout=10)
        response.raise_for_status()
        torrents = response.json()
    except Exception as exc:
        print(f"[QBT] Failed to fetch torrents: {exc}", flush=True)
        return []

    now = int(time.time())
    downloads = []
    for torrent in torrents:
        state = torrent.get('state', '').lower()
        if state not in {'downloading', 'stalleddl', 'queueddl', 'pauseddl', 'stoppeddl', 'forceddl'}:
            continue

        added_on = int(torrent.get('added_on', 0) or 0)
        name = torrent.get('name', '')
        current_speed = int(torrent.get('dlspeed', 0) or 0)
        paused = state in {'pauseddl', 'stoppeddl'}
        
        # NEW: Grab total size (in bytes)
        total_size = int(torrent.get('size', 0) or torrent.get('total_size', 0) or 0)
        season, episode, kind = parse_priority(name)
        is_tv = kind in (0, 1)

        if now - added_on > MAX_RECENT_SECONDS:
            if paused:
                downloads.append({
                    'source': 'qbit',
                    'id': torrent.get('hash', ''),
                    'name': name,
                    'added_on': added_on,
                    'state': state,
                    'current_speed': 0,
                    'is_paused': True,
                    'total_size': total_size,
                    'is_tv': is_tv,
                    'priority': (999, 999, 999, added_on),
                    'is_limbo': True
                })
            continue

        downloads.append({
            'source': 'qbit',
            'id': torrent.get('hash', ''),
            'name': name,
            'added_on': added_on,
            'state': state,
            'current_speed': current_speed,
            'is_paused': paused,
            'total_size': total_size,
            'is_tv': is_tv,
            'priority': (season, episode, kind, added_on),
        })
    return downloads


def sab_get_downloads():
    if not SAB_API_KEY:
        return []

    try:
        response = requests.get(
            f"{SAB_HOST}/api",
            params={'mode': 'queue', 'output': 'json', 'apikey': SAB_API_KEY},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"[SAB] Failed to fetch queue: {exc}", flush=True)
        return []

    queue = data.get('queue', {})
    try:
        global_kbps = float(queue.get('kbpersec') or 0)
        global_speed_bytes = int(global_kbps * 1024)
    except ValueError:
        global_speed_bytes = 0

    slots = queue.get('slots') or []
    now = int(time.time())
    downloads = []
    applied_global_speed = False

    for slot in slots:
        status = str(slot.get('status', '')).lower()
        if status not in {'downloading', 'paused', 'queued'}:
            continue

        added_on = parse_sab_added(slot)
        if added_on is None:
            added_on = now
            
        name = slot.get('filename') or slot.get('nzo_name') or slot.get('name') or ''
        paused = status == 'paused'
        
        # NEW: Convert SAB's MB to bytes
        try:
            total_size = int(float(slot.get('mb') or 0) * 1024 * 1024)
        except ValueError:
            total_size = 0
            
        season, episode, kind = parse_priority(name)
        is_tv = kind in (0, 1)

        if now - added_on > MAX_RECENT_SECONDS:
            if paused:
                downloads.append({
                    'source': 'sab',
                    'id': slot.get('nzo_id') or slot.get('id') or name,
                    'name': name,
                    'added_on': added_on,
                    'state': status,
                    'current_speed': 0,
                    'is_paused': True,
                    'total_size': total_size,
                    'is_tv': is_tv,
                    'priority': (999, 999, 999, added_on),
                    'is_limbo': True
                })
            continue

        if status == 'downloading' and not applied_global_speed:
            current_speed = global_speed_bytes
            applied_global_speed = True
        else:
            current_speed = 0

        downloads.append({
            'source': 'sab',
            'id': slot.get('nzo_id') or slot.get('id') or name,
            'name': name,
            'added_on': added_on,
            'state': status,
            'current_speed': current_speed,
            'is_paused': paused,
            'total_size': total_size,
            'is_tv': is_tv,
            'priority': (season, episode, kind, added_on),
        })
    return downloads


def get_effective_total_speed():
    throttle_bytes = parse_size_to_bytes(THROTTLE_SPEED)
    full_bytes = parse_size_to_bytes(FULL_SPEED)

    if is_throttled:
        return float('inf') if throttle_bytes == 0 else throttle_bytes

    if full_bytes > 0:
        return full_bytes

    return historical_peak_speed or float('inf')


def apply_rate_limits(total_speed_limit, current_qbt_speed, current_sab_speed, active_items):
    global historical_peak_speed
    global is_throttled

    sab_target = total_speed_limit
    qbt_target = total_speed_limit

    # Identify the top priority client
    top_source = active_items[0]['source'] if active_items else None
    pool = historical_peak_speed if total_speed_limit == float('inf') else total_speed_limit

    if pool > 0:
        if top_source == 'sab':
            sab_target = total_speed_limit
            qbt_target = max(pool - current_sab_speed, 1024)
        elif top_source == 'qbit':
            qbt_target = total_speed_limit
            sab_target = max(pool - current_qbt_speed, 1024)

    # --- 1. APPLY SABNZBD LIMIT ---
    if SAB_API_KEY:
        target_speed_string = '0' if sab_target == float('inf') else format_speed_limit_bytes(sab_target)
        try:
            sab_url = f"{SAB_HOST}/api?mode=config&name=speedlimit&value={target_speed_string}&apikey={SAB_API_KEY}&output=json"
            response = requests.get(sab_url, timeout=5)
            if response.status_code == 200:
                speed_str = "Unlimited" if target_speed_string == "0" else target_speed_string
                print(f"[SUCCESS] SABnzbd limit set to: {speed_str}", flush=True)
            else:
                print(f"[ERROR] SABnzbd HTTP {response.status_code}: {response.text}", flush=True)
        except Exception as exc:
            print(f"[ERROR] Failed to communicate with SABnzbd: {exc}", flush=True)

    # --- 2. APPLY QBITTORRENT LIMIT ---
    qbt_limit_bytes = -1 if qbt_target == float('inf') else int(qbt_target)
    try:
        session = qbt_login_session()
        
        # 1. Update BOTH regular and alt download limits to the dynamic value via Preferences
        # This safely applies the math without destroying your custom upload limits
        prefs_payload = {
            "dl_limit": qbt_limit_bytes,
            "alt_dl_limit": qbt_limit_bytes
        }
        
        session.post(
            f"{QBT_HOST}/api/v2/app/setPreferences",
            data={'json': json.dumps(prefs_payload)},
            timeout=5
        )
        
        # 2. Toggle Alt-Speed mode based on throttle status
        # FIX: Changed 'state' to 'mode' to properly trigger the QBT API
        mode_val = 1 if is_throttled else 0
        session.post(
            f"{QBT_HOST}/api/v2/transfer/setSpeedLimitsMode",
            data={'mode': mode_val},
            timeout=5
        )
        
        speed_str = "Unlimited" if qbt_limit_bytes == -1 else format_speed_limit_bytes(qbt_limit_bytes)
        mode_str = "Alt Mode/Throttled" if is_throttled else "Regular Mode"
        print(f"[SUCCESS] qBittorrent limit set to: {speed_str} ({mode_str})", flush=True)
    except Exception as exc:
        print(f"[ERROR] Failed to set qBittorrent speed limit: {exc}", flush=True)


def qbt_toggle_torrents(active_hashes, all_items):
    try:
        session = qbt_login_session()
    except Exception as exc:
        print(exc, flush=True)
        return

    try:
        active_hash_list = [item['id'] for item in all_items if item['source'] == 'qbit' and item['id'] in active_hashes]
        pause_hash_list = [item['id'] for item in all_items if item['source'] == 'qbit' and item['id'] not in active_hashes and not item['is_paused']]
        resume_hash_list = [item['id'] for item in all_items if item['source'] == 'qbit' and item['id'] in active_hashes and item['is_paused']]

        if pause_hash_list:
            # Try qBT v5.0+ endpoint (stop)
            resp = session.post(
                f"{QBT_HOST}/api/v2/torrents/stop",
                data={'hashes': '|'.join(pause_hash_list)},
                timeout=10,
            )
            # Fallback to qBT v4.x endpoint (pause) if 404
            if resp.status_code == 404:
                resp = session.post(
                    f"{QBT_HOST}/api/v2/torrents/pause",
                    data={'hashes': '|'.join(pause_hash_list)},
                    timeout=10,
                )
            resp.raise_for_status()

        if resume_hash_list:
            # Try qBT v5.0+ endpoint (start)
            resp = session.post(
                f"{QBT_HOST}/api/v2/torrents/start",
                data={'hashes': '|'.join(resume_hash_list)},
                timeout=10,
            )
            # Fallback to qBT v4.x endpoint (resume) if 404
            if resp.status_code == 404:
                resp = session.post(
                    f"{QBT_HOST}/api/v2/torrents/resume",
                    data={'hashes': '|'.join(resume_hash_list)},
                    timeout=10,
                )
            resp.raise_for_status()
            
    except Exception as exc:
        print(f"[QBT] Failed to pause/resume torrents: {exc}", flush=True)


def sab_toggle_torrents(active_ids, all_items):
    if not SAB_API_KEY:
        return

    for item in all_items:
        if item['source'] != 'sab':
            continue

        try:
            if item['id'] in active_ids and item['is_paused']:
                requests.get(
                    f"{SAB_HOST}/api",
                    params={'mode': 'resume', 'name': item['id'], 'apikey': SAB_API_KEY},
                    timeout=10,
                )
            elif item['id'] not in active_ids and not item['is_paused']:
                requests.get(
                    f"{SAB_HOST}/api",
                    params={'mode': 'pause', 'name': item['id'], 'apikey': SAB_API_KEY},
                    timeout=10,
                )
        except Exception as exc:
            print(f"[SAB] Failed to pause/resume {item['name']}: {exc}", flush=True)


def rebalance_downloads():
    global historical_peak_speed

    with queue_lock:
        qbt_items = qbt_get_downloads()
        sab_items = sab_get_downloads()
        all_items = qbt_items + sab_items

        qbt_current = sum(item['current_speed'] or 0 for item in qbt_items)
        sab_current = sum(item['current_speed'] or 0 for item in sab_items)
        combined_current = qbt_current + sab_current

        # Update peak speed and determine current hard limits
        if not is_throttled and parse_size_to_bytes(FULL_SPEED) == 0:
            historical_peak_speed = max(historical_peak_speed, combined_current)
            total_limit = float('inf')
        else:
            total_limit = get_effective_total_speed()

        # NEW: If idle, preemptively apply limits to the clients so they are ready for new items, then exit.
        if not all_items:
            apply_rate_limits(total_limit, 0, 0, [])
            return

        # --- QUEUE MANAGEMENT ---
        limbo_items = [item for item in all_items if item.get('is_limbo')]
        managed_items = [item for item in all_items if not item.get('is_limbo')]

        # --- HYBRID QUEUE SORTING LOGIC ---
        tv_items = [item for item in managed_items if item.get('is_tv')]
        movie_items = [item for item in managed_items if not item.get('is_tv')]

        tv_items.sort(key=lambda item: item['priority'])
        movie_items.sort(key=lambda item: item.get('total_size', 0))

        total_tv_size = sum(item.get('total_size', 0) for item in tv_items)

        if total_tv_size == 0:
            managed_items = movie_items
        else:
            small_movies = [m for m in movie_items if m.get('total_size', 0) < total_tv_size]
            large_movies = [m for m in movie_items if m.get('total_size', 0) >= total_tv_size]
            managed_items = small_movies + tv_items + large_movies
        # ----------------------------------

        bandwidth_pool_baseline = historical_peak_speed if total_limit == float('inf') else total_limit
        HEALTHY_SPEED_THRESHOLD = 5 * 1024 * 1024  # 5 MB/s

        active_items = []
        active_bytes = 0
        has_active_sab = False

        # --- TELEMETRY HEADER ---
        if total_limit == float('inf'):
            peak_str = format_speed_limit_bytes(historical_peak_speed) if historical_peak_speed > 0 else "Pending..."
            limit_str = f"Unlimited (Peak: {peak_str})"
        else:
            limit_str = format_speed_limit_bytes(total_limit)

        if total_tv_size >= 1024**3:
            tv_size_str = f"{total_tv_size / (1024**3):.2f} GB"
        elif total_tv_size >= 1024**2:
            tv_size_str = f"{total_tv_size / (1024**2):.2f} MB"
        else:
            tv_size_str = f"{total_tv_size / 1024:.2f} KB" if total_tv_size > 0 else "0 B"

        print("\n" + "="*50, flush=True)
        print(f"[BALANCER RUN] Total Limit: {limit_str} | Throttled: {is_throttled}", flush=True)
        print(f"[HYBRID LOGIC] TV Queue Weight: {tv_size_str}", flush=True)
        print("-" * 50, flush=True)

        for item in limbo_items:
            print(f" [✓ RESCUED] (Expired Limbo)        | Speed:   N/A | {item['name']}", flush=True)

        for item in managed_items:
            speed_str = format_speed_limit_bytes(item['current_speed'] or 0)
            
            if not active_items:
                print(f" [✓ ACTIVE] (Top Priority) | Speed: {speed_str:>5} | {item['name']}", flush=True)
                active_items.append(item)
                active_bytes += item['current_speed'] or 0
                if item['source'] == 'sab':
                    has_active_sab = True
                continue

            if item['source'] == 'sab' and has_active_sab:
                print(f" [✓ QUEUED] (SAB Internal)   | Speed: {speed_str:>5} | {item['name']}", flush=True)
                active_items.append(item)
                active_bytes += item['current_speed'] or 0
                continue

            if total_limit != float('inf') and active_bytes >= total_limit:
                print(f" [⏸ PAUSED] (Hit Bandwidth Limit)        | {item['name']}", flush=True)
                continue

            if active_bytes >= HEALTHY_SPEED_THRESHOLD:
                active_speed_str = format_speed_limit_bytes(active_bytes)
                print(f" [⏸ PAUSED] (Queue Healthy at {active_speed_str:>4})    | {item['name']}", flush=True)
                continue

            print(f" [✓ ACTIVE] (Filling Idle Bandwidth)| Speed: {speed_str:>5} | {item['name']}", flush=True)
            active_items.append(item)
            active_bytes += item['current_speed'] or 0
            if item['source'] == 'sab':
                has_active_sab = True

        print("="*50 + "\n", flush=True)
        # --- END TELEMETRY ---

        active_qbt_hashes = {item['id'] for item in active_items if item['source'] == 'qbit'}
        active_sab_ids = {item['id'] for item in active_items if item['source'] == 'sab'}

        for item in limbo_items:
            if item['source'] == 'qbit':
                active_qbt_hashes.add(item['id'])
            elif item['source'] == 'sab':
                active_sab_ids.add(item['id'])

        apply_rate_limits(total_limit, qbt_current, sab_current, active_items)
        qbt_toggle_torrents(active_qbt_hashes, all_items)
        sab_toggle_torrents(active_sab_ids, all_items)


# --- CORE LOGIC ---
def set_throttles(enable_throttle: bool, reason: str):
    """Engages or releases throttles, preventing duplicate API calls."""
    global is_throttled

    if enable_throttle == is_throttled:
        return

    is_throttled = enable_throttle
    if is_throttled:
        print(f"\n[ACTION] Engaging throttles! (Trigger: {reason})", flush=True)
    else:
        print(f"\n[ACTION] Releasing throttles (Full speed!). (Trigger: {reason})", flush=True)

    # Defer entirely to the balancer engine so all API calls are centralized
    rebalance_downloads()


def sync_with_tracearr():
    """Background thread that acts as source of truth and queue balancer."""
    if not TRACEARR_TOKEN:
        print("[TRACEARR] No API token provided. Background sync will still rebalance downloads.", flush=True)

    print(f"[TRACEARR] Background sync started. Polling every {TRACEARR_SYNC_INTERVAL} seconds.", flush=True)

    while True:
        if TRACEARR_TOKEN:
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
            except Exception as exc:
                print(f"[TRACEARR SYNC] Failed to connect to Tracearr: {exc}", flush=True)

        rebalance_downloads()
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
    except Exception as exc:
        print(f"[PLEX ERROR] Failed to parse payload: {exc}", flush=True)

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
