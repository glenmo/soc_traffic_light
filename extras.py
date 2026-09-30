"""
extras.py - background fetchers for the Lodge kiosk page.

Drop into soc_traffic_light/ and hook into app.py with:

    from extras import Extras
    extras = Extras()
    extras.start()

    @app.route("/api/extras")
    def api_extras():
        return jsonify(extras.snapshot())

    @app.route("/kiosk")
    def kiosk():
        return render_template("kiosk.html")

Each source keeps its last good result. A failed fetch never blanks a
tile; the tile just shows its own "as of" time.
"""
import html
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("extras")

LAT, LON = -37.72, 145.55            # Mount Toolebewong
TZ = "Australia/Melbourne"
# Pretty /wp-json/ permalinks 404 on this site; ?rest_route= works.
WP_BASE = "https://mooramoora.org.au/"
WP_EVENTS_ROUTE = "/wp/v2/eventbrite_events"   # Import Eventbrite Events plugin
# CFA district feed: BOM fire danger rating + Total Fire Ban, today and 3-4 days out.
CFA_RSS = "https://www.cfa.vic.gov.au/cfa/rssfeed/central-firedistrict_rss.xml"
FIRE_DISTRICT = "Central"
UA = {"User-Agent": "Mozilla/5.0 (mooramoora-kiosk/1.0)"}
# Optional one-line Lodge notice for the kiosk header. Not /api/message: that's
# the advanced dashboard's legend. Edit the file, it's read on every request.
MESSAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiosk_message.txt")
REFRESH_SECS = 15 * 60
RETRY_SECS = 2 * 60                  # after a failed fetch, try again sooner


class Extras:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"forecast": None, "fire": None, "events": None}
        self._stamp = {"forecast": None, "fire": None, "events": None}
        self._err = {"forecast": None, "fire": None, "events": None}

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="extras")
        t.start()

    def snapshot(self):
        with self._lock:
            return {
                "server_time": datetime.now(timezone.utc).isoformat(),
                "message": _read_message(),
                "sources": {
                    k: {"data": self._data[k], "fetched_at": self._stamp[k], "error": self._err[k]}
                    for k in self._data
                },
            }

    def _loop(self):
        sources = (("forecast", self.fetch_forecast),
                   ("fire", self.fetch_fire),
                   ("events", self.fetch_events))
        due = {name: 0.0 for name, _ in sources}
        while True:
            for name, fn in sources:
                if time.monotonic() < due[name]:
                    continue
                try:
                    result = fn()
                    with self._lock:
                        self._data[name] = result
                        self._stamp[name] = datetime.now(timezone.utc).isoformat()
                        self._err[name] = None
                    due[name] = time.monotonic() + REFRESH_SECS
                except Exception as e:
                    log.warning("%s fetch failed: %s", name, e)
                    with self._lock:
                        self._err[name] = str(e)
                    due[name] = time.monotonic() + RETRY_SECS
            time.sleep(30)

    # ---- forecast: Open-Meteo, no key ----
    def fetch_forecast(self):
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT, "longitude": LON, "timezone": TZ,
                "current": "temperature_2m,weather_code,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
                "forecast_days": 4,
                # models=bom_access_global returns all nulls for this location,
                # so use Open-Meteo's default blend.
            },
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        days = []
        for i, d in enumerate(j["daily"]["time"]):
            days.append({
                "date": d,
                "min": j["daily"]["temperature_2m_min"][i],
                "max": j["daily"]["temperature_2m_max"][i],
                "code": j["daily"]["weather_code"][i],
                "rain_pct": j["daily"]["precipitation_probability_max"][i],
            })
        return {
            "now_temp": j["current"]["temperature_2m"],
            "now_code": j["current"]["weather_code"],
            "days": days,
        }

    # ---- fire danger: CFA district RSS ----
    def fetch_fire(self):
        r = requests.get(CFA_RSS, timeout=15, headers=UA)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            desc = html.unescape(item.findtext("description") or "")
            m = re.search(FIRE_DISTRICT + r":\s*([A-Z ]+?)\s*<", desc)
            if not m:
                continue                      # e.g. the "Fire restrictions" item
            day = datetime.strptime(item.findtext("title").strip(), "%A, %d %B %Y").date()
            # First paragraph is "Today, ... is not currently a day of Total Fire Ban."
            # or the TFB declaration.
            first = re.search(r"<p>(.*?)</p>", desc, re.S)
            first = first.group(1) if first else ""
            out.append({
                "date": day.isoformat(),
                "rating": m.group(1).strip().title(),     # "Moderate", "No Rating", ...
                "tfb": "Total Fire Ban" in first and "not" not in first.split(),
            })
        if not out:
            raise ValueError(FIRE_DISTRICT + " district not found in CFA feed")
        return {"district": FIRE_DISTRICT, "periods": out}

    # ---- events: WordPress REST (Eventbrite imports) ----
    def fetch_events(self):
        # The plugin keeps the event date in post meta that REST doesn't expose,
        # so read the "Date:" line off each event page (cached per post version).
        r = requests.get(WP_BASE, params={"rest_route": WP_EVENTS_ROUTE, "per_page": 20,
                                          "_fields": "id,title,link,date,modified"},
                         timeout=10, headers=UA)
        r.raise_for_status()
        today = datetime.now(ZoneInfo(TZ)).date().isoformat()
        out = []
        for p in r.json():
            start = self._event_date(p)
            if start and start < today:
                continue
            out.append({"title": html.unescape(p.get("title", {}).get("rendered", "")),
                        "start": start, "link": p.get("link")})
        out.sort(key=lambda e: (e["start"] is None, e["start"] or ""))
        return out

    def _event_date(self, p):
        cache = self.__dict__.setdefault("_event_dates", {})
        key = (p.get("id"), p.get("modified"))
        if key not in cache:
            try:
                page = requests.get(p["link"], timeout=10, headers=UA)
                page.raise_for_status()
                cache[key] = _parse_event_date(page.text, p.get("date", ""))
            except Exception as e:
                log.warning("event date for %s: %s", p.get("link"), e)
                return None                   # not cached, retried next round
        return cache[key]


def _read_message():
    try:
        with open(MESSAGE_FILE) as f:
            return " ".join(f.read().split())
    except OSError:
        return ""


def _parse_event_date(page_html, published):
    """Pull the date from the plugin's '<strong>Date:</strong> <p>October 2</p>'
    block. It has no year, so take the first such date on or after the post's
    publish date. Returns 'YYYY-MM-DD' or None."""
    m = re.search(r"<strong>Date:</strong>\s*<p>\s*([^<]+?)\s*</p>", page_html)
    if not m:
        return None
    text = html.unescape(m.group(1)).split(" - ")[0].strip()
    for fmt in ("%B %d, %Y", "%d %B %Y", "%B %d", "%d %B"):
        try:
            d = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if "%Y" in fmt:
        return d.date().isoformat()
    pub = date.fromisoformat(published[:10]) if published else date.today()
    candidate = date(pub.year, d.month, d.day)
    if candidate < pub:
        candidate = date(pub.year + 1, d.month, d.day)
    return candidate.isoformat()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    x = Extras()
    for fn in (x.fetch_forecast, x.fetch_fire, x.fetch_events):
        try:
            print(fn.__name__, json.dumps(fn(), indent=1)[:600])
        except Exception as e:
            print(fn.__name__, "FAILED:", e)
