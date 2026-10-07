# SOC Traffic Light

A minimal, glanceable status page for the Mooramoora microgrid battery SOC.

Two coloured circles (SP Pro and Solis) — green / orange / red — plus the
SOC %, battery charge/discharge direction, and a small last-updated stamp.
Designed to be readable from across the room on a wall-mounted tablet or
old monitor.

```
   ┌────────────────────┐   ┌────────────────────┐
   │       SP PRO       │   │       SOLIS        │
   │                    │   │                    │
   │       ●  82%       │   │       ●  79%       │
   │                    │   │                    │
   │   ▲ 3.4 kW         │   │   ▼ 1.1 kW         │
   │     charging       │   │     discharging    │
   │                    │   │                    │
   │   updated 4s ago   │   │   updated 5s ago   │
   └────────────────────┘   └────────────────────┘

         Green ≥70 %     Orange 40–70 %     Red <40 %
```

## How it works

This app does **not** poll any inverters directly. Instead, it fetches the
existing JSON endpoints from your `microgrid_remote_monitor` Flask app
(rubberduck on the LAN, pignus on the internet) and re-renders the SOC
fields as a traffic light.

```
   rubberduck.local:5000          (existing app — polls Solis + SP Pro)
            │
            │  GET /api/sppro/data
            │  GET /api/solis/data
            ▼
   desky.local:8080   ←─────── soc_traffic_light/app.py
            │
            ▼
       browser on tablet / wall display
```

The same code runs on `pignus.arachnoid.net.au` for the internet-facing
mirror; it just points at a different upstream (`http://localhost:8100`
or wherever the existing `server_app.py` is listening).

## Thresholds

Default mapping (configurable on the CLI):

| Light  | SOC range |
|--------|-----------|
| Green  | ≥ 70 %    |
| Orange | 40 – 70 % |
| Red    | < 40 %    |

## Quick start — desky.local (LAN dashboard)

```bash
git clone <this repo> ~/soc_traffic_light
cd ~/soc_traffic_light
sudo bash install.sh
```

That installs a venv, sets up `soc-traffic-light.service` pointing at
`http://rubberduck.local:5000`, and starts it.

Browse: `http://desky.local:8080/`

## Quick start — pignus.arachnoid.net.au (internet mirror)

On pignus the existing `server_app.py` already runs on `:8100`, so just
point this app at localhost:

```bash
git clone <this repo> /opt/soc_traffic_light
cd /opt/soc_traffic_light
sudo bash install.sh --upstream http://localhost:8100 --port 8080
```

Then either browse directly to `http://pignus.arachnoid.net.au:8080/`,
or — recommended — reverse-proxy it behind your existing Apache vhost
(see *Apache vhost snippet* below).

## CLI options

```
--host           Flask listen address              (default: 0.0.0.0)
--port           Flask listen port                 (default: 8080)
--upstream       URL of microgrid_remote_monitor   (default: http://rubberduck.local:5000)
--poll-interval  Seconds between upstream fetches  (default: 5)
--request-timeout HTTP timeout per upstream call   (default: 4)
--green-min      SOC %% at which light goes green  (default: 70)
--orange-min     SOC %% at which light goes orange (default: 40)
--site-name      Heading text                      (default: Mooramoora)
--debug          Flask debug mode
```

## Install script options

`install.sh` is a thin wrapper that:

1. Creates a venv in `./venv`
2. Pip-installs `requirements.txt`
3. Writes `/etc/systemd/system/soc-traffic-light.service`
4. Enables + starts it

```
sudo bash install.sh \
    --upstream http://localhost:8100 \
    --port 8080 \
    --green-min 70 \
    --orange-min 40 \
    --site-name Mooramoora
```

## API

| Endpoint     | Description                                          |
| ------------ | ---------------------------------------------------- |
| `GET /`      | Traffic-light dashboard (HTML)                       |
| `GET /api/soc` | JSON: per-device SOC, status, power, timestamps    |
| `GET /healthz` | Liveness probe — 200 OK if either inverter is reporting |

Example `/api/soc` response:

```json
{
  "site": "Mooramoora",
  "thresholds": { "green_min": 70, "orange_min": 40 },
  "upstream": "http://rubberduck.local:5000",
  "server_time": "2026-05-15T08:42:11.123456+00:00",
  "devices": {
    "sppro": {
      "label": "SP Pro",
      "soc": 82.0,
      "status": "green",
      "power_w": 3400.0,
      "online": true,
      "error": null,
      "fetched_at": "2026-05-15T18:42:08.901234",
      "polled_at":  "2026-05-15T08:42:09.123456+00:00"
    },
    "solis": { "label": "Solis", "soc": 79.0, "status": "green", "power_w": -1100.0, ... }
  }
}
```

## Apache vhost snippet (pignus)

To serve the traffic light on the same hostname as your existing dashboard:

```apache
<VirtualHost *:443>
    ServerName pignus.arachnoid.net.au

    # ... your existing SSL / log / etc. config ...

    # Traffic-light page at /soc/
    ProxyPass        /soc/  http://127.0.0.1:8080/
    ProxyPassReverse /soc/  http://127.0.0.1:8080/
</VirtualHost>
```

After this, `https://pignus.arachnoid.net.au/soc/` shows the lights.

## Front-end behaviour

The page is intentionally robust against being left running for weeks on
a wall display:

- Fetches `/api/soc` every 5 s with `cache: 'no-store'` and a cache-busting query.
- Dims a card and shows "no data from upstream" if a device's data is older than 60 s.
- Shows a red banner across the top if neither inverter is reporting.
- Front-end watchdog: `location.reload()` if no successful fetch arrives for 2 min.
- Meta-refresh backstop: hard-reloads the page every 15 min regardless.

## Lodge kiosk: MooraMoora mesh page

On drongo the `/kiosk` page rotates every 30 s through three views: the kiosk, the `/` guide, and
`/mesh`, which lists recent text messages on the MooraMoora Meshtastic channel and how many radios
were heard in the last 24 h. Anyone else opening `/kiosk` gets only the first two.

`mesh.py` subscribes to `msh/ANZ/2/e/MooraMoora/#` on `mqtt.meshtastic.org` and decrypts the packets
with the channel key. Messages appear only if a radio at Moora Moora uplinks that channel to MQTT.
The latest 30 messages and the node names are kept in `mesh_state.json`, so a restart doesn't blank
the page.

Two files next to `app.py` configure it. Both are git-ignored and should be mode 600:

| File | Contents |
|---|---|
| `mesh_psk.txt` | The channel key in base64, as in the channel URL. Without it the feed is off. |
| `mesh_token.txt` | A random token. `/mesh` and `/mesh/data` answer only with `?k=<token>` and return 404 otherwise, including when this file is missing. |

The data endpoint is `/mesh/data`, not under `/api/`: pignus's Apache sends `/api/` to the advanced dashboard on port 8100.

drongo opens `/kiosk?mesh=<token>`; its `autostart` reads the token from `~/.config/kiosk-mesh-token`.
Tests: `venv/bin/python -m pytest tests/test_mesh.py`.

## Files

```
soc_traffic_light/
├── app.py                  # Flask app + upstream poller
├── mesh.py                 # MooraMoora channel feed for the kiosk's /mesh page
├── requirements.txt        # flask, requests, paho-mqtt, cryptography
├── install.sh              # venv + systemd installer
├── templates/
│   └── index.html          # the traffic-light page
└── README.md
```

## License

GPL-2.0, same as the upstream `microgrid_remote_monitor` project.
