#!/usr/bin/env bash
# First-boot config import for the ArtNet HTP merger.
#
# Invoked by artnet-htp-firstboot.service if and only if the operator dropped
# `artnet-htp-config.yaml` onto the SD card's boot partition. Validates the
# file, copies to /etc/artnet-htp/config.yaml on success, leaves an audit
# trail by renaming the source to `.applied`. On failure, writes a friendly
# error message that the web UI surfaces via /api/version.
set -euo pipefail

BOOT_DIR=/boot/firmware
SRC="${BOOT_DIR}/artnet-htp-config.yaml"
APPLIED="${BOOT_DIR}/artnet-htp-config.yaml.applied"
ERR_BOOT="${BOOT_DIR}/artnet-htp-config.yaml.error"

DEST_DIR=/etc/artnet-htp
DEST="${DEST_DIR}/config.yaml"
DEST_ERR="${DEST_DIR}/firstboot-error.txt"

ARTNET_BIN=/opt/artnet-htp/.venv/bin/artnet-htp

log() { echo "[firstboot] $*"; }

# Defensive: shouldn't happen because the service has ConditionPathExists,
# but guard anyway in case someone runs the script by hand.
if [ ! -f "$SRC" ]; then
  log "no $SRC — nothing to do"
  exit 0
fi

# Make sure the destination dir exists (it should, but harmless to ensure).
install -d -o artnet -g artnet -m 0750 "$DEST_DIR"

# Clear any prior error before we attempt this run.
rm -f "$DEST_ERR" "$ERR_BOOT"

# Validate. --validate-config exits non-zero with a human message on failure.
log "validating $SRC..."
if ! ERR_OUT=$("$ARTNET_BIN" --validate-config "$SRC" 2>&1); then
  log "VALIDATION FAILED:"
  echo "$ERR_OUT" | sed 's/^/[firstboot]   /'
  # Make the error visible in two places:
  #   1. /etc/artnet-htp/firstboot-error.txt — picked up by /api/version, so
  #      the operator sees it in the web UI.
  #   2. <boot>/artnet-htp-config.yaml.error — visible to anyone who pulls
  #      the SD card and reads the boot partition on their laptop.
  printf '%s\n' "$ERR_OUT" > "$DEST_ERR"
  printf '%s\n' "$ERR_OUT" > "$ERR_BOOT"
  log "wrote $DEST_ERR and $ERR_BOOT — leaving existing config untouched"
  # Exit 0 so the unit "succeeds" and we don't block boot. The error is
  # surfaced through the UI; failing the unit just makes journal noisier.
  exit 0
fi

log "validation passed"

# Copy into place with the right ownership/perms.
install -m 0640 -o artnet -g artnet "$SRC" "$DEST"
log "wrote $DEST"

# Rename the source on the boot partition so this script doesn't re-run on
# every boot (ConditionPathExists on the original filename is now false).
# Operators who pull the SD card later see the file is still there as
# `.applied` — useful audit trail when debugging at the venue.
mv "$SRC" "$APPLIED"
log "renamed $SRC -> $APPLIED"

log "first-boot config import complete"
