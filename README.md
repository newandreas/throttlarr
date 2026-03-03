# Throttlarr

Throttlarr is a Python service that manages your download speeds. Monitors your Plex and Jellyfin streams via webhooks and [Tracearr](https://github.com/connorgallopo/Tracearr), instantly throttling qBittorrent and SABnzbd when someone hits play to ensure a buffer-free viewing experience.

> [!CAUTION]
> This app was coded with the help of LLMs, I am not a professional coder. Don't trust the the app to be safe enough to expose to the internet.

## 🛠️ Features

* **Instant Response:** Uses webhooks to throttle speeds the second a stream starts.

* Periodically polls Tracearr to ensure speeds are only released when we know nobody is watching.


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
      - SAB_THROTTLE_SPEED=20M # Speed when watching (e.g., 20M)
      - SAB_FULL_SPEED=0      # 0 = Unlimited

      # Tracearr Config
      - TRACEARR_URL=tracearr:3000
      - TRACEARR_TOKEN=${TRACEARR_API_KEY}
      - TRACEARR_SYNC_INTERVAL=300 # How often to poll Tracearr in seconds (default: 300, 5 minutes)
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
### Webhooks

Point your media servers to the following endpoints:

    Plex: http://throttlarr:5000/plex

    Jellyfin: http://throttlarr:5000/jellyfin

### 🦑 Jellyfin:
* Go to Plugins
* Download and install Webhook plugin and restart Jellyfin
* Go to Plugins, press Webhook and press Settings
* Add Generic Destination
* Add http://throttlarr:5000/jellyfin
* Select "Playback Start" and "Playback Stop"
* Copy and paste this into Template:

```json
{
  "NotificationType": "{{NotificationType}}",
  "DeviceId": "{{DeviceId}}",
  "DeviceName": "{{DeviceName}}",
  "Username": "{{NotificationUsername}}",
  "MediaTitle": "{{Name}}"
}
```

### Plex:
* Go to Settings
* Under your user, select Webhooks
* Add http://throttlarr:5000/plex

### SABnzbd:
> [!IMPORTANT]
> Because this app communicates via Docker's internal DNS, you must allow the hostname in SABnzbd. Go to SABnzbd Settings -> General, Switch to Advanced View. Add "sabnzbd" to the Host Whitelist field and save. It should be simply "sabnzbd.example.com, sabnzbd"
