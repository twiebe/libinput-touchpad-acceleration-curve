#!/bin/bash
# SPDX-License-Identifier: MIT
# Makes the compositor re-apply the curve from /etc/touchpad-accel.conf (no sudo needed).
# Nudges the touchpad speed by 0.01 and back; each change goes through
# libinput_device_config_accel_set_speed in the compositor and therefore through the shim.
set -euo pipefail

[ "$EUID" -ne 0 ] || { echo "Run this without sudo: it talks to your desktop session."; exit 1; }

reload_kwin() {
  local IFACE=org.kde.KWin.InputDevice dev name cur tmp found=""
  for dev in $(busctl --user tree org.kde.KWin | grep -o '/org/kde/KWin/InputDevice/event[0-9]*'); do
    name=$(busctl --user get-property org.kde.KWin "$dev" $IFACE name 2>/dev/null || true)
    [[ ${name,,} == *touchpad* || ${name,,} == *trackpad* ]] || continue
    # A device without pointer acceleration (e.g. a second node of the same hardware) is skipped.
    cur=$(busctl --user get-property org.kde.KWin "$dev" $IFACE pointerAcceleration 2>/dev/null \
      | awk '{print $2}' || true)
    [ -n "$cur" ] || continue
    tmp=$(nudged "$cur")
    echo "Touchpad: $dev ($name), speed $cur"
    busctl --user set-property org.kde.KWin "$dev" $IFACE pointerAcceleration d "$tmp" || continue
    busctl --user set-property org.kde.KWin "$dev" $IFACE pointerAcceleration d "$cur"
    found=1
  done
  [ -n "$found" ] || { echo "No touchpad found in KWin"; exit 1; }
}

# GNOME has one speed setting, which Mutter applies to all touchpads.
reload_gnome() {
  local schema=org.gnome.desktop.peripherals.touchpad cur tmp
  cur=$(gsettings get $schema speed)
  tmp=$(nudged "$cur")
  echo "GNOME touchpad speed: $cur"
  gsettings set $schema speed "$tmp"
  sleep 0.3
  gsettings set $schema speed "$cur"
}

nudged() {
  LC_ALL=C awk -v c="$1" 'BEGIN { t = c + 0.01; if (t > 1) t = c - 0.01; printf "%.4f", t }'
}

since=$(date '+%Y-%m-%d %H:%M:%S')
services=$(busctl --user list --no-legend 2>/dev/null || true)
if grep -q '^org\.gnome\.Mutter\.DisplayConfig ' <<<"$services"; then
  reload_gnome
elif grep -q '^org\.kde\.KWin ' <<<"$services"; then
  reload_kwin
else
  echo "Neither GNOME Shell nor KWin found in this session"
  exit 1
fi
sleep 0.5

lines=$( (journalctl -b --since "$since"; journalctl --user -b --since "$since") 2>/dev/null \
  | grep 'touchpad-accel:' | grep -v 'sudo' || true)
if [ -n "$lines" ]; then
  echo "$lines"
else
  echo "No message from the shim - the compositor did NOT re-apply the curve."
fi
