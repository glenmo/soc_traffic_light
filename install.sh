#!/usr/bin/env bash
#
# Install the SOC Traffic Light as a systemd service.
#
# Usage:
#   sudo bash install.sh                     # desky.local default — upstream=rubberduck.local:5000
#   sudo bash install.sh --upstream http://localhost:8100 --port 8080   # pignus mirror
#
# After install:
#   sudo systemctl status  soc-traffic-light.service
#   sudo journalctl -u     soc-traffic-light.service -f
#
set -euo pipefail

# ---- defaults --------------------------------------------------------------
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="soc-traffic-light.service"
SERVICE_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="8080"
HOST="0.0.0.0"
UPSTREAM="http://rubberduck.local:5000"
GREEN_MIN="70"
ORANGE_MIN="40"
SITE_NAME="Mooramoora"

# ---- arg parsing -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --upstream)    UPSTREAM="$2";    shift 2 ;;
    --port)        PORT="$2";        shift 2 ;;
    --host)        HOST="$2";        shift 2 ;;
    --green-min)   GREEN_MIN="$2";   shift 2 ;;
    --orange-min)  ORANGE_MIN="$2";  shift 2 ;;
    --site-name)   SITE_NAME="$2";   shift 2 ;;
    --user)        SERVICE_USER="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">> App dir:       $APP_DIR"
echo ">> Service user:  $SERVICE_USER"
echo ">> Upstream:      $UPSTREAM"
echo ">> Listen:        $HOST:$PORT"

# ---- venv ------------------------------------------------------------------
if [[ ! -d "$APP_DIR/venv" ]]; then
  echo ">> Creating venv at $APP_DIR/venv"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R "$SERVICE_USER" "$APP_DIR/venv" || true

# ---- systemd unit ----------------------------------------------------------
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
echo ">> Writing $UNIT_PATH"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Microgrid SOC Traffic Light (simple status page)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py \\
    --host ${HOST} \\
    --port ${PORT} \\
    --upstream ${UPSTREAM} \\
    --green-min ${GREEN_MIN} \\
    --orange-min ${ORANGE_MIN} \\
    --site-name "${SITE_NAME}"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo ">> Installed. Status:"
systemctl --no-pager status "${SERVICE_NAME}" || true
echo
echo ">> Open: http://$(hostname -f 2>/dev/null || hostname).local:${PORT}/"
