#!/bin/bash
# SPDX-License-Identifier: MIT
# Installs the touchpad shim (64 + 32 bit). Usage: make && sudo ./install.sh
#
# /etc/ld.so.preload gets the line /usr/local/$LIB/touchpad-accel.so. glibc expands
# $LIB per process to its library directory, which differs between distributions:
#   Fedora:        lib64 (64-bit)                 lib (32-bit)
#   Debian/Ubuntu: lib/x86_64-linux-gnu (64-bit)  lib32 or lib/i386-linux-gnu (32-bit)
#   Arch:          lib (64-bit)                   lib32 (32-bit)
# We derive the directories from where the system's libc lives, and verify after
# installing that a newly started 64-bit process really loads the shim.
set -euo pipefail
cd "$(dirname "$0")"
for f in touchpad-accel.so touchpad-accel-32.so; do
  [ -f "$f" ] || { echo "$f missing - run 'make' first"; exit 1; }
done
LINE='/usr/local/$LIB/touchpad-accel.so'

lib_dir_64() {
  local libc dir
  libc=$(ldd /bin/sh 2>/dev/null | awk '/libc\.so\.6/ {print $3; exit}')
  [ -n "$libc" ] || return 1
  dir=$(cd "$(dirname "$libc")" && pwd -P)
  dir=${dir#/usr}
  echo "${dir#/}"
}
is_elf32() { [ "$(od -An -tx1 -j4 -N1 "$(readlink -f "$1")" | tr -d ' \n')" = 01 ]; }
lib_dirs_32() {
  local d
  for d in lib lib32 lib/i386-linux-gnu; do
    if [ -e "/usr/$d/libc.so.6" ] && is_elf32 "/usr/$d/libc.so.6"; then echo "$d"; fi
  done
}

DIR64=$(lib_dir_64) || { echo "Could not determine the 64-bit library directory"; exit 1; }
DIRS32=$(lib_dirs_32)
echo "64-bit library directory: $DIR64"
echo "32-bit library directories: ${DIRS32:-none (no 32-bit libc installed)}"

# Write the new file under a temporary name and rename it atomically: running
# processes (KWin) keep their old copy, newly started ones always find a file.
put() {
  install -Dm755 "$1" "$2.new"
  mv -f "$2.new" "$2"
  restorecon "$2" 2>/dev/null || true
}
put touchpad-accel.so "/usr/local/$DIR64/touchpad-accel.so"
for d in $DIRS32; do
  put touchpad-accel-32.so "/usr/local/$d/touchpad-accel.so"
done
if [ ! -f /etc/touchpad-accel.conf ]; then
  install -m644 touchpad-accel.conf /etc/touchpad-accel.conf
  restorecon /etc/touchpad-accel.conf 2>/dev/null || true
fi

touch /etc/ld.so.preload
# remove the old lib64-only line (early version), add the current one exactly once
sed -i '\#^/usr/local/lib64/touchpad-accel\.so$#d' /etc/ld.so.preload
grep -qxF "$LINE" /etc/ld.so.preload || echo "$LINE" >> /etc/ld.so.preload
restorecon /etc/ld.so.preload 2>/dev/null || true

# Self-check: a newly started 64-bit process (grep itself, reading its own memory
# map) must have the shim loaded. No pipe here: with pipefail, an early-exiting
# grep -q could make a pipeline fail even on a match.
if ! grep -q 'touchpad-accel\.so' /proc/self/maps; then
  sed -i '\#^/usr/local/\$LIB/touchpad-accel\.so$#d' /etc/ld.so.preload
  [ -s /etc/ld.so.preload ] || rm -f /etc/ld.so.preload
  echo "ERROR: a new process did not load /usr/local/$DIR64/touchpad-accel.so."
  echo "The preload entry was removed again; nothing is active."
  exit 1
fi

echo "OK. /etc/ld.so.preload:"; cat /etc/ld.so.preload
echo "Now test (sudo python3 -I test-real.py), then log out and back in."
