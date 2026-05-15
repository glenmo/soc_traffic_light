"""End-to-end smoke test for soc_traffic_light.

Spins up a tiny fake upstream that mimics rubberduck's
/api/sppro/data and /api/solis/data endpoints, points the real
app.py at it, then verifies /api/soc returns the right traffic-light
statuses for green, orange, and red SOC values.
"""

import json
import os
import socket
import subprocess
import sys
import time
import threading
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---- fake upstream ---------------------------------------------------------
FAKE_SOC = {"sppro": 85.0, "solis": 50.0}
FAKE_POWER = {"sppro": 3200.0, "solis": -1500.0}


class FakeUpstream(BaseHTTPRequestHandler):
    def do_GET(self):
        for device in ("sppro", "solis"):
            if self.path.startswith(f"/api/{device}/data"):
                payload = {
                    "battery_soc": FAKE_SOC[device],
                    "battery_power": FAKE_POWER[device],
                    "_timestamp": datetime.now().isoformat(),
                    "_read_ok": True,
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(404); self.end_headers()

    def log_message(self, *a, **kw):
        pass  # quiet


def free_port():
    s = socket.socket(); s.bind(("", 0)); p = s.getsockname()[1]; s.close()
    return p


def wait_for(url, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return r.read()
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Timeout waiting for {url}")


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


def main():
    # 1. fake upstream
    up_port = free_port()
    fake = HTTPServer(("127.0.0.1", up_port), FakeUpstream)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{up_port}"
    print(f"[fake]  upstream listening on {upstream}")

    # 2. real app.py pointed at fake upstream
    app_port = free_port()
    env = dict(os.environ); env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py"),
         "--host", "127.0.0.1",
         "--port", str(app_port),
         "--upstream", upstream,
         "--poll-interval", "1"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{app_port}"
    print(f"[app]   listening on {base}, pid={proc.pid}")

    failures = []

    try:
        wait_for(base + "/healthz", timeout=15)

        def assert_status(device_soc, expected):
            FAKE_SOC["sppro"] = device_soc
            FAKE_SOC["solis"] = device_soc
            # Wait for poll-interval + a little
            time.sleep(2.0)
            data = fetch_json(base + "/api/soc")
            got_s = data["devices"]["sppro"]["status"]
            got_l = data["devices"]["solis"]["status"]
            ok = (got_s == expected and got_l == expected)
            mark = "OK " if ok else "FAIL"
            print(f"  [{mark}] soc={device_soc:>5}%  expected={expected:<7}  "
                  f"sppro={got_s:<7} solis={got_l:<7}")
            if not ok:
                failures.append((device_soc, expected, got_s, got_l))

        # Test the three thresholds with default 70/40 boundaries
        print("\n[test] traffic-light thresholds (green>=70, orange 40-70, red<40)")
        assert_status(85, "green")     # well above 70
        assert_status(70, "green")     # exactly at green threshold
        assert_status(69, "orange")    # just below green
        assert_status(40, "orange")    # exactly at orange threshold
        assert_status(39, "red")       # just below orange
        assert_status(5,  "red")       # well below

        # Test the index page renders
        print("\n[test] / renders index.html")
        with urllib.request.urlopen(base + "/", timeout=3) as r:
            html = r.read().decode()
            assert "Battery State of Charge" in html, "missing header"
            assert "SP Pro" in html and "Solis" in html, "device labels missing"
            print("  [OK ] index.html contains expected markers")

        # Test power-flow direction & magnitude
        print("\n[test] power flow direction surfaces correctly")
        time.sleep(2)
        data = fetch_json(base + "/api/soc")
        sp = data["devices"]["sppro"]
        so = data["devices"]["solis"]
        # SP Pro is charging (+3200 W), Solis is discharging (-1500 W)
        if not (sp["power_w"] == 3200.0 and so["power_w"] == -1500.0):
            failures.append(("power_flow", "3200/-1500",
                             sp["power_w"], so["power_w"]))
            print(f"  [FAIL] sppro={sp['power_w']}, solis={so['power_w']}")
        else:
            print(f"  [OK ] sppro=+{sp['power_w']:.0f} W charging, "
                  f"solis={so['power_w']:.0f} W discharging")

        # Test upstream-down behaviour
        print("\n[test] upstream down -> status=unknown, online=False")
        fake.shutdown()
        fake.server_close()   # actually release the listening socket
        time.sleep(3)
        data = fetch_json(base + "/api/soc")
        for dev in ("sppro", "solis"):
            d = data["devices"][dev]
            if d["status"] != "unknown" or d["online"]:
                failures.append((dev, "unknown/offline",
                                 d["status"], d["online"]))
                print(f"  [FAIL] {dev}: status={d['status']} online={d['online']}")
            else:
                print(f"  [OK ] {dev}: status=unknown, online=False, "
                      f"error={d['error'][:60] if d['error'] else None}")

    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()

    print()
    if failures:
        print(f"!! {len(failures)} failure(s):")
        for f in failures: print("  -", f)
        sys.exit(1)
    print("All smoke tests passed.")


if __name__ == "__main__":
    main()
