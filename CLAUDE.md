# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A small Flask app (`app.py`, plus `extras.py` and `mesh.py`) behind most of the public pages on https://monitor.mooramoora.org.au/. It doesn't talk to any inverter. `UpstreamPoller` polls `microgrid_remote_monitor`'s `/api/sppro/data` and `/api/solis/data` every 5 s and turns the SoCs into a traffic light (green ≥ 70 %, orange ≥ 40 %).

| Route | Template | What it is | Public URL |
|---|---|---|---|
| `/` | `index.html` | Traffic-light guide | `/basic/` |
| `/home` | `home.html` | Tiles of the public kiosk screens, live scaled previews | `/` (exact) |
| `/kiosk` | `kiosk.html` | Lodge kiosk, 1920x1080, shown full-screen on drongo | `/kiosk` |
| `/mesh`, `/mesh/data` | `mesh.html` | MooraMoora Meshtastic messages, token-only (404 without `?k=`) | `/mesh?k=<token>` |
| `/api/soc`, `/api/extras`, `/healthz` | | JSON for the pages | same paths |

- `extras.py` fetches the forecast (Open-Meteo), CFA fire danger and Moora Moora events in a background thread, and reads the kiosk header message from `kiosk_message.txt`. `day_code()` picks the forecast icon. The same rule is copied in `microgrid_remote_monitor/weather_monitor/app.py`, so change both together.
- `mesh.py` subscribes to the MooraMoora channel on `mqtt.meshtastic.org` and decrypts it with `mesh_psk.txt`. Viewer tokens live in `mesh_token.txt`, which is re-read on every request. A Werkzeug log filter redacts `?k=` tokens, so don't log full request URLs elsewhere.
- `kiosk.html` also reads other apps on the same host: `/ev/…`, `/hotwater/api/state`, `/comfort/api/current` (the heater tiles) and `/api/solis|sppro/data`. It only works fully behind pignus's Apache. On drongo, `cycleViews()` rotates it every 30 s through `/weather/`, `/basic/` and `/mesh`. When framed (the `/home` previews) it skips the rotation.

## Deploying

The live instance is on **pignus**, in `~/soc_traffic_light`, as `soc-traffic-light.service` on port **8765** (`--upstream http://localhost:8100`). Apache (`/etc/apache2/sites-available/monitor.mooramoora.org.au.conf`) maps the public paths above onto it, with a catch-all `ProxyPass /`.

```bash
git push
ssh pignus 'cd ~/soc_traffic_light && git pull --ff-only && sudo systemctl restart soc-traffic-light.service'
```

Restart even for template-only changes, because Flask caches templates. drongo picks up kiosk changes on the page's 15-minute self-reload (repo `drongo-kiosk` covers restarting its Chromium sooner). desky also runs a LAN copy in `~/soc_traffic_light` (port 8765, upstream `rubberduck.local:5000`). Update it with `git pull` there, plus `venv/bin/pip install -r requirements.txt` if the requirements changed. `glen` has no passwordless sudo on desky, but the unit runs as `glen` with `Restart=always`, so `kill $(systemctl show -p MainPID --value soc-traffic-light.service)` restarts it. `/kiosk` there lacks the EV, hot water and heater data, because those come from other apps behind pignus's Apache.

The README's quick-start and Apache sections predate this setup (port 8080, `/soc/`). Trust the live unit and vhost over them.

## Testing

No test runner config. With a venv holding `requirements.txt` plus pytest:

```bash
venv/bin/python -m pytest tests/test_mesh.py   # mesh decoding and tokens
venv/bin/python tests/smoke_test.py            # starts app.py against a fake upstream, checks /api/soc
```

A quick check of a new route: `app.app.test_client().get("/route")`.

## Constraints

- Untracked runtime files on pignus are gitignored and must never be committed: `mesh_psk.txt`, `mesh_token.txt`, `mesh_state.json`, `kiosk_message.txt`.
- `/mesh` must stay token-only, and must not be linked from public pages.
- Apache sends `/api/` to the advanced dashboard on :8100. Only `/api/soc` and `/api/extras` reach this app, so new data endpoints need a path outside `/api/` (like `/mesh/data`) or a new Apache rule.
- The kiosk is laid out at 1920x1080 and scaled by `fitKiosk()`. Phones and portrait screens get a scrolling single column instead (the `MOBILE` media query), and the rotation is off there.
