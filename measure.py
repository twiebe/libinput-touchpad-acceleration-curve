#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measures on the real touchpad how finger speed is translated into pointer motion.

Opens the touchpad in a separate libinput context (KWin is not affected), sets the
profile the way KWin does, and records movement for a few seconds.

If the shim is installed via /etc/ld.so.preload, it is active here as well. Then:
  Custom curve:      sudo python3 -I measure.py [seconds]
  Profile adaptive:  temporarily rename the config (the shim then just passes through):
      sudo mv /etc/touchpad-accel.conf /etc/touchpad-accel.conf.off
      sudo python3 -I measure.py
      sudo mv /etc/touchpad-accel.conf.off /etc/touchpad-accel.conf
Shim not installed: sudo env LD_PRELOAD=$PWD/touchpad-accel.so python3 -I measure.py

During the measurement: first move very slowly/precisely for a few seconds,
then at normal speed, then a few fast swipes.
"""
import ctypes
import fcntl
import glob
import math
import os
import select
import struct
import sys
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
EV_POINTER_MOTION = 400
PROFILES = {0: "none", 1: "flat", 2: "adaptive", 4: "custom"}
# Speed classes in mm/s of finger speed
BINS = [0, 5, 10, 20, 40, 80, 160, 320, 640, math.inf]


def find_touchpad():
    for name_file in sorted(glob.glob("/sys/class/input/event*/device/name")):
        with open(name_file) as f:
            name = f.read().strip()
        if "touchpad" in name.lower():
            return "/dev/input/" + name_file.split("/")[4], name
    sys.exit("No touchpad found")


def x_resolution(path):
    """X axis resolution in units/mm (EVIOCGABS(ABS_X))."""
    buf = bytearray(24)
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.ioctl(fd, 0x80184540, buf)
    finally:
        os.close(fd)
    return struct.unpack("6i", bytes(buf))[5]


def summarize(samples, res):
    """samples: list of (raw_units, dt_ms, out_px). Prints a table per speed class."""
    rows = []
    for lo, hi in zip(BINS, BINS[1:]):
        sel = [(r, dt, o) for r, dt, o in samples if lo <= r / res / dt * 1000 < hi]
        if not sel:
            continue
        raw = sum(r for r, _, _ in sel)
        out = sum(o for _, _, o in sel)
        dead = sum(1 for r, _, o in sel if o < 0.05 * r)
        in_speed = sum(r / dt for r, dt, _ in sel) / len(sel)  # raw units/ms (like libinput)
        rows.append((lo, hi, len(sel), in_speed, out / raw, out / raw * res, dead))
    print(f"{'Finger mm/s':>12} {'Events':>7} {'in u/ms':>8} {'px/unit':>8} {'px/mm':>7} {'~0-out':>7}")
    for lo, hi, n, ins, g, gmm, dead in rows:
        label = f"{lo:g}-{hi:g}" if hi != math.inf else f">{lo:g}"
        print(f"{label:>12} {n:>7} {ins:>8.2f} {g:>8.3f} {gmm:>7.2f} {dead:>7}")


def main():
    ctypes.CDLL("libinput.so.10", mode=ctypes.RTLD_GLOBAL)
    g = ctypes.CDLL(None)  # global namespace -> the preloaded shim takes effect

    OPEN_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    CLOSE_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)

    class Interface(ctypes.Structure):
        _fields_ = [("open_restricted", OPEN_CB), ("close_restricted", CLOSE_CB)]

    def _open(p, flags, _):
        try:
            return os.open(p, flags)
        except OSError as e:
            return -e.errno

    def _close(fd, _):
        os.close(fd)

    iface = Interface(OPEN_CB(_open), CLOSE_CB(_close))
    vp, dbl = ctypes.c_void_p, ctypes.c_double
    sig = {
        "libinput_path_create_context": (vp, [ctypes.POINTER(Interface), vp]),
        "libinput_path_add_device": (vp, [vp, ctypes.c_char_p]),
        "libinput_unref": (vp, [vp]),
        "libinput_get_fd": (ctypes.c_int, [vp]),
        "libinput_dispatch": (ctypes.c_int, [vp]),
        "libinput_get_event": (vp, [vp]),
        "libinput_event_get_type": (ctypes.c_int, [vp]),
        "libinput_event_get_pointer_event": (vp, [vp]),
        "libinput_event_pointer_get_dx": (dbl, [vp]),
        "libinput_event_pointer_get_dy": (dbl, [vp]),
        "libinput_event_pointer_get_dx_unaccelerated": (dbl, [vp]),
        "libinput_event_pointer_get_dy_unaccelerated": (dbl, [vp]),
        "libinput_event_pointer_get_time_usec": (ctypes.c_uint64, [vp]),
        "libinput_event_destroy": (None, [vp]),
        "libinput_device_config_accel_get_profile": (ctypes.c_int, [vp]),
        "libinput_device_config_accel_set_profile": (ctypes.c_int, [vp, ctypes.c_int]),
        "libinput_device_config_accel_set_speed": (ctypes.c_int, [vp, dbl]),
    }
    for name, (res_t, args) in sig.items():
        fn = getattr(g, name)
        fn.restype, fn.argtypes = res_t, args

    path, name = find_touchpad()
    res = x_resolution(path)
    print(f"Touchpad: {name} ({path}), X resolution: {res} units/mm")
    if res <= 0:
        sys.exit("Touchpad reports no resolution")

    li = g.libinput_path_create_context(ctypes.byref(iface), None)
    dev = g.libinput_path_add_device(li, path.encode()) if li else None
    if not dev:
        sys.exit("Could not open device (did you run with sudo?)")
    g.libinput_device_config_accel_set_profile(dev, 2)  # like KWin
    g.libinput_device_config_accel_set_speed(dev, 0.4)  # only relevant for adaptive
    prof = g.libinput_device_config_accel_get_profile(dev)
    print(f"Profile: {PROFILES.get(prof, prof)}  (speed 0.4, only relevant for adaptive)")
    print(f"\nMeasuring for {DURATION:g} s: first slow/precise, then normal, then fast ...")

    fd = g.libinput_get_fd(li)
    samples, last_t = [], None
    end = time.monotonic() + DURATION
    while time.monotonic() < end:
        select.select([fd], [], [], 0.2)
        g.libinput_dispatch(li)
        while ev := g.libinput_get_event(li):
            if g.libinput_event_get_type(ev) == EV_POINTER_MOTION:
                pe = g.libinput_event_get_pointer_event(ev)
                t = g.libinput_event_pointer_get_time_usec(pe)
                raw = math.hypot(g.libinput_event_pointer_get_dx_unaccelerated(pe),
                                 g.libinput_event_pointer_get_dy_unaccelerated(pe))
                out = math.hypot(g.libinput_event_pointer_get_dx(pe),
                                 g.libinput_event_pointer_get_dy(pe))
                # Time delta like libinput: pause > 1 s -> assume 7 ms
                dt = (t - last_t) / 1000 if last_t is not None else 7.0
                if dt <= 0 or dt > 1000:
                    dt = 7.0
                last_t = t
                if raw > 0:
                    samples.append((raw, dt, out))
            g.libinput_event_destroy(ev)
    g.libinput_unref(li)

    print(f"\n{len(samples)} motion events.\n")
    if samples:
        summarize(samples, res)
        print("\npx/mm = pointer travel in pixels per mm of finger travel; ~0-out = events "
              "that produced almost no pointer motion")


if __name__ == "__main__":
    main()
