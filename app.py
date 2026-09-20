from flask import Flask, jsonify, request
from datetime import datetime, timezone, timedelta
import json
import hmac
import os
import re
import re
import requests
import threading
import time

app = Flask(__name__)

# Service endpoints are normalized here so container hostnames and variant URL inputs
# are treated consistently before we talk to qBittorrent, SABnzbd, or Tracearr.
def fix_url(url):
    """Ensures URLs have an http(s) scheme and no trailing slash."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url.rstrip('/')

# Runtime configuration pulled from env. The defaults are tuned for the Docker compose
# layout used by this project so the service can boot with minimal setup.
QBT_HOST = fix_url(os.getenv('QB_HOST', 'torrent:8080'))
QBT_USER = os.getenv('QB_USER', 'user')
QBT_PASS = os.getenv('QB_PASS', 'password')

TRACEARR_URL = fix_url(os.getenv('TRACEARR_URL', 'tracearr:3000'))
TRACEARR_TOKEN = os.getenv('TRACEARR_TOKEN', '')

SAB_HOST = fix_url(os.getenv('SAB_HOST', 'sabnzbd:8080'))
SAB_API_KEY = os.getenv('SAB_API_KEY', '')
THROTTLARR_API_TOKEN = os.getenv('THROTTLARR_API_TOKEN', '')

# Speed settings are staged so the app can react to media playback without forcing a
# full stop in the queue. 0 means "unlimited" for the relevant mode.
FULL_SPEED = os.getenv('FULL_SPEED', '0')
SOFT_THROTTLE_SPEED = os.getenv('SOFT_THROTTLE_SPEED', '30M')
HARD_THROTTLE_SPEED = os.getenv('HARD_THROTTLE_SPEED', '5M')

# Prefetcharr can be used as a soft signal that a show is likely to be watched soon,
# giving us a way to prioritize or deprioritize matching torrents.
PREFETCHARR_LOGS_DIR = os.getenv('PREFETCHARR_LOGS_DIR', '')

# Hard throttle escalation triggers are intentionally conservative: a single stream is not
# enough to cause a hard cap, but sustained multi-stream or high-bitrate playback is.
try:
    HARD_THROTTLE_STREAMS = int(os.getenv('HARD_THROTTLE_STREAMS', '2'))
    HARD_THROTTLE_BITRATE = int(os.getenv('HARD_THROTTLE_BITRATE', '20000'))
except ValueError:
    HARD_THROTTLE_STREAMS = 2
    HARD_THROTTLE_BITRATE = 20000

# Keep a sane fallback if env parsing breaks; this loop is better off being a little slow
# than failing completely because of a bad config value.
try:
    TRACEARR_SYNC_INTERVAL = int(os.getenv('TRACEARR_SYNC_INTERVAL', '300'))
except ValueError:
    print("[WARNING] Invalid TRACEARR_SYNC_INTERVAL provided. Defaulting to 300 seconds.", flush=True)
    TRACEARR_SYNC_INTERVAL = 300

# Old items are intentionally treated as low-priority once they age out of the active window.
MAX_RECENT_SECONDS = 60 * 60 * 3  # 3 hours

# Shared state that the rebalance loop uses to decide what is active, what is paused, and
# which limits have actually been applied to each client.
throttle_level = 0  # 0 = normal, 1 = soft throttle, 2 = hard throttle
historical_peak_speed = 0
queue_lock = threading.Lock()
last_applied_sab_limit = None
last_applied_qbt_limit = None
last_applied_qbt_mode = None

active_prefetch_shows = set()
last_prefetch_check = 0
qbt_intended_states = {}
qbt_manual_overrides = set()
qbt_manual_pauses = set()

# Helper functions live here. Most of the service logic is intentionally small and data-driven
# so the balancing loop can stay deterministic and easier to reason about.
def update_prefetch_shows():
    """Scans recent Prefetcharr logs and caches active TV titles for the current window."""
    global active_prefetch_shows, last_prefetch_check

    # We only re-read the log directory once a minute to avoid a hot loop on large log files.
    if time.time() - last_prefetch_check < 60:
        return

    if not PREFETCHARR_LOGS_DIR or not os.path.exists(PREFETCHARR_LOGS_DIR):
        return

    recent_shows = set()

    # Match the timestamp and title from Prefetcharr log entries so we can find active shows
    # without needing a dedicated API or database.
    log_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z).*?title="([^"]+)"', re.IGNORECASE)

    # Compare against UTC time explicitly so the cutoff matches the log timestamps.
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=6)

    for root, _, files in os.walk(PREFETCHARR_LOGS_DIR):
        for file in files:
            if 'log' in file.lower():
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            match = log_pattern.search(line)
                            if match:
                                time_str = match.group(1)
                                title = match.group(2).lower().replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
                                
                                # Convert the log's UTC string to a datetime object
                                try:
                                    log_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                                    
                                    if log_time >= cutoff_time:
                                        recent_shows.add(title) # Just save the title!
                                except ValueError:
                                    pass
                except Exception:
                    pass
    
    active_prefetch_shows = recent_shows
    last_prefetch_check = time.time()


def sab_sync_queue_order(managed_items):
    if not SAB_API_KEY:
        return

    sab_items = [item for item in managed_items if item['source'] == 'sab']
    if len(sab_items) <= 1:
        return

    is_sorted = True
    for i in range(len(sab_items) - 1):
        p1 = sab_items[i].get('sab_pos', 99999)
        p2 = sab_items[i+1].get('sab_pos', 99999)
        if p1 > p2:
            is_sorted = False
            break

    if is_sorted:
        return

    try:
        for item in reversed(sab_items):
            requests.get(
                f"{SAB_HOST}/api",
                params={'mode': 'switch', 'value': item['id'], 'value2': 0, 'apikey': SAB_API_KEY},
                timeout=5
            )
    except Exception as exc:
        print(f"[SAB] Failed to sync queue order: {exc}", flush=True)


def qbt_sync_queue_order(managed_items):
    qbt_items = [item for item in managed_items if item['source'] == 'qbit']
    if len(qbt_items) <= 1:
        return

    is_sorted = True
    for i in range(len(qbt_items) - 1):
        pos1 = qbt_items[i].get('qbt_pos', -1)
        pos2 = qbt_items[i+1].get('qbt_pos', -1)
        
        p1 = 99999 if pos1 < 0 else pos1
        p2 = 99999 if pos2 < 0 else pos2
        
        if p1 >= p2:
            is_sorted = False
            break

    if is_sorted:
        return

    try:
        session = qbt_login_session()
        for item in reversed(qbt_items):
            session.post(
                f"{QBT_HOST}/api/v2/torrents/topPrio",
                data={'hashes': item['id']},
                timeout=5
            )
    except Exception as exc:
        print(f"[QBT] Failed to sync queue order: {exc}", flush=True)


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
    if unit == 'K': return int(number * 1024)
    if unit == 'M': return int(number * 1024 * 1024)
    if unit == 'G': return int(number * 1024 * 1024 * 1024)
    return int(number)


def format_speed_limit_bytes(bytes_per_sec):
    if bytes_per_sec == float('inf') or bytes_per_sec <= 0:
        return '0'
    megabytes = bytes_per_sec / (1024 * 1024)
    if megabytes >= 1:
        return f"{megabytes:.2f}".rstrip('0').rstrip('.') + "M"
    kilobytes = bytes_per_sec / 1024
    return f"{kilobytes:.2f}".rstrip('0').rstrip('.') + "K"

def parse_priority(name):
    text = str(name).replace('_', ' ').replace('.', ' ').replace('-', ' ')

    # 1. Standard S01E01 or S1E1 patterns (Returns: Season, Episode, 1)
    match = re.search(r'[sS](\d{1,2})\s*[eE](\d{1,2})', text)
    if match: 
        return int(match.group(1)), int(match.group(2)), 1

    # 2. 1x01 or 01x01 patterns
    match = re.search(r'(\d{1,2})[xX](\d{1,2})', text)
    if match: 
        return int(match.group(1)), int(match.group(2)), 1

    # 3. Full "SEASON 1" text patterns
    # FIX: Assign Season Packs episode 999 so they sort AFTER individual episodes!
    match = re.search(r'SEASON\s*(\d{1,2})', text, re.IGNORECASE)
    if match: 
        return int(match.group(1)), 999, 1

    # 4. Standalone season packs (e.g., Silo.S01.2160p or Silo S1)
    match = re.search(r'\b[sS](\d{1,2})\b', text)
    if match: 
        return int(match.group(1)), 999, 1

    # 5. "1 of 12" style patterns
    match = re.search(r'^(\d{1,2})\s*of\s*\d{1,2}', text, re.IGNORECASE)
    if match: 
        return int(match.group(1)), 999, 1

    # Fallback to movie classification (Season 999, Episode 999)
    return 999, 999, 2


def parse_sab_added(slot):
    for key in ('added', 'age', 'age_seconds'):
        value = slot.get(key)
        if value is None: continue
        if isinstance(value, (int, float)):
            if value > 1e9: return int(value)
            return int(time.time() - int(value))
        if isinstance(value, str):
            if value.isdigit(): return int(value)
            parts = value.split(':')
            if len(parts) == 3:
                try:
                    h, m, s = [int(p) for p in parts]
                    return int(time.time() - (h * 3600 + m * 60 + s))
                except ValueError: pass
    return None


def parse_duration_seconds(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {'-', 'n/a', 'infinite', '∞'}:
        return None
    parts = text.split(':')
    try:
        if len(parts) == 3:
            hours, minutes, seconds = [int(float(part)) for part in parts]
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = [int(float(part)) for part in parts]
            return minutes * 60 + seconds
        return int(float(text))
    except (TypeError, ValueError):
        return None


def qbt_login_session():
    session = requests.Session()
    try:
        response = session.post(
            f"{QBT_HOST}/api/v2/auth/login",
            data={'username': QBT_USER, 'password': QBT_PASS},
            timeout=10,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f'qBittorrent login failed: {response.status_code} {response.text}')
        if response.status_code == 200 and 'Ok.' not in response.text:
            raise RuntimeError(f'qBittorrent login failed: {response.status_code} {response.text}')
    except Exception as exc:
        raise RuntimeError(f'Failed to authenticate with qBittorrent: {exc}') from exc
    return session


def qbt_get_downloads():
    global qbt_intended_states, qbt_manual_overrides, qbt_manual_pauses
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
        hash_id = torrent.get('hash', '')
        
        # Track manual qBittorrent changes so we only keep overriding the torrent when the
        # user is still behaving like a normal automation user rather than manually changing
        # the torrent state outside of the balancer.
        if hash_id in qbt_intended_states:
            intended_paused = qbt_intended_states[hash_id]
            if intended_paused and not paused:
                # The app paused this torrent but it is now running again. Treat that as a
                # manual override and stop fighting the user's action.
                qbt_manual_overrides.add(hash_id)
                qbt_manual_pauses.discard(hash_id)
            elif not intended_paused and paused:
                # The user paused this torrent after the app left it running. Keep it in the
                # normal queue, but do not resume it automatically.
                qbt_manual_pauses.add(hash_id)
                qbt_manual_overrides.discard(hash_id)
            elif not paused and hash_id in qbt_manual_pauses:
                # The user resumed a manually paused torrent. It returns to the normal queue.
                qbt_manual_pauses.discard(hash_id)
        elif paused:
            # A paused torrent with no state recorded by this process was paused manually (or
            # before a restart). Do not resume it until the user changes its state.
            qbt_manual_pauses.add(hash_id)
        elif hash_id in qbt_manual_pauses:
            # A manually paused torrent was resumed by the user. It returns to the normal queue.
            qbt_manual_pauses.discard(hash_id)

        total_size = int(torrent.get('size', 0) or torrent.get('total_size', 0) or 0)
        season, episode, kind = parse_priority(name)
        is_tv = kind in (0, 1)
        
        try:
            qbt_pos = int(torrent.get('priority', -1))
        except (ValueError, TypeError):
            qbt_pos = -1

        item = {
            'source': 'qbit',
            'id': hash_id,
            'name': name,
            'added_on': added_on,
            'state': state,
            'current_speed': 0 if paused else current_speed,
            'completed_bytes': int(torrent.get('completed', 0) or 0),
            'eta_seconds': int(torrent.get('eta', 0) or 0) if torrent.get('eta') is not None else None,
            'is_paused': paused,
            'total_size': total_size,
            'is_tv': is_tv,
            'qbt_pos': qbt_pos,
            'priority': (season, episode, kind, added_on),
            'is_manual_override': hash_id in qbt_manual_overrides,
            'is_manual_pause': hash_id in qbt_manual_pauses
        }
        item['remaining_bytes'] = max(0, item['total_size'] - item['completed_bytes']) if item['total_size'] > 0 else None

        if now - added_on > MAX_RECENT_SECONDS:
            if paused and not item['is_manual_override']:
                item['is_limbo'] = True
                item['priority'] = (999, 999, 999, added_on)
                downloads.append(item)
            continue
        
        downloads.append(item)
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

    for index, slot in enumerate(slots):
        status = str(slot.get('status', '')).lower()
        if status not in {'downloading', 'paused', 'queued'}:
            continue

        added_on = parse_sab_added(slot)
        if added_on is None:
            added_on = now
            
        name = slot.get('filename') or slot.get('nzo_name') or slot.get('name') or ''
        paused = status == 'paused'
        
        try:
            total_size = int(float(slot.get('mb') or 0) * 1024 * 1024)
        except ValueError:
            total_size = 0

        try:
            remaining_bytes = int(float(slot.get('mbleft') or 0) * 1024 * 1024)
        except (TypeError, ValueError):
            remaining_bytes = 0
            
        season, episode, kind = parse_priority(name)
        is_tv = kind in (0, 1)

        if status == 'downloading' and not applied_global_speed:
            current_speed = global_speed_bytes
            applied_global_speed = True
        else:
            current_speed = 0

        item = {
            'source': 'sab',
            'id': slot.get('nzo_id') or slot.get('id') or name,
            'name': name,
            'added_on': added_on,
            'state': status,
            'current_speed': current_speed,
            'completed_bytes': max(0, total_size - remaining_bytes),
            'eta_seconds': parse_duration_seconds(slot.get('timeleft') or slot.get('eta')),
            'is_paused': False,
            'total_size': total_size,
            'is_tv': is_tv,
            'sab_pos': index,
            'priority': (season, episode, kind, added_on),
            'is_manual_override': str(slot.get('priority', '0')) == '2'
        }
        item['remaining_bytes'] = max(0, total_size - item['completed_bytes']) if total_size > 0 else None

        if now - added_on > MAX_RECENT_SECONDS and paused:
            item['is_limbo'] = True
            item['priority'] = (999, 999, 999, added_on)

        downloads.append(item)
    return downloads


def serialize_download_item(item):
    return {
        'source': item.get('source'),
        'id': item.get('id'),
        'name': item.get('name'),
        'state': item.get('state'),
        'added_at': item.get('added_on'),
        'queue_position': item.get('qbt_pos', item.get('sab_pos')),
        'size_bytes': item.get('total_size', 0),
        'completed_bytes': item.get('completed_bytes', 0),
        'remaining_bytes': item.get('remaining_bytes'),
        'speed_bytes': item.get('current_speed', 0),
        'eta_seconds': item.get('eta_seconds'),
        'paused': item.get('is_paused', False),
        'is_tv': item.get('is_tv', False),
        'is_prefetch': item.get('is_prefetch', False),
    }


def get_effective_total_speed():
    soft_bytes = parse_size_to_bytes(SOFT_THROTTLE_SPEED)
    hard_bytes = parse_size_to_bytes(HARD_THROTTLE_SPEED)
    full_bytes = parse_size_to_bytes(FULL_SPEED)

    if throttle_level == 2:
        return float('inf') if hard_bytes == 0 else hard_bytes
    elif throttle_level == 1:
        return float('inf') if soft_bytes == 0 else soft_bytes

    if full_bytes > 0:
        return full_bytes

    return historical_peak_speed or float('inf')


def completion_sort_key(item):
    total_size = item.get('total_size', 0)
    completed_bytes = item.get('completed_bytes', 0)
    completion_ratio = completed_bytes / total_size if total_size > 0 else 0
    return (-completion_ratio, item.get('remaining_bytes', float('inf')), item['priority'])


def apply_rate_limits(total_speed_limit, current_qbt_speed, current_sab_speed, active_items):
    global historical_peak_speed, throttle_level
    global last_applied_sab_limit, last_applied_qbt_limit, last_applied_qbt_mode

    sab_target = total_speed_limit
    qbt_target = total_speed_limit

    top_source = active_items[0]['source'] if active_items else None
    pool = historical_peak_speed if total_speed_limit == float('inf') else total_speed_limit

    if pool > 0:
        if top_source == 'sab':
            sab_target = total_speed_limit
            qbt_target = max(pool - current_sab_speed, 1024)
        elif top_source == 'qbit':
            qbt_target = total_speed_limit
            sab_target = max(pool - current_qbt_speed, 1024)

    # --- SABNZBD ---
    if SAB_API_KEY:
        target_speed_string = '0' if sab_target == float('inf') else format_speed_limit_bytes(sab_target)
        if target_speed_string != last_applied_sab_limit:
            try:
                sab_url = f"{SAB_HOST}/api?mode=config&name=speedlimit&value={target_speed_string}&apikey={SAB_API_KEY}&output=json"
                response = requests.get(sab_url, timeout=5)
                if response.status_code == 200:
                    speed_str = "Unlimited" if target_speed_string == "0" else target_speed_string
                    print(f"[SUCCESS] SABnzbd limit set to: {speed_str}", flush=True)
                    last_applied_sab_limit = target_speed_string
            except Exception as exc:
                print(f"[ERROR] SABnzbd limit set failed: {exc}", flush=True)

    # --- QBITTORRENT ---
    qbt_limit_bytes = -1 if qbt_target == float('inf') else int(qbt_target)
    mode_val = 1 if throttle_level > 0 else 0
    
    if qbt_limit_bytes != last_applied_qbt_limit or mode_val != last_applied_qbt_mode:
        try:
            session = qbt_login_session()
            prefs_payload = {"dl_limit": qbt_limit_bytes, "alt_dl_limit": qbt_limit_bytes}
            session.post(f"{QBT_HOST}/api/v2/app/setPreferences", data={'json': json.dumps(prefs_payload)}, timeout=5)
            session.post(f"{QBT_HOST}/api/v2/transfer/setSpeedLimitsMode", data={'mode': mode_val}, timeout=5)
            
            speed_str = "Unlimited" if qbt_limit_bytes == -1 else format_speed_limit_bytes(qbt_limit_bytes)
            mode_str = f"Alt Mode (Stage {throttle_level})" if throttle_level > 0 else "Regular Mode"
            print(f"[SUCCESS] qBittorrent limit set to: {speed_str} ({mode_str})", flush=True)
            
            last_applied_qbt_limit = qbt_limit_bytes
            last_applied_qbt_mode = mode_val
        except Exception as exc:
            print(f"[ERROR] qBittorrent limit set failed: {exc}", flush=True)


def qbt_toggle_torrents(active_hashes, all_items):
    try:
        session = qbt_login_session()
    except Exception:
        return

    try:
        pause_hash_list = [item['id'] for item in all_items if item['source'] == 'qbit' and item['id'] not in active_hashes and not item['is_paused']]
        resume_hash_list = [item['id'] for item in all_items if item['source'] == 'qbit' and item['id'] in active_hashes and item['is_paused'] and not item.get('is_manual_override') and not item.get('is_manual_pause')]

        if pause_hash_list:
            resp = session.post(f"{QBT_HOST}/api/v2/torrents/stop", data={'hashes': '|'.join(pause_hash_list)}, timeout=10)
            if resp.status_code == 404:
                session.post(f"{QBT_HOST}/api/v2/torrents/pause", data={'hashes': '|'.join(pause_hash_list)}, timeout=10)

        if resume_hash_list:
            resp = session.post(f"{QBT_HOST}/api/v2/torrents/start", data={'hashes': '|'.join(resume_hash_list)}, timeout=10)
            if resp.status_code == 404:
                session.post(f"{QBT_HOST}/api/v2/torrents/resume", data={'hashes': '|'.join(resume_hash_list)}, timeout=10)
    except Exception as exc:
        print(f"[QBT] Failed to pause/resume torrents: {exc}", flush=True)

    global qbt_intended_states
    for item in all_items:
        if item['source'] == 'qbit':
            qbt_intended_states[item['id']] = item['id'] not in active_hashes

def rebalance_downloads():
    global historical_peak_speed

    # Refresh the Prefetcharr signal before we decide which titles are actually hot.
    update_prefetch_shows()

    with queue_lock:
        qbt_items = qbt_get_downloads()
        sab_items = sab_get_downloads()
        all_items = qbt_items + sab_items

        qbt_current = sum(item['current_speed'] or 0 for item in qbt_items)
        sab_current = sum(item['current_speed'] or 0 for item in sab_items)
        combined_current = qbt_current + sab_current

        if throttle_level == 0 and parse_size_to_bytes(FULL_SPEED) == 0:
            historical_peak_speed = max(historical_peak_speed, combined_current)
            total_limit = float('inf')
        else:
            total_limit = get_effective_total_speed()

        if not all_items:
            apply_rate_limits(total_limit, 0, 0, [])
            return

        limbo_items = [item for item in all_items if item.get('is_limbo')]
        managed_items = [item for item in all_items if not item.get('is_limbo')]

        # Prefetcharr is treated as a soft priority signal, not a hard requirement. If the
        # matching title appears in an active recent log, we boost it over generic TV entries.
        for item in managed_items:
            item['is_prefetch'] = False
            if item.get('is_tv'):
                clean_name = item['name'].lower().replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()

                for pf_title in active_prefetch_shows:
                    if pf_title in clean_name:
                        item['is_prefetch'] = True
                        break

        # Keep manual overrides at the top, then Prefetcharr-priority titles, then normal TV
        # traffic, and finally movies. This preserves the queue semantics while still letting
        # the active recordings dominate the bandwidth pool.
        manual_items = [item for item in managed_items if item.get('is_manual_override')]
        in_progress_items = [item for item in managed_items if item.get('completed_bytes', 0) > 0 and not item.get('is_manual_override')]
        new_items = [item for item in managed_items if item.get('completed_bytes', 0) <= 0 and not item.get('is_manual_override')]
        in_progress_items.sort(key=completion_sort_key)

        prefetch_items = [item for item in new_items if item.get('is_prefetch')]
        tv_items = [item for item in new_items if item.get('is_tv') and not item.get('is_prefetch')]
        movie_items = [item for item in new_items if not item.get('is_tv')]

        manual_items.sort(key=lambda item: item['priority'])
        prefetch_items.sort(key=lambda item: item['priority'])
        tv_items.sort(key=lambda item: item['priority'])
        movie_items.sort(key=lambda item: item.get('total_size', 0))

        total_tv_size = sum(item.get('total_size', 0) for item in tv_items) + sum(item.get('total_size', 0) for item in prefetch_items) + sum(item.get('total_size', 0) for item in manual_items) + sum(item.get('total_size', 0) for item in in_progress_items if item.get('is_tv'))

        if total_tv_size == 0 and not manual_items:
            managed_items = in_progress_items + movie_items
        else:
            small_movies = [m for m in movie_items if m.get('total_size', 0) < total_tv_size]
            large_movies = [m for m in movie_items if m.get('total_size', 0) >= total_tv_size]
            managed_items = manual_items + in_progress_items + prefetch_items + small_movies + tv_items + large_movies

        qbt_sync_queue_order(managed_items)
        sab_sync_queue_order(managed_items)

        HEALTHY_SPEED_THRESHOLD = 5 * 1024 * 1024  # 5 MB/s
        active_items = []
        active_bytes = 0
        has_active_sab = False

        if total_limit == float('inf'):
            peak_str = format_speed_limit_bytes(historical_peak_speed) if historical_peak_speed > 0 else "Pending..."
            limit_str = f"Unlimited (Peak: {peak_str})"
        else:
            limit_str = format_speed_limit_bytes(total_limit)

        tv_size_str = f"{total_tv_size / (1024**3):.2f} GB" if total_tv_size >= 1024**3 else f"{total_tv_size / (1024**2):.2f} MB" if total_tv_size >= 1024**2 else f"{total_tv_size / 1024:.2f} KB" if total_tv_size > 0 else "0 B"

        print("\n" + "="*50, flush=True)
        print(f"[BALANCER RUN] Total Limit: {limit_str} | Throttle Stage: {throttle_level}", flush=True)
        print(f"[HYBRID LOGIC] TV Queue Weight: {tv_size_str}", flush=True)
        print("-" * 50, flush=True)

        for item in limbo_items:
            print(f" [✓ RESCUED] (Expired Limbo)        | Speed:   N/A | {item['name']}", flush=True)

        for item in managed_items:
            speed_str = format_speed_limit_bytes(item['current_speed'] or 0)
            
            # Dynamic Tagging
            if item.get('is_manual_override'):
                tag = " ✋ "
            elif item.get('is_prefetch'):
                tag = " 🚀 "
            else:
                tag = "    "
            
            if not active_items:
                print(f" [✓ ACTIVE] (Top Priority){tag}| Speed: {speed_str:>5} | {item['name']}", flush=True)
                active_items.append(item)
                active_bytes += item['current_speed'] or 0
                if item['source'] == 'sab':
                    has_active_sab = True
            elif item['source'] == 'sab' and has_active_sab:
                print(f" [✓ QUEUED] (SAB Internal){tag}| Speed: {speed_str:>5} | {item['name']}", flush=True)
                active_items.append(item)
                active_bytes += item['current_speed'] or 0
            elif total_limit != float('inf') and active_bytes >= total_limit:
                print(f" [⏸ PAUSED] (Bandwidth Cap){tag}| {item['name']}", flush=True)
            elif active_bytes >= HEALTHY_SPEED_THRESHOLD:
                print(f" [⏸ PAUSED] (Queue Healthy){tag}| {item['name']}", flush=True)
            else:
                print(f" [✓ ACTIVE] (Filling Idle) {tag}| Speed: {speed_str:>5} | {item['name']}", flush=True)
                active_items.append(item)
                active_bytes += item['current_speed'] or 0
                if item['source'] == 'sab':
                    has_active_sab = True

        print("="*50 + "\n", flush=True)

        active_qbt_hashes = {item['id'] for item in active_items if item['source'] == 'qbit'}
        for item in limbo_items:
            if item['source'] == 'qbit':
                active_qbt_hashes.add(item['id'])

        apply_rate_limits(total_limit, qbt_current, sab_current, active_items)
        qbt_toggle_torrents(active_qbt_hashes, all_items)


# Publicly exposed throttle state for the rest of the app. The logic intentionally refuses to
# escalate from hard back to soft or from soft back to full unless the trigger changes.
def set_throttles(level: int, reason: str):
    global throttle_level
    if level == 1 and throttle_level == 2: return
    if level == throttle_level: return

    throttle_level = level
    if throttle_level == 2:
        print(f"\n[ACTION] Engaging HARD Throttles! (Trigger: {reason})", flush=True)
    elif throttle_level == 1:
        print(f"\n[ACTION] Engaging SOFT Throttles. (Trigger: {reason})", flush=True)
    else:
        print(f"\n[ACTION] Releasing all throttles (Full speed!). (Trigger: {reason})", flush=True)

    rebalance_downloads()


def sync_with_tracearr():
    if not TRACEARR_TOKEN:
        print("[TRACEARR] No API token provided. Background sync will still rebalance downloads.", flush=True)

        print("[TRACEARR] No API token provided. Background sync will still rebalance downloads.", flush=True)

    print(f"[TRACEARR] Background sync started. Polling every {TRACEARR_SYNC_INTERVAL} seconds.", flush=True)


    while True:
        if TRACEARR_TOKEN:
            try:
                headers = {'accept': 'application/json', 'Authorization': f'Bearer {TRACEARR_TOKEN}'}
                url = f"{TRACEARR_URL}/api/v1/public/streams"
                response = requests.get(url, headers=headers, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    raw_streams = data.get('data', [])
                    streams = [s for s in raw_streams if s.get('state') == 'playing']
                    
                    total_streams = len(streams)
                    max_bitrate_kbps = 0
                    
                    for stream in streams:
                        bitrate = stream.get('bitrate', 0)
                        if bitrate > max_bitrate_kbps: max_bitrate_kbps = bitrate

                    if total_streams == 0: set_throttles(0, reason="Tracearr reports 0 streams")
                    elif total_streams >= HARD_THROTTLE_STREAMS: set_throttles(2, reason=f"Stream count reached {total_streams}")
                    elif max_bitrate_kbps >= HARD_THROTTLE_BITRATE: set_throttles(2, reason=f"High bitrate detected ({max_bitrate_kbps} Kbps)")
                    else: set_throttles(1, reason=f"Active stream ({max_bitrate_kbps} Kbps)")
                        
                else: print(f"[TRACEARR SYNC] Error HTTP {response.status_code}: {response.text}", flush=True)
            except Exception as exc:
                print(f"[TRACEARR SYNC] Failed to connect to Tracearr: {exc}", flush=True)

        rebalance_downloads()
        time.sleep(TRACEARR_SYNC_INTERVAL)


@app.route('/api/downloads', methods=['GET'])
def downloads_api():
    """Return read-only normalized queue state for internal consumers."""
    if not THROTTLARR_API_TOKEN:
        return jsonify({'error': 'endpoint token is not configured'}), 503

    supplied_token = request.headers.get('X-Throttlarr-Token', '')
    if not hmac.compare_digest(supplied_token, THROTTLARR_API_TOKEN):
        return jsonify({'error': 'unauthorized'}), 401

    with queue_lock:
        downloads = qbt_get_downloads() + sab_get_downloads()

    return jsonify({
        'updated_at': int(time.time()),
        'downloads': [serialize_download_item(item) for item in downloads]
    })


# Media server webhooks are the fast path for "the user is actively watching" signals.
# They are intentionally lightweight and only push the app into the soft-throttle phase.
@app.route('/plex', methods=['POST'])
def plex_webhook():
    payload = request.form.get('payload')
    if not payload: return "No payload", 400
    try:
        event = json.loads(payload).get('event')
        if event in ['media.play', 'media.resume']: set_throttles(1, reason=f"Plex Webhook ({event})")
    except Exception: pass
    return "OK", 200


@app.route('/jellyfin', methods=['POST'])
def jellyfin_webhook():
    data = request.get_json(force=True, silent=True)
    if not data: return "No payload", 400
    event = data.get('NotificationType')
    if event in ['PlaybackStart', 'PlaybackUnpause']: set_throttles(1, reason=f"Jellyfin Webhook ({event})")
    return "OK", 200


@app.route('/emby', methods=['POST'])
def emby_webhook():
    data = request.get_json(force=True, silent=True)
    if not data: return "No payload", 400
    event = data.get('Event')
    if event in ['playback.start', 'playback.unpause']: set_throttles(1, reason=f"Emby Webhook ({event})")
    return "OK", 200


# Start the background poller once, when the Flask app boots. The process is daemonized so
# the service can keep running without holding the app open.
def start_background_threads():
    print("[SYSTEM] Initializing background sync thread...", flush=True)
    thread = threading.Thread(target=sync_with_tracearr, daemon=True)
    thread.start()

is_flask_reloader = os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('FLASK_ENV') == 'development'

if is_flask_reloader:
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_background_threads()
else:
    start_background_threads()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
