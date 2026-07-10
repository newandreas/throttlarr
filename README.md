# Throttlarr

Throttlarr is an intelligent Python service that manages your download speeds and queue priorities. It monitors your **Plex**, **Jellyfin**, and **Emby** streams via webhooks and [Tracearr](https://github.com/connorgallopo/Tracearr), instantly throttling qBittorrent and SABnzbd when someone hits play to ensure a buffer-free viewing experience.

Beyond basic throttling, Throttlarr features a **Balancing Engine** that acts as a traffic cop for your downloads, dynamically allocating bandwidth between download clients and reorganizing your queues so you get your media faster.

> [!CAUTION]
> This app was coded with the help of LLMs, I am not a professional coder. Don't trust the app to be safe enough to expose to the internet.

---

## 🛠️ Features

* **Multi-Stage Throttling:** Automatically switches between `FULL_SPEED`, `SOFT_THROTTLE_SPEED`, and `HARD_THROTTLE_SPEED`. It steps down to a soft throttle for normal viewing and escalates to a hard throttle if stream counts or bitrates exceed your desired limit.
* **Instant Response:** Uses webhooks from your media server to instantly engage throttles the second a stream starts or resumes.
* **Smart Tracearr Polling:** Connects to Tracearr to monitor actively "playing" streams in the background, validating active stream counts and analyzing bitrates to release or escalate throttles automatically.
* **Smart Bandwidth Sharing:** Dynamically shares a single speed limit across SABnzbd and qBittorrent. The top-priority client gets the full pipeline, while the secondary client only gets the leftover idle bandwidth (choked down to 1 KB/s if necessary).
* **Hybrid Queue Prioritization:** Automatically sorts your downloads to get you quick fullfillment. TV Shows are downloaded linearly by Season/Episode. Movies are sorted by file size. Small movies jump to the front of the line; massive movies are pushed to the back until your TV queue finishes.
* **qBittorrent Synergy:** Respects your Upload limits. Throttlarr dynamically adjusts the internal download limits while using the "Alternative Speed Limits" toggle strictly as a visual indicator and upload throttle. *Now supports automatic endpoint switching for both qBittorrent v4.x and v5.0+.*
* **Scalable:** Supports 1, 2, or 100 media servers. Tracearr aggregates all your instances into one stream count.

---

## 📦 Deployment

### Docker Compose

```yaml
services:
  throttlarr:
    image: ghcr.io/newandreas/throttlarr:latest
    container_name: throttlarr
    restart: unless-stopped
    # Use internal docker networking (no ports exposed) if Plex/Jellyfin/Emby are in the same network
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
      
      # Multi-Stage Speed Limits (Applies to SABnzbd and qBittorrent combined)
      - FULL_SPEED=0                 # 0 = Unlimited
      - SOFT_THROTTLE_SPEED=45M      # Soft limit when a standard stream starts (e.g., 30M = 30 Megabytes/s)
      - HARD_THROTTLE_SPEED=25M      # Escalated limit for heavy streaming loads
      
      # Escalation Triggers
      - HARD_THROTTLE_STREAMS=3      # Engage hard throttle if this many active streams are playing
      - HARD_THROTTLE_BITRATE=50000  # Engage hard throttle if a single stream exceeds this bitrate (in Kbps, e.g., 50000 = 50 Mbps video)

      # Tracearr Config
      - TRACEARR_URL=tracearr:3000
      - TRACEARR_TOKEN=${TRACEARR_API_KEY}
      - TRACEARR_SYNC_INTERVAL=20    # How often to poll Tracearr and adjust download queue in seconds
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

### Webhooks (optional but recommended for instant response)

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

