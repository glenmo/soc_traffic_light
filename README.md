# SOC Traffic Light

The Flask app behind most of the public pages on **https://monitor.mooramoora.org.au/**:
the battery traffic light (`/basic/`), the Lodge kiosk (`/kiosk`), the public front page (`/`)
and the MooraMoora mesh page (`/mesh`). It runs on pignus; see `CLAUDE.md` for a map of
routes, data sources and constraints.

The traffic light is a glanceable status page for the Wombat Hollow microgrid batteries: one
green / orange / red light, the guidance for each colour, and the SP Pro and Solis SoC with
charge or discharge power. It's meant to be readable from across the room and left running
for weeks on a wall display.

## How it works

This app does **not** poll any inverters directly. A background thread fetches the JSON
endpoints of the existing `microgrid_remote_monitor` app every 5 s and turns the SoCs into
a traffic light.

```
   server_app.py on pignus :8100     (microgrid_remote_monitor; data pushed from rubberduck)
            │
            │  GET /api/sppro/data
            │  GET /api/solis/data
            ▼
   soc_traffic_light/app.py on pignus :8765
            │
            ▼
   Apache, monitor.mooramoora.org.au  →  browsers, drongo's kiosk screen
```

## Thresholds

Default mapping (configurable on the CLI):

| Light  | SOC range |
|--------|-----------|
| Green  | ≥ 70 %    |
| Orange | 40 – 70 % |
| Red    | < 40 %    |

## Deployment (pignus)

The live instance runs on pignus from `~/soc_traffic_light` as `soc-traffic-light.service`:

```
venv/bin/python app.py --host 0.0.0.0 --port 8765 --upstream http://localhost:8100 \
    --green-min 70 --orange-min 40 --site-name "Wombat Hollow Microgrid"
```

To deploy a change:

```bash
git push
ssh pignus 'cd ~/soc_traffic_light && git pull --ff-only && sudo systemctl restart soc-traffic-light.service'
```

Restart even after template-only changes, because Flask caches templates. drongo picks up
kiosk changes on the page's 15-minute self-reload.

To install from scratch (on pignus, or another host pointing at a different upstream):

```bash
git clone https://github.com/glenmo/soc_traffic_light.git ~/soc_traffic_light
cd ~/soc_traffic_light
sudo bash install.sh --upstream http://localhost:8100 --port 8765 --site-name "Wombat Hollow Microgrid"
```

desky also runs an old LAN copy (`--upstream http://rubberduck.local:5000`, port 8765) that is
still at the initial commit and isn't updated by the steps above.

## CLI options

```
--host           Flask listen address              (default: 0.0.0.0)
--port           Flask listen port                 (default: 8080; pignus uses 8765)
--upstream       URL of microgrid_remote_monitor   (default: http://rubberduck.local:5000)
--poll-interval  Seconds between upstream fetches  (default: 5)
--request-timeout HTTP timeout per upstream call   (default: 4)
--green-min      SOC %% at which light goes green  (default: 70)
--orange-min     SOC %% at which light goes orange (default: 40)
--site-name      Heading text                      (default: Wombat Hollow Microgrid)
--debug          Flask debug mode
```

## Install script options

`install.sh` is a thin wrapper that:

1. Creates a venv in `./venv`
2. Pip-installs `requirements.txt`
3. Writes `/etc/systemd/system/soc-traffic-light.service`
4. Enables + starts it

Its own defaults are `--port 8080`, `--upstream http://rubberduck.local:5000` and
`--site-name Mooramoora`, so pass the pignus values shown above.

```
sudo bash install.sh \
    --upstream http://localhost:8100 \
    --port 8765 \
    --green-min 70 \
    --orange-min 40 \
    --site-name "Wombat Hollow Microgrid"
```

## Routes

| App route | Public URL | Description |
| --------- | ---------- | ----------- |
| `GET /` | `/basic/` | Traffic-light page (HTML) |
| `GET /home` | `/` | Public front page: tiles of the kiosk screens |
| `GET /kiosk` | `/kiosk` | Lodge kiosk for drongo's 1920x1080 screen |
| `GET /mesh`, `GET /mesh/data` | same | MooraMoora mesh messages; 404 without `?k=<token>` |
| `GET /api/soc` | same | JSON: per-device SOC, status, power, timestamps |
| `GET /api/extras` | same | JSON: forecast, fire danger, events, kiosk message |
| `GET /healthz` | same | Liveness probe: 200 OK if either inverter is reporting |

Example `/api/soc` response:

