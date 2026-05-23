#!/bin/bash -e
# Runs INSIDE the chroot (qemu-aarch64 emulation). Creates the artnet user,
# builds a venv, installs the package, sets up the systemd unit. We don't
# call deploy/install.sh from here because install.sh expects a live system
# (sudo, daemon-reload, etc.) — re-implementing the chroot path keeps things
# explicit. install.sh is what someone runs on a vanilla Pi from source.

ARTNET_USER=artnet
INSTALL_DIR=/opt/artnet-htp
CONFIG_DIR=/etc/artnet-htp
LOG_DIR=/var/log/artnet-htp
SERVICE_NAME=artnet-htp

# 1. System user — no shell, no home dir. Same as install.sh.
if ! id "$ARTNET_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin --user-group "$ARTNET_USER"
fi

# 2. Dirs + ownership.
install -d -o "$ARTNET_USER" -g "$ARTNET_USER" -m 0750 "$CONFIG_DIR" "$LOG_DIR"
chown -R "$ARTNET_USER:$ARTNET_USER" "$INSTALL_DIR"
# config.yaml + build-info.json were created by 00-run.sh as root. Fix owners.
[ -f "$CONFIG_DIR/config.yaml" ] && chown "$ARTNET_USER:$ARTNET_USER" "$CONFIG_DIR/config.yaml"
[ -f "$CONFIG_DIR/build-info.json" ] && chmod 0644 "$CONFIG_DIR/build-info.json"

# 3. Venv + pip install. runuser avoids the sudo dependency.
# --copies (not the default --symlinks) gives us a REAL python binary
# under .venv/bin/python — so the setcap below applies only to OUR
# python, not the system-wide /usr/bin/python3.11.
runuser -u "$ARTNET_USER" -- python3 -m venv --copies "$INSTALL_DIR/.venv"
# Non-editable install: package gets copied into site-packages.
# The chroot has full network (pi-gen plumbs DNS through) so pip can fetch
# pydantic-core, etc. arm64 wheels from pypi.
runuser -u "$ARTNET_USER" -- "$INSTALL_DIR/.venv/bin/pip" install --no-cache-dir "$INSTALL_DIR"

# 3a. Note: setcap on .venv/bin/python happens in 02-run.sh (host-side) —
# pi-gen drops CAP_SETFCAP from chroot scripts as a security measure, so
# the host has to apply the cap to the file inside the rootfs after this
# script finishes. See 02-run.sh for the rationale.

# 4. systemd unit.
install -m 644 "$INSTALL_DIR/deploy/artnet-htp.service" /etc/systemd/system/artnet-htp.service
systemctl enable "$SERVICE_NAME.service"

# 5. First-boot config import service. Loads operator-dropped
# /boot/firmware/artnet-htp-config.yaml before artnet-htp.service starts.
# Files were staged at /var/cache/artnet-htp-build/ by the host-side 00-run.sh.
if [ -f /var/cache/artnet-htp-build/firstboot.sh ]; then
  install -d /usr/local/lib/artnet-htp
  install -m 0755 /var/cache/artnet-htp-build/firstboot.sh \
    /usr/local/lib/artnet-htp/firstboot.sh
  install -m 0644 /var/cache/artnet-htp-build/artnet-htp-firstboot.service \
    /etc/systemd/system/artnet-htp-firstboot.service
  systemctl enable artnet-htp-firstboot.service
  rm -rf /var/cache/artnet-htp-build
fi

# 6. Sudoers entry for the network UI (v0.3.0+). Grants the artnet user
# passwordless sudo for ONLY nmcli (Network section apply) and /sbin/reboot
# (apply triggers a reboot). Without this the Pi can't accept network
# changes via the web UI. /etc/sudoers.d files must be 0440 + chown root
# or sudo refuses to load them.
cat > /etc/sudoers.d/artnet-nmcli <<'SUDO'
# Allow the merger service to apply network config + reboot.
artnet ALL=(root) NOPASSWD: /usr/bin/nmcli
artnet ALL=(root) NOPASSWD: /sbin/reboot
SUDO
chmod 0440 /etc/sudoers.d/artnet-nmcli
# Syntax-check the file so a typo here doesn't break sudo on the device.
visudo -c -f /etc/sudoers.d/artnet-nmcli

# 7. Verify the installed package can at least import + report its version.
runuser -u "$ARTNET_USER" -- "$INSTALL_DIR/.venv/bin/python" -c \
  "import artnet_htp; print('artnet_htp version:', artnet_htp.__version__)"

echo "==> stage-artnet-htp installed successfully"
