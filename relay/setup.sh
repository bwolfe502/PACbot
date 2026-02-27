#!/bin/bash
# ============================================================
# PACbot Relay Server Setup — Ubuntu 22.04+ / Debian
# ============================================================
# Run as root on a fresh DigitalOcean Droplet (or any VPS):
#   curl -sL <url>/setup.sh | bash
# Or copy this file to the server and run:
#   chmod +x setup.sh && sudo ./setup.sh
# ============================================================
set -e

APP_DIR="/opt/pacbot-relay"
SERVICE_NAME="pacbot-relay"

echo "=== PACbot Relay Server Setup ==="

# 1. Install Python
echo "[1/5] Installing Python..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv > /dev/null

# 2. Create app directory
echo "[2/5] Setting up $APP_DIR..."
mkdir -p "$APP_DIR"

# 3. Check for relay_server.py
if [ ! -f "$APP_DIR/relay_server.py" ]; then
    echo ""
    echo "ERROR: relay_server.py not found in $APP_DIR"
    echo "Copy it first:  scp relay_server.py root@<droplet-ip>:$APP_DIR/"
    echo "Then re-run this script."
    exit 1
fi

# 4. Create venv and install aiohttp
echo "[3/5] Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet aiohttp

# 5. Generate a random secret if none provided
if [ -z "$RELAY_SECRET" ]; then
    RELAY_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo ""
    echo "Generated RELAY_SECRET: $RELAY_SECRET"
    echo "Save this — you'll need it in PACbot's settings."
    echo ""
fi

# 6. Create systemd service
echo "[4/5] Creating systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=PACbot WebSocket Relay
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=RELAY_SECRET=$RELAY_SECRET
Environment=RELAY_PORT=80
ExecStart=$APP_DIR/venv/bin/python relay_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 7. Enable and start
echo "[5/5] Starting service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Service status:  systemctl status $SERVICE_NAME"
echo "View logs:       journalctl -u $SERVICE_NAME -f"
echo ""
echo "Relay URL:       ws://$(hostname -I | awk '{print $1}')/ws/tunnel"
echo "Shared secret:   $RELAY_SECRET"
echo ""
echo "In PACbot settings:"
echo "  Relay URL:     ws://$(hostname -I | awk '{print $1}')/ws/tunnel"
echo "  Shared Secret: $RELAY_SECRET"
echo "  Bot Name:      (pick a name, e.g. 'home')"
