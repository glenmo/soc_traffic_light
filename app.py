#!/usr/bin/env python3
"""
SOC Traffic Light — simple microgrid SOC status page.

Fetches battery SOC + battery power from an upstream microgrid_remote_monitor
instance (the existing Flask app on rubberduck.local, or the pignus mirror)
and serves a stripped-down traffic-light dashboard:

  - SP Pro on the left, Solis on the right
  - Big coloured circle (green / orange / red) per inverter
  - SOC % under each circle
  - Battery power flow (charging / discharging kW)
  - Small last-updated timestamp

Thresholds (configurable on the CLI):
  GREEN  : SOC >= 70 %
  ORANGE : 40 <= SOC < 70 %
  RED    : SOC < 40 %

Typical deployment:

  # On desky.local (LAN dashboard)
  python3 app.py --upstream http://rubberduck.local:5000

  # On pignus (internet mirror — points at the existing server_app on the VPS)
  python3 app.py --upstream http://localhost:8100 --port 8080
"""

import argparse
import logging
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("soc_traffic_light")

# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
app = Flask(__name__, template_folder="templates", static_folder="static")


@app.after_request
def _no_cache_api(resp):
    """Stop the kiosk browser from caching JSON polls."""
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# --------------------------------------------------------------------------- #
# Config (filled in by main())
# --------------------------------------------------------------------------- #
class Config:
    upstream = "http://rubberduck.local:5000"
    poll_interval = 5  # seconds — how often we re-fetch upstream
    request_timeout = 4  # seconds per upstream HTTP call
    green_min = 70
    orange_min = 40
    site_name = "Mooramoora"


CONFIG = Config()


# --------------------------------------------------------------------------- #
# Upstream poller
# --------------------------------------------------------------------------- #
class UpstreamPoller:
    """Polls /api/sppro/data and /api/solis/data on the upstream Flask app
    and caches the latest decoded SOC + power per inverter."""

    DEVICES = ("sppro", "solis")

    def __init__(self, upstream: str, poll_interval: int, request_timeout: int):
        self.upstream = upstream.rstrip("/")
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self.lock = threading.Lock()
        self.state = {d: self._empty_state() for d in self.DEVICES}
        self._stop_event = threading.Event()
        self._thread = None

    @staticmethod
    def _empty_state():
        return {
            "soc": None,         # %
            "power_w": None,     # signed W: + charging, - discharging
            "fetched_at": None,  # ISO timestamp from upstream payload if present
            "polled_at": None,   # ISO timestamp from local clock when we last polled
            "online": False,
            "error": None,
        }

    def _fetch_one(self, device: str) -> dict:
        url = f"{self.upstream}/api/{device}/data"
        try:
            r = requests.get(url, timeout=self.request_timeout)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            return {**self._empty_state(),
                    "polled_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(e).__name__}: {e}"}

        # The Solis/SP Pro endpoints return a flat dict keyed by register name.
        # `battery_soc` is %, `battery_power` is W (signed: + charge, - discharge).
        soc = payload.get("battery_soc")
        power = payload.get("battery_power")
        # Some SP Pro variants expose kW instead of W under a different key —
        # be defensive.
        if power is None:
            for alt in ("battery_power_kw", "batt_power", "battery_power_w"):
                if alt in payload:
                    power = payload[alt]
                    if alt == "battery_power_kw":
                        power = power * 1000.0
                    break

        return {
            "soc": float(soc) if soc is not None else None,
            "power_w": float(power) if power is not None else None,
            "fetched_at": payload.get("_timestamp"),
            "polled_at": datetime.now(timezone.utc).isoformat(),
            "online": payload.get("_read_ok", soc is not None),
            "error": None,
        }

    def poll_once(self):
        for device in self.DEVICES:
            new_state = self._fetch_one(device)
            with self.lock:
                self.state[device] = new_state
            if new_state["error"]:
                log.warning("Upstream %s fetch failed: %s", device, new_state["error"])

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.exception("Unexpected poll error: %s", e)
            self._stop_event.wait(self.poll_interval)

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Polling %s every %ds", self.upstream, self.poll_interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def snapshot(self) -> dict:
        with self.lock:
            return {d: dict(s) for d, s in self.state.items()}


POLLER: UpstreamPoller = None  # filled in by main()


# --------------------------------------------------------------------------- #
# Status helpers
# --------------------------------------------------------------------------- #
def soc_to_status(soc, green_min, orange_min):
    """Map a numeric SOC to a traffic-light status string."""
    if soc is None:
        return "unknown"
    if soc >= green_min:
        return "green"
    if soc >= orange_min:
        return "orange"
    return "red"


def build_response():
    snap = POLLER.snapshot() if POLLER else {}
    devices = {}
    for name, label in (("sppro", "SP Pro"), ("solis", "Solis")):
        s = snap.get(name, {})
        soc = s.get("soc")
        devices[name] = {
            "label": label,
            "soc": soc,
            "status": soc_to_status(soc, CONFIG.green_min, CONFIG.orange_min),
            "power_w": s.get("power_w"),
            "online": bool(s.get("online")),
            "error": s.get("error"),
            "fetched_at": s.get("fetched_at"),
            "polled_at": s.get("polled_at"),
        }
    return {
        "site": CONFIG.site_name,
        "thresholds": {"green_min": CONFIG.green_min,
                       "orange_min": CONFIG.orange_min},
        "upstream": CONFIG.upstream,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "devices": devices,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template(
        "index.html",
        site_name=CONFIG.site_name,
        green_min=CONFIG.green_min,
        orange_min=CONFIG.orange_min,
    )


@app.route("/api/soc")
def api_soc():
    return jsonify(build_response())


@app.route("/healthz")
def healthz():
    snap = POLLER.snapshot() if POLLER else {}
    any_online = any(s.get("online") for s in snap.values())
    return jsonify({"ok": True, "any_online": any_online}), 200


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    global POLLER

    parser = argparse.ArgumentParser(description="SOC Traffic Light")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Flask listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Flask listen port (default: 8080)")
    parser.add_argument("--upstream", default="http://rubberduck.local:5000",
                        help="Upstream microgrid_remote_monitor base URL "
                             "(default: http://rubberduck.local:5000)")
    parser.add_argument("--poll-interval", type=int, default=5,
                        help="How often to refetch upstream (seconds, default: 5)")
    parser.add_argument("--request-timeout", type=int, default=4,
                        help="HTTP timeout per upstream call (seconds, default: 4)")
    parser.add_argument("--green-min", type=int, default=70,
                        help="SOC %% at which light goes green (default: 70)")
    parser.add_argument("--orange-min", type=int, default=40,
                        help="SOC %% at which light goes orange (default: 40)")
    parser.add_argument("--site-name", default="Mooramoora",
                        help="Site name shown in the header (default: Mooramoora)")
    parser.add_argument("--debug", action="store_true",
                        help="Flask debug mode")
    args = parser.parse_args()

    CONFIG.upstream = args.upstream
    CONFIG.poll_interval = args.poll_interval
    CONFIG.request_timeout = args.request_timeout
    CONFIG.green_min = args.green_min
    CONFIG.orange_min = args.orange_min
    CONFIG.site_name = args.site_name

    POLLER = UpstreamPoller(
        upstream=args.upstream,
        poll_interval=args.poll_interval,
        request_timeout=args.request_timeout,
    )
    POLLER.start()

    log.info("SOC Traffic Light listening on %s:%d (upstream=%s)",
             args.host, args.port, args.upstream)
    try:
        app.run(host=args.host, port=args.port,
                debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        POLLER.stop()


if __name__ == "__main__":
    main()
