#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests the shim against the system's REAL libinput without risking the compositor
or the login.

Opens the touchpad in a separate, private libinput context (does not disturb the
compositor), calls set_profile/set_speed the way a compositor does, and checks
whether the custom profile is active afterwards. A crash here means the compositor
would have crashed too.

Usage (shim installed via /etc/ld.so.preload):
  sudo python3 -I test-real.py
Shim not installed yet (run in the directory containing the .so files):
  sudo env LD_PRELOAD=$PWD/touchpad-accel.so python3 -I test-real.py
Another device than the first touchpad, e.g. an external one:
  sudo python3 -I test-real.py /dev/input/event19
"""
import ctypes
import glob
import locale
import os
import sys

# Like the compositor: adopt the user's locale (e.g. de_DE uses a decimal comma).
# This also verifies that the shim parses numbers independently of it.
print("Locale:", locale.setlocale(locale.LC_ALL, ""))

PROFILES = {0: "none", 1: "flat", 2: "adaptive", 4: "custom"}


def find_touchpad():
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(f"/sys/class/input/{os.path.basename(path)}/device/name") as f:
            return path, f.read().strip()
    for name_file in sorted(glob.glob("/sys/class/input/event*/device/name")):
        with open(name_file) as f:
            name = f.read().strip()
        if "touchpad" in name.lower():
            return "/dev/input/" + name_file.split("/")[4], name
    sys.exit("No touchpad found")


# Resolve functions through the global namespace (the way the compositor calls them),
# so the preloaded shim takes effect.
ctypes.CDLL("libinput.so.10", mode=ctypes.RTLD_GLOBAL)
g = ctypes.CDLL(None)

OPEN_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
CLOSE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)


class Interface(ctypes.Structure):
    _fields_ = [("open_restricted", OPEN_CB), ("close_restricted", CLOSE_CB)]


def _open(path, flags, _):
    try:
        return os.open(path, flags)
    except OSError as e:
        return -e.errno


def _close(fd, _):
    os.close(fd)


iface = Interface(OPEN_CB(_open), CLOSE_CB(_close))

g.libinput_path_create_context.restype = ctypes.c_void_p
g.libinput_path_create_context.argtypes = [ctypes.POINTER(Interface), ctypes.c_void_p]
g.libinput_path_add_device.restype = ctypes.c_void_p
g.libinput_path_add_device.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
g.libinput_unref.argtypes = [ctypes.c_void_p]
g.libinput_device_config_accel_get_profile.argtypes = [ctypes.c_void_p]
g.libinput_device_config_accel_set_profile.argtypes = [ctypes.c_void_p, ctypes.c_int]
g.libinput_device_config_accel_set_speed.argtypes = [ctypes.c_void_p, ctypes.c_double]

# Is the shim active? Regardless of whether it came via LD_PRELOAD or /etc/ld.so.preload.
with open("/proc/self/maps") as f:
    preload = any("touchpad-accel" in line for line in f)
print("Shim loaded:", "yes" if preload else "no (baseline run only)")
# The click lock hooks libinput's calls into libevdev, so it needs a shared libevdev.
with open("/proc/self/maps") as f:
    shared_evdev = any("libevdev" in line for line in f)
print("Shared libevdev:", "yes" if shared_evdev else "NO - click lock cannot work")

path, name = find_touchpad()
print(f"Touchpad: {name} ({path})")

li = g.libinput_path_create_context(ctypes.byref(iface), None)
if not li:
    sys.exit("Could not create libinput context")
dev = g.libinput_path_add_device(li, path.encode())
if not dev:
    g.libinput_unref(li)
    sys.exit("Could not open device (did you run with sudo?)")

prof = g.libinput_device_config_accel_get_profile
print("Profile before:     ", PROFILES.get(prof(dev), prof(dev)))
rc1 = g.libinput_device_config_accel_set_profile(dev, 2)  # like a compositor: adaptive
print(f"set_profile(adaptive) -> rc={rc1}, profile now: {PROFILES.get(prof(dev), prof(dev))}")
rc2 = g.libinput_device_config_accel_set_speed(dev, 0.4)  # like a compositor
final = prof(dev)
print(f"set_speed(0.4)        -> rc={rc2}, profile now: {PROFILES.get(final, final)}")
g.libinput_unref(li)

print()
if not preload:
    print("RESULT: baseline run without shim done (expected: profile adaptive).")
elif final == 4:
    print("RESULT: OK - no crash, custom profile active.")
else:
    print("RESULT: no crash, but custom profile NOT active "
          "(/etc/touchpad-accel.conf missing? See touchpad-accel lines above).")
