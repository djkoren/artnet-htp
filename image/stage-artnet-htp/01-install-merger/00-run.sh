#!/bin/bash -e
# Host-side step (NOT inside chroot). Copy our source tree and any baked
# metadata into the target rootfs so the chroot stage can install them.

# Source tree the workflow rsync'd in earlier:
#   pi-gen/stage-artnet-htp/files/artnet-htp-src/  → /opt/artnet-htp/

install -d "${ROOTFS_DIR}/opt/artnet-htp"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.pytest_cache' --exclude='.venv' \
  "${BASE_DIR}/stage-artnet-htp/files/artnet-htp-src/" \
  "${ROOTFS_DIR}/opt/artnet-htp/"

# Stage the firstboot service+script in /var/cache/artnet-htp-build/ so the
# chroot stage can install them. The cache dir is cleaned up by 01-run-chroot.sh.
if [ -d "${BASE_DIR}/stage-artnet-htp/files/firstboot" ]; then
  install -d "${ROOTFS_DIR}/var/cache/artnet-htp-build"
  install -m 0755 "${BASE_DIR}/stage-artnet-htp/files/firstboot/firstboot.sh" \
    "${ROOTFS_DIR}/var/cache/artnet-htp-build/firstboot.sh"
  install -m 0644 "${BASE_DIR}/stage-artnet-htp/files/firstboot/artnet-htp-firstboot.service" \
    "${ROOTFS_DIR}/var/cache/artnet-htp-build/artnet-htp-firstboot.service"
fi

# Bake build-info.json into /etc/artnet-htp so the /api/version endpoint can
# surface the CI build's git_sha and built_at. The workflow generates this
# file just before calling pi-gen.
if [ -f "${BASE_DIR}/stage-artnet-htp/files/build-info.json" ]; then
  install -d "${ROOTFS_DIR}/etc/artnet-htp"
  install -m 644 "${BASE_DIR}/stage-artnet-htp/files/build-info.json" \
    "${ROOTFS_DIR}/etc/artnet-htp/build-info.json"
fi

# Seed a minimal default config. The first-boot service will override this
# with an operator-dropped config from the boot partition if present.
install -d "${ROOTFS_DIR}/etc/artnet-htp"
install -m 644 \
  "${BASE_DIR}/stage-artnet-htp/files/default-config.yaml" \
  "${ROOTFS_DIR}/etc/artnet-htp/config.yaml"
