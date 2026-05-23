#!/bin/bash -e
# Host-side, runs AFTER 01-run-chroot.sh has built the venv inside the rootfs.
#
# Why setcap is here and not in the chroot script:
# pi-gen's on_chroot helper invokes `capsh --drop=cap_setfcap` before running
# scripts inside the chroot, which is a sandboxing measure so a stage can't
# escalate by setting file caps on arbitrary binaries. That ban includes
# legitimate setcap calls like ours — v0.3.2's first build attempt failed
# with "unable to set CAP_SETFCAP effective capability: Operation not
# permitted" inside the chroot.
#
# The host process running this script has full caps and can write to xattrs
# on files inside the rootfs directory at ${ROOTFS_DIR}. The cap survives
# the rootfs-to-ext4-image packaging because xattrs are stored on the inode
# and pi-gen's image-make step preserves them.
#
# What this grants: the merger's python binary can bind TCP/UDP ports below
# 1024 (we use it for port 80). Without this we'd need either AmbientCapabilities
# in the systemd unit (which implicitly sets no_new_privs and breaks
# `sudo nmcli` from the Network panel) or run the service as root.

PY="${ROOTFS_DIR}/opt/artnet-htp/.venv/bin/python"

if [ ! -f "$PY" ]; then
  echo "ERROR: expected $PY to exist (created by 01-run-chroot.sh)"
  exit 1
fi

setcap cap_net_bind_service=+ep "$PY"
echo "==> setcap applied:"
getcap "$PY"
