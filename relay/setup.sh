#!/bin/bash
# ============================================================
# PACbot Relay Server Setup — Ubuntu 22.04+ / Debian
# ============================================================
# Run as root on a fresh DigitalOcean Droplet (or any VPS):
#   chmod +x setup.sh && sudo ./setup.sh
#
# Requires: domain name pointed at this server's IP (for TLS cert).
# Set RELAY_DOMAIN before running, or pass as argument:
#   RELAY_DOMAIN=1453.life ./setup.sh
# ============================================================
set -e

RELAY_DOMAIN="${RELAY_DOMAIN:-${1:-}}"
APP_DIR="/opt/pacbot-relay"
SERVICE_NAME="pacbot-relay"
SERVICE_USER="pacbot"
RELAY_PORT=8090  # internal port, nginx proxies to this

if [ -z "$RELAY_DOMAIN" ]; then
    echo "ERROR: Set RELAY_DOMAIN first."
    echo "  RELAY_DOMAIN=1453.life ./setup.sh"
    exit 1
fi

echo "=== PACbot Relay Server Setup ==="
echo "Domain: $RELAY_DOMAIN"

# 1. Install dependencies
echo "[1/7] Installing packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx > /dev/null

# 2. Create service user (non-root)
echo "[2/7] Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# 3. Create app directory
echo "[3/7] Setting up $APP_DIR..."
mkdir -p "$APP_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# 4. Check for relay_server.py
if [ ! -f "$APP_DIR/relay_server.py" ]; then
    echo ""
    echo "ERROR: relay_server.py not found in $APP_DIR"
    echo "Copy it first:  scp relay_server.py root@<droplet-ip>:$APP_DIR/"
    echo "Then re-run this script."
    exit 1
fi

# 5. Create venv and install aiohttp
echo "[4/7] Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet aiohttp

# 6. Generate a random secret if none provided
if [ -z "$RELAY_SECRET" ]; then
    RELAY_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo ""
    echo "Generated RELAY_SECRET: $RELAY_SECRET"
    echo ""
fi

# 7. Create systemd service (non-root)
echo "[5/7] Creating systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=PACbot WebSocket Relay
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=RELAY_SECRET=$RELAY_SECRET
Environment=RELAY_PORT=$RELAY_PORT
ExecStart=$APP_DIR/venv/bin/python relay_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# 8. Configure nginx as TLS reverse proxy
echo "[6/7] Configuring nginx..."
cat > "/etc/nginx/sites-available/$SERVICE_NAME" <<EOF
server {
    listen 80;
    server_name $RELAY_DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$RELAY_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /ws/tunnel {
        proxy_pass http://127.0.0.1:$RELAY_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
EOF

ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/$SERVICE_NAME"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# 9. Get TLS certificate (auto-configures nginx for HTTPS)
echo "[7/7] Obtaining TLS certificate..."
certbot --nginx -d "$RELAY_DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect

# 10. Enable and start relay service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Service status:  systemctl status $SERVICE_NAME"
echo "View logs:       journalctl -u $SERVICE_NAME -f"
echo "Renew cert:      certbot renew --dry-run"
echo ""
echo "Relay URL:       wss://$RELAY_DOMAIN/ws/tunnel"
echo "Dashboard:       https://$RELAY_DOMAIN/<bot_name>"
echo "Shared secret:   $RELAY_SECRET"
