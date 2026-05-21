#!/usr/bin/env bash
# Install / update the ArtNet HTP merger on a Raspberry Pi (or any systemd
# Linux box). Idempotent: safe to re-run.
#
# Usage:   sudo bash deploy/install.sh [--source DIR]
# Default source is the directory containing this script's parent.
set -euo pipefail

INSTALL_DIR=/opt/artnet-htp
CONFIG_DIR=/etc/artnet-htp
LOG_DIR=/var/log/artnet-htp
USER=artnet
GROUP=artnet
SERVICE_NAME=artnet-htp

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (use sudo)" >&2
  exit 1
fi

SOURCE_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
if [[ -z "$SOURCE_DIR" ]]; then
  SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi

echo "==> installing from $SOURCE_DIR"

# Create user/group
if ! id "$USER" >/dev/null 2>&1; then
  echo "==> creating system user '$USER'"
  useradd --system --no-create-home --shell /usr/sbin/nologin --user-group "$USER"
fi

# Create dirs
echo "==> creating directories"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR"
chown -R "$USER:$GROUP" "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR"

# Copy sources (rsync excluding venvs and caches)
echo "==> copying sources"
rsync -a --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='*.pyc' \
  --exclude='.git' \
  "$SOURCE_DIR"/ "$INSTALL_DIR"/
chown -R "$USER:$GROUP" "$INSTALL_DIR"

# Create venv (idempotent)
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  echo "==> creating venv"
  sudo -u "$USER" python3 -m venv "$INSTALL_DIR/.venv"
fi

# Install deps. We deliberately skip `pip install --upgrade pip` — the system
# pip is fine, and attempting to upgrade it from pypi.org pollutes the log with
# SSL warnings on flaky networks (or fails entirely on offline production Pis).
echo "==> installing dependencies"
sudo -u "$USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

# Seed config if not present
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  echo "==> writing starter config at $CONFIG_DIR/config.yaml"
  cp "$INSTALL_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
  chown "$USER:$GROUP" "$CONFIG_DIR/config.yaml"
  echo "    edit it to set source IPs, output IPs, universes, then 'systemctl restart $SERVICE_NAME'"
else
  echo "==> config exists at $CONFIG_DIR/config.yaml (left untouched)"
fi

# Install systemd unit. Gated so we can run this same script inside a pi-gen
# chroot (where systemd isn't actually up): `systemctl enable` still creates
# the right symlinks because it's a static operation, but `daemon-reload`
# would fail. On a live Pi, both run normally.
echo "==> installing systemd unit"
install -m 644 "$SOURCE_DIR/deploy/artnet-htp.service" /etc/systemd/system/artnet-htp.service
if command -v systemctl >/dev/null; then
  if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
  else
    echo "    (no /run/systemd/system → skipping daemon-reload, likely in chroot)"
  fi
  systemctl enable "$SERVICE_NAME"
fi

echo
echo "==> done."
echo "Start:   sudo systemctl start $SERVICE_NAME"
echo "Status:  sudo systemctl status $SERVICE_NAME"
echo "Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "Web UI:  http://<this-host>:8080"