```json
{
  "site": "Wombat Hollow Microgrid",
  "thresholds": { "green_min": 70, "orange_min": 40 },
  "upstream": "http://localhost:8100",
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

## Apache (pignus)

The app is served from `/etc/apache2/sites-available/monitor.mooramoora.org.au.conf`. The parts
that reach it, in config order:

```apache
# This app's API endpoints, above the /api/ catch-all that goes to server_app.py on :8100
ProxyPass        /api/soc    http://127.0.0.1:8765/api/soc
ProxyPass        /api/extras http://127.0.0.1:8765/api/extras
ProxyPass        /healthz    http://127.0.0.1:8765/healthz
ProxyPass        /api/       http://127.0.0.1:8100/api/

# ... other apps (/advanced/, /ev/, /comfort/, /hotwater/, /weather/, ...) and the
# password-protected /overview/ tile page (see the noisy-index repo) ...

# Public front page at exactly /. The empty () is needed: a ProxyPassMatch target with
# no backreference gets the request path appended, so / would become /home/ (a 404).
ProxyPassMatch   ^/()$ http://127.0.0.1:8765/home$1

# Traffic light page at /basic/, then the catch-all (/kiosk, /mesh, /mesh/data, ...)
RewriteRule ^/basic$ /basic/ [R=301,L]
ProxyPass        /basic/     http://127.0.0.1:8765/
ProxyPassReverse /basic/     http://127.0.0.1:8765/
ProxyPass        /           http://127.0.0.1:8765/
ProxyPassReverse /           http://127.0.0.1:8765/
```

Because `/api/` belongs to :8100, new data endpoints for this app need a path outside `/api/`
(like `/mesh/data`) or their own `ProxyPass` above the `/api/` line. Back up the vhost before
editing it, and run `sudo apache2ctl configtest` before `sudo systemctl reload apache2`.

## Front-end behaviour

The page is intentionally robust against being left running for weeks on
a wall display:

- Fetches `/api/soc` every 5 s with `cache: 'no-store'` and a cache-busting query.
- Dims a card and shows "no data from upstream" if a device's data is older than 60 s.
- Shows a red banner across the top if neither inverter is reporting.
- Front-end watchdog: `location.reload()` if no successful fetch arrives for 2 min.
- Meta-refresh backstop: hard-reloads the page every 15 min regardless.

## Public front page (`/`)

`https://monitor.mooramoora.org.au/` is `templates/home.html` (route `/home`; pignus's Apache
sends exactly `/` there with a `ProxyPassMatch`). It shows a live, scaled preview tile of each
public screen in the kiosk rotation (`/kiosk`, `/weather/`, `/basic/`), each linking to the page.
`/mesh` is left out because it needs a viewer token. Framed in a tile, `/kiosk` skips its own
rotation. The password-protected tile page of every web app on pignus (web-index.service) is at
`/overview/`.

## Lodge kiosk: MooraMoora mesh page

On drongo the `/kiosk` page rotates every 30 s through four views: the kiosk,
`/weather/` (the Lodge weather page: SMA Meteostation readings, a 24 h trend and a 7-day forecast,
served by `weather_monitor` in the microgrid_remote_monitor repo), `/basic/` (the Wombat Hollow
Microgrid battery state of charge page), and `/mesh`, which lists recent
text messages on the MooraMoora Meshtastic channel and how many radios were heard in the last 24 h.
Anyone else opening `/kiosk` gets only the first three. The `/` guide is no longer in the rotation
because `/` now requires a password.

`mesh.py` subscribes to `msh/ANZ/2/e/MooraMoora/#` on `mqtt.meshtastic.org` and decrypts the packets
with the channel key. Messages appear only if a radio at Moora Moora uplinks that channel to MQTT.
The latest 30 messages and the node names are kept in `mesh_state.json`, so a restart doesn't blank
the page.

Two files next to `app.py` configure it. Both are git-ignored and should be mode 600:

| File | Contents |
|---|---|
| `mesh_psk.txt` | The channel key in base64, as in the channel URL. Without it the feed is off. |
| `mesh_token.txt` | One viewer per line, `<name> <token>` (e.g. `drongo …`, `glen …`). `/mesh` and `/mesh/data` answer only with `?k=<token>` and return 404 otherwise, including when this file is missing. It is re-read on every request, so add or delete a line to grant or revoke access without a restart. |

The data endpoint is `/mesh/data`, not under `/api/`: pignus's Apache sends `/api/` to the advanced dashboard on port 8100.

drongo opens `/kiosk?mesh=<token>`; its `autostart` reads the token from `~/.config/kiosk-mesh-token`.
Anyone else with a token opens `/mesh?k=<token>` directly; on a phone the page switches to a scrolling layout.
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
