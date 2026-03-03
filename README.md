# Throttlarr

Throttlarr is a Python service that manages your download speeds. It monitors your **Plex**, **Jellyfin**, and **Emby** streams via webhooks and [Tracearr](https://github.com/connorgallopo/Tracearr), instantly throttling qBittorrent and SABnzbd when someone hits play to ensure a buffer-free viewing experience.

> [!CAUTION]
> This app was coded with the help of LLMs, I am not a professional coder. Don't trust the app to be safe enough to expose to the internet.

## 🛠️ Features

* **Instant Response:** Uses webhooks to throttle speeds the instant stream starts.
* Periodically polls Tracearr to ensure speeds are only increased when we know nobody is watching.
* **Scalable:** Supports 1, 2, or 100 media servers. If you have multiple Plex, Jellyfin, or Emby instances, Tracearr aggregates them all into one stream count.

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
      - SAB_THROTTLE_SPEED=20M # Speed when watching (e.g., 20M)
      - SAB_FULL_SPEED=0      # 0 = Unlimited

      # Tracearr Config
      - TRACEARR_URL=tracearr:3000
      - TRACEARR_TOKEN=${TRACEARR_API_KEY}
      - TRACEARR_SYNC_INTERVAL=300 # How often to poll Tracearr in seconds (default: 300, 5 minutes)
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
