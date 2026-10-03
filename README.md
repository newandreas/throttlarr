# Throttlarr

Throttlarr is a Python service that manages download bandwidth for qBittorrent and SABnzbd. It watches **Plex**, **Jellyfin**, and **Emby** activity through webhooks and [Tracearr](https://github.com/connorgallopo/Tracearr), then adjusts the queue and speed limits so active streams keep buffer-free playback while background downloads stay under control.

> [!CAUTION]
> This app was coded with the help of LLMs, I am not a professional coder. Don't trust the app to be safe enough to expose to the internet.

---

## 🛠️ Features

* **Instant Response:** Uses media-server webhooks to start soft throttling the moment playback begins.
* **Tracearr Sync:** Polls Tracearr on a configurable interval so throttling stays in sync with active streams, even when a webhook is missed.
* **Prefetcharr Priority:** Scans recent Prefetcharr logs and prioritizes matching episodes from the trigger episode onward, plus later seasons.
* **Manual Override Detection:** Gives manually resumed downloads highest priority and excludes manually paused downloads from automation.
* **Hybrid Queue Logic:** Keeps a priority-aware ordering across TV, prefetch items, manual overrides, and regular downloads instead of treating the queue as a flat list.
* **SABnzbd Awareness:** Ignores stale SAB entries and manual pause states so the balancer does not interfere with user-driven downloads.
* **Scalable:** Supports 1, 2, or 100 media servers. If you have multiple Plex, Jellyfin, or Emby instances, Tracearr aggregates them all into one stream count.

## 📦 Deployment

### Docker Compose

```yaml
services:
  throttlarr:
    image: ghcr.io/newandreas/throttlarr:latest
    container_name: throttlarr
    restart: unless-stopped
    # Use internal docker networking (no ports exposed) if Plex/Jellyfin are in the same network
    # ports:
    #   - "5000:5000" 
    environment:
      # qBittorrent Config
      - QB_HOST=torrent:8080 # Service name or http://IP:PORT
      - QB_USER=${QB_USER}
      - QB_PASS=${QB_PASS}

      # SABnzbd Config
      - SAB_HOST=sabnzbd:1337
      - SAB_API_KEY=${SAB_API_KEY}

      # Tracearr Config
      - TRACEARR_URL=tracearr:3000
      - TRACEARR_TOKEN=${TRACEARR_API_KEY}
      - TRACEARR_SYNC_INTERVAL=300 # How often to poll Tracearr in seconds (default: 300)

      # Multi-Stage Speed Limits
      - FULL_SPEED=0              # Max bandwidth when idle (0 = unlimited)
      - SOFT_THROTTLE_SPEED=45M   # Speed limit for standard streams
      - HARD_THROTTLE_SPEED=30M   # Speed limit for heavy network loads
      
      # Escalation Triggers
      - HARD_THROTTLE_STREAMS=4   # Number of concurrent streams to trigger a Hard Throttle
      - HARD_THROTTLE_BITRATE=40000 # Bitrate in Kbps to trigger a Hard Throttle (e.g., 40000 = 40 Mbps)
      
      # Prefetcharr VIP Integration
      - PREFETCHARR_LOGS_DIR=/prefetcharr_logs
      - QUEUE_DIAGNOSTICS=0 # Set to 1 for up to 12 ranked rows with titles per rebalance
    volumes:
      # Mount the parent prefetcharr log directory as Read-Only to enable VIP queue priority
      - /opt/appdata/prefetcharr/logs:/prefetcharr_logs:ro
    depends_on:
      tracearr:
        condition: service_healthy

```

### Example `.env` file

```ini
# qBittorrent
QB_USER=admin
QB_PASS=your_password_here

# SABnzbd
SAB_API_KEY=your_32_char_api_key

# Tracearr
TRACEARR_API_KEY=trr_pub_your_token

```

Run the container:

```bash
docker compose up -d

```

---

## 🔧 Configuration

### Queue priority and override behavior

Completed and stale downloads are excluded from automatic ranking. Manually
paused downloads are excluded and never auto-resumed. Remaining downloads
follow these priority tiers, from highest to lowest:

See the [download queue priority policy](docs/queue-priority.md) for the
full rules and development guidance.

1. **Manual resume:** A download manually resumed by the user gets the highest priority.
2. **Prefetcharr targets:** Matching titles in the trigger season and later seasons outrank normal downloads. In the trigger season, episodes before the trigger episode do not qualify. Unknown trigger season or episode values broaden the match. Matching episodes sort ahead of season packs, and targeted downloads keep this priority while in progress.
3. **Early finish:** A waiting movie may move ahead of normal in-progress work when its full size is smaller than the top-ranked normal download's remaining bytes. A waiting TV episode uses the same comparison against the top-ranked normal in-progress TV episode. Neither promotion jumps ahead of manual resumes or Prefetcharr targets.
4. **Other in-progress downloads:** The smallest remaining amount comes first.
5. **Normal TV episodes:** Newer seasons come first, then earlier episodes within each season. Individual episodes sort before season packs.
6. **Other waiting movies:** These follow normal TV and sort smaller first.
7. **Ties:** Queue age, source, then ID make equal-priority ordering deterministic.

Priority describes precedence, not predicted completion time. Throttlarr
coordinates an aggregate speed limit across qBittorrent and SABnzbd, lets
available capacity cascade to lower-ranked work, and synchronizes the ranked
order to qBittorrent. SABnzbd still schedules within its own queue, and actual
per-download speeds vary.

### Troubleshooting logs

Each rebalance logs a UTC summary with the throttle stage, observed speeds,
queue counts, top item ID, and requested SABnzbd/qBittorrent limits. For a
temporary ranked-item trace, set `QUEUE_DIAGNOSTICS=1`, restart Throttlarr,
reproduce the behavior, then collect recent logs with:

```bash
docker compose logs --since 15m throttlarr
```

Diagnostic rows include download titles and are limited to 12 per rebalance.
Review logs before sharing them; known credentials and common authentication
query parameters are redacted, but logs can still contain private metadata.

### Webhooks (optional)

Because Throttlarr relies on Tracearr's background polling to detect when streams *stop* or change bitrates, you only need to send webhooks when a stream *starts* or *resumes* to guarantee instant soft throttling.

Point your media servers' webhooks to the following endpoints:

* **Plex:** `http://throttlarr:5000/plex`
* **Jellyfin:** `http://throttlarr:5000/jellyfin`
* **Emby:** `http://throttlarr:5000/emby`

---

### 🦑 Jellyfin

1. Go to **Dashboard** -> **Plugins**.
2. Download and install the **Webhook** plugin, then restart Jellyfin.
3. Go back to Plugins, click Webhook, and press **Settings**.
4. Click **Add Generic Destination**.
5. **Webhook Url:** `http://throttlarr:5000/jellyfin`
6. **Notification Type:** Check only **Playback Start** and **Playback Unpause**.
7. Copy and paste this into the **Template** box:

```json
{
  "NotificationType": "{{NotificationType}}"
}

```

8. Save!

---

### 🎬 Emby

> [!NOTE]
> Native Webhooks in Emby typically require Emby Premiere.

1. Go to **Settings** -> **Server** -> **Webhooks**.
2. Click **Add Webhook**.
3. **URL:** `http://throttlarr:5000/emby`
4. **Data Format:** `application/json`
5. **Events:** Check **Playback Start** and **Playback Unpause**.
6. Save!

---

### 🍿 Plex

1. Go to **Settings**.
2. Under your user account (top left), select **Webhooks**.
3. Click **Add Webhook**.
4. **URL:** `http://throttlarr:5000/plex`
5. Save!

---

### ⬇️ SABnzbd

> [!IMPORTANT]
> Because this app communicates via Docker's internal DNS, you must allow the hostname in SABnzbd.
> 1. Go to SABnzbd **Settings** -> **General**.
> 2. Switch to **Advanced View** (top right corner).
> 3. Add `sabnzbd` to the **Host Whitelist** field and save. It should look like `sabnzbd.example.com, sabnzbd`.
> 
> 

