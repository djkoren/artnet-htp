#!/bin/bash -e
# Required by pi-gen: copies the previous stage's rootfs into this stage's
# working directory so our sub-stages have something to mutate.
#
# Without this, ROOTFS_DIR is empty and all chroot operations silently
# no-op against a missing rootfs, which is what burned v0.2.0/v0.2.1 —
# pi-gen exported stage2 unchanged instead of our customized rootfs.
#
# Mirrors the prerun.sh in pi-gen's stage1/stage2/etc.

if [ ! -d "${ROOTFS_DIR}" ]; then
  copy_previous
fi
