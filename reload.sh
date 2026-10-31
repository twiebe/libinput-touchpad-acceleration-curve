#!/bin/bash
# SPDX-License-Identifier: MIT
# Makes KWin re-apply the curve from /etc/touchpad-accel.conf (no sudo needed).
# Nudges the touchpad speed via D-Bus by 0.01 and back; each change goes through
# libinput_device_config_accel_set_speed in KWin and therefore through the shim.
set -euo pipefail
IFACE=org.kde.KWin.InputDevice

dev=""
for d in $(busctl --user tree org.kde.KWin | grep -o '/org/kde/KWin/InputDevice/event[0-9]*'); do
  name=$(busctl --user get-property org.kde.KWin "$d" $IFACE name 2>/dev/null || true)
  if [[ ${name,,} == *touchpad* ]]; then dev=$d; break; fi
done
[ -n "$dev" ] || { echo "No touchpad found in KWin"; exit 1; }

cur=$(busctl --user get-property org.kde.KWin "$dev" $IFACE pointerAcceleration | awk '{print $2}')
tmp=$(awk -v c="$cur" 'BEGIN { t = c + 0.01; if (t > 1) t = c - 0.01; printf "%.4f", t }')
echo "Touchpad: $dev ($name), speed $cur"

since=$(date '+%Y-%m-%d %H:%M:%S')
busctl --user set-property org.kde.KWin "$dev" $IFACE pointerAcceleration d "$tmp"
busctl --user set-property org.kde.KWin "$dev" $IFACE pointerAcceleration d "$cur"
sleep 0.5

lines=$( (journalctl -b --since "$since"; journalctl --user -b --since "$since") 2>/dev/null \
  | grep 'touchpad-accel:' | grep -v 'sudo' || true)
if [ -n "$lines" ]; then
  echo "$lines" | tail -1
else
  echo "No message from the shim - KWin did NOT re-apply the curve."
fi
