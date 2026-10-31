#!/bin/bash
# SPDX-License-Identifier: MIT
# Removes the touchpad shim. Usage: sudo ./uninstall.sh
set -euo pipefail
if [ -f /etc/ld.so.preload ]; then
  sed -i -e '\#^/usr/local/\$LIB/touchpad-accel\.so$#d' \
         -e '\#^/usr/local/lib64/touchpad-accel\.so$#d' /etc/ld.so.preload
  [ -s /etc/ld.so.preload ] || rm /etc/ld.so.preload
fi
# all library directories install.sh may have used (see there)
for d in lib64 lib lib32 lib/x86_64-linux-gnu lib/i386-linux-gnu; do
  rm -f "/usr/local/$d/touchpad-accel.so"
done
echo "Removed. /etc/touchpad-accel.conf was kept (sudo rm it if you like)."
echo "Log out and back in."
