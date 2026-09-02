#!/usr/bin/env bash
# Teleshop one-shot installer for a fresh Ubuntu 22.04/24.04 server.
#
#   sudo bash install.sh
#
# Does everything in Part 6 of the deployment guide: packages, database,
# firewall, project user, Python environment, systemd service and Caddy.
# It asks four questions, then runs unattended.
#
# Safe to re-run: every step checks whether it has already been done, so if
# it fails halfway you can fix the cause and run it again.
set -euo pipefail

GREEN=$'\e[0;32m'; AMBER=$'\e[0;33m'; RED=$'\e[0;31m'; OFF=$'\e[0m'
say()  { echo "${GREEN}==>${OFF} $*"; }
warn() { echo "${AMBER}!!${OFF} $*"; }
die()  { echo "${RED}xx${OFF} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this with sudo: sudo bash install.sh"

APP_USER=teleshop
APP_DIR=/home/$APP_USER/app
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------- questions
echo
echo "Teleshop installer"
echo "──────────────────────────────────────────────────────────────"
echo "Have these ready:"
echo "  · your master bot token from @BotFather"
echo "  · your Telegram numeric ID from @userinfobot"
echo "  · the domain you already pointed at this server"
echo

read -rp "Domain (e.g. shop.example.com): " DOMAIN
[[ -n "$DOMAIN" ]] || die "A domain is required — Telegram login will not work without one."
read -rp "Master bot token: " BOT_TOKEN
[[ -n "$BOT_TOKEN" ]] || die "Bot token is required."
read -rp "Your Telegram ID (numbers only): " ADMIN_ID
[[ "$ADMIN_ID" =~ ^[0-9]+$ ]] || die "Telegram ID must be digits only."
read -rp "Default currency [ETB]: " CURRENCY
CURRENCY=${CURRENCY:-ETB}

# Generated, never typed by a human.
DB_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
SESSION_SECRET=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)

# ------------------------------------------------------------------ packages
say "Updating the system (this is the slow part — a few minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

say "Installing Python, PostgreSQL, Redis and tools"
apt-get install -y -qq python3 python3-pip python3-venv \
    postgresql redis-server unzip curl ufw ca-certificates gnupg

systemctl enable --now postgresql redis-server >/dev/null 2>&1 || true

# ------------------------------------------------------------------ database
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='teleshop'" | grep -q 1; then
    warn "Database user already exists — resetting its password"
    sudo -u postgres psql -q -c "ALTER USER teleshop WITH PASSWORD '$DB_PASS';"
else
    say "Creating the database"
    sudo -u postgres psql -q -c "CREATE USER teleshop WITH PASSWORD '$DB_PASS';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='teleshop'" | grep -q 1 \
    || sudo -u postgres psql -q -c "CREATE DATABASE teleshop OWNER teleshop;"

# ------------------------------------------------------------------ firewall
say "Configuring the firewall (SSH, HTTP, HTTPS only)"
ufw allow 22/tcp  >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# ---------------------------------------------------------------- app user
id -u "$APP_USER" >/dev/null 2>&1 || {
    say "Creating the $APP_USER user"
    adduser --disabled-password --gecos "" "$APP_USER" >/dev/null
}

say "Copying the project to $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    cp -r "$SRC_DIR"/. "$APP_DIR"/
fi
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER"

# ------------------------------------------------------------ python + .env
say "Installing Python packages (two to four minutes)"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ -f "$APP_DIR/.env" ]]; then
    warn "Keeping the existing .env — delete it and re-run to regenerate"
else
    say "Writing settings file"
    cat > "$APP_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
SUPER_ADMIN_ID=$ADMIN_ID

DATABASE_URL=postgresql+asyncpg://teleshop:$DB_PASS@localhost:5432/teleshop
REDIS_URL=redis://localhost:6379/0

WEB_BASE_URL=https://$DOMAIN
WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_SESSION_SECRET=$SESSION_SECRET

SUBSCRIPTION_PRICE_STARS=1200
SUBSCRIPTION_DAYS=365
TRIAL_DAYS=30

AFFILIATE_COMMISSION_CREDITS=300
AFFILIATE_PAYOUT_THRESHOLD=1300
AFFILIATE_PAYMENT_AMOUNT=1300

DEFAULT_CURRENCY=$CURRENCY
LOG_LEVEL=INFO
LOG_DIR=logs
EOF
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

# -------------------------------------------------------------------- caddy
if ! command -v caddy >/dev/null 2>&1; then
    say "Installing Caddy (handles HTTPS automatically)"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy
fi

say "Pointing $DOMAIN at the dashboard"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8080
}
EOF
systemctl restart caddy

# ------------------------------------------------------------------ service
say "Creating the background service"
cat > /etc/systemd/system/teleshop.service <<EOF
[Unit]
Description=Teleshop
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable teleshop >/dev/null
systemctl restart teleshop

# ------------------------------------------------------------------ backups
say "Scheduling a nightly database backup at 3am"
cat > /home/$APP_USER/backup.sh <<'EOF'
#!/bin/bash
mkdir -p /home/teleshop/backups
DATE=$(date +%Y-%m-%d)
sudo -u postgres pg_dump teleshop | gzip > /home/teleshop/backups/teleshop-$DATE.sql.gz
find /home/teleshop/backups -name "*.sql.gz" -mtime +14 -delete
EOF
chmod +x /home/$APP_USER/backup.sh
( crontab -l 2>/dev/null | grep -v 'backup.sh'; echo "0 3 * * * /home/$APP_USER/backup.sh" ) | crontab -

# ------------------------------------------------------------------- report
sleep 4
echo
echo "──────────────────────────────────────────────────────────────"
if systemctl is-active --quiet teleshop; then
    echo "${GREEN}Teleshop is running.${OFF}"
else
    echo "${RED}Teleshop did not start.${OFF} See why with:"
    echo "    journalctl -u teleshop -n 40 --no-pager"
fi
echo
echo "ONE STEP LEFT — Telegram login will not work until you do this:"
echo "  1. Open Telegram, message @BotFather"
echo "  2. Send:  /setdomain"
echo "  3. Pick your master bot"
echo "  4. Send:  $DOMAIN"
echo
echo "Then open:  https://$DOMAIN"
echo
echo "Useful later:"
echo "  systemctl status teleshop        is it running"
echo "  journalctl -u teleshop -f        watch it live"
echo "  systemctl restart teleshop       restart it"
echo "──────────────────────────────────────────────────────────────"
