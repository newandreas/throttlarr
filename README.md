# Throttlarr

Throttlarr is a Python service that manages download bandwidth for qBittorrent and SABnzbd. It watches **Plex**, **Jellyfin**, and **Emby** activity through webhooks and [Tracearr](https://github.com/connorgallopo/Tracearr), then adjusts the queue and speed limits so active streams keep buffer-free playback while background downloads stay under control.

> [!CAUTION]
> This app was coded with the help of LLMs, I am not a professional coder. Don't trust the app to be safe enough to expose to the internet.

## 🛠️ Features

* **Instant Response:** Uses media-server webhooks to start soft throttling the moment playback begins.
* **Tracearr Sync:** Polls Tracearr on a configurable interval so throttling stays in sync with active streams, even when a webhook is missed.
* **Prefetcharr Priority:** Scans recent Prefetcharr logs and promotes matching titles to the front of the queue when they look like the next thing a user is about to watch.
* **Manual Override Detection:** Tracks qBittorrent state changes so manual resume/pause actions are recognized and not fought by automation.
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
    volumes:
      # Mount the parent prefetcharr log directory as Read-Only to enable VIP queue priority
      - /opt/appdata/prefetcharr/logs:/prefetcharr_logs:ro
    depends_on:
      tracearr:
        condition: service_healthy

```

### Example [.env file](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/#use-the-env_file-attribute)

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

## 🔧 Configuration

### Queue priority and override behavior

Throttlarr does not just set a global limit; it also reorders the queue to favor what matters most:

* **Prefetcharr VIP priority:** Recent Prefetcharr log titles are matched against the current download names. If a show is actively being prefetched or watched soon, matching torrents are promoted higher in the queue.
* **Manual override detection:** qBittorrent state is tracked so when a user resumes a torrent that the app paused, or pauses one the app resumed, that action is treated as a manual override and left alone.
* **Season pack ordering:** Individual episodes still sort ahead of full season packs, which helps avoid massive season bundles stealing priority from the next episode a user wants to watch.
* **Tracearr filtering:** Paused streams are ignored when deriving the throttling state, so the app does not overreact to non-playing media.
* **SABnzbd handling:** Old or manually paused SAB downloads are ignored instead of being treated as active queue items.

### Webhooks (optional)

Because Throttlarr relies on Tracearr to detect when streams *stop*, you only need to send webhooks when a stream *starts* or *resumes*.

Point your media servers webhooks to the following endpoints:

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
