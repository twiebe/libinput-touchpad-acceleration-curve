#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests the click lock in the compiled shim, without a touchpad or root.

Builds the shim with a private config path and loads it in front of a fake
libinput/libevdev, then feeds events through the shim's libevdev_next_event.

Optionally replays `libinput record` files and checks them too (no reported step
larger than the real one, which means no pointer jumps; no change when off).

Usage: python3 -I test-hook.py [recording.yml ...]
"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_LEFT = 272
SLOT, POS_X, POS_Y, TRACKING = 47, 53, 54, 57
RES = 24  # units/mm
MAX_X, MAX_Y = 2833, 1723
CONF_ON = "motion_step 1\nmotion_points 0 1\nclick_lock_mm 0.5\nrelease_lock_ms 120\n"
CONF_OFF = "motion_step 1\nmotion_points 0 1\n"

FAKE = r"""
#include <linux/input.h>
#include <stddef.h>
struct libevdev; struct libinput_device; struct libinput_config_accel;
static struct input_event queued; static int queued_rc, clickpad = 1;
void fake_queue(struct input_event e, int rc) { queued = e; queued_rc = rc; }
void fake_clickpad(int v) { clickpad = v; }
int libevdev_next_event(struct libevdev *d, unsigned f, struct input_event *e) {
  (void)d; (void)f; *e = queued; return queued_rc; }
int libevdev_has_property(const struct libevdev *d, unsigned p) {
  (void)d; return p == INPUT_PROP_BUTTONPAD && clickpad; }
int libevdev_get_current_slot(const struct libevdev *d) { (void)d; return 0; }
int libevdev_get_abs_resolution(const struct libevdev *d, unsigned c) {
  (void)d; (void)c; return %(res)d; }
int libevdev_get_abs_minimum(const struct libevdev *d, unsigned c) {
  (void)d; (void)c; return 0; }
int libevdev_get_abs_maximum(const struct libevdev *d, unsigned c) {
  (void)d; return c == ABS_MT_POSITION_X ? %(max_x)d : %(max_y)d; }
int libinput_device_config_accel_set_speed(struct libinput_device *d, double s) {
  (void)d; (void)s; return 0; }
int libinput_device_config_accel_set_profile(struct libinput_device *d, int p) {
  (void)d; (void)p; return 0; }
int libinput_device_config_tap_get_finger_count(struct libinput_device *d) {
  (void)d; return 1; }
const char *libinput_device_get_name(struct libinput_device *d) {
  (void)d; return "fake"; }
struct libinput_config_accel *libinput_config_accel_create(int p) {
  (void)p; return (struct libinput_config_accel *)1; }
void libinput_config_accel_destroy(struct libinput_config_accel *a) { (void)a; }
int libinput_config_accel_set_points(struct libinput_config_accel *a, int t,
    double s, size_t n, double *v) { (void)a; (void)t; (void)s; (void)n; (void)v; return 0; }
int libinput_device_config_accel_apply(struct libinput_device *d,
    struct libinput_config_accel *a) { (void)d; (void)a; return 0; }
""" % {"res": RES, "max_x": MAX_X, "max_y": MAX_Y}


class Event(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_long), ("usec", ctypes.c_long), ("type", ctypes.c_ushort),
                ("code", ctypes.c_ushort), ("value", ctypes.c_int)]


class Pad:
    """Sends events through the shim and returns what libinput would see."""

    def __init__(self, shim, fake, conf_path):
        self.next_event = shim.libevdev_next_event
        self.next_event.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(Event)]
        self.set_speed = shim.libinput_device_config_accel_set_speed
        self.set_speed.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self.fake = fake
        self.fake.fake_queue.argtypes = [Event, ctypes.c_int]
        self.conf_path = conf_path
        self.dev = 0x1000
        self.ms = 1000.0

    def configure(self, conf):
        with open(self.conf_path, "w") as f:
            f.write(conf)
        self.dev += 16  # a new device resets the shim's touch state
        self.set_speed(1, 0.0)

    def event(self, etype, code, value, rc=0, time=None):
        t = self.ms if time is None else time
        self.fake.fake_queue(Event(int(t // 1000), int((t % 1000) * 1000), etype, code, value), rc)
        out = Event()
        got = self.next_event(self.dev, 0, ctypes.byref(out))
        assert got == rc, f"return value changed: {got} != {rc}"
        return out.value

    def down(self, x, y, slot=0, track=1):
        self.event(EV_ABS, SLOT, slot)
        self.event(EV_ABS, TRACKING, track)
        self.move(x, y)

    def up(self, slot=0):
        self.event(EV_ABS, SLOT, slot)
        self.event(EV_ABS, TRACKING, -1)

    def move(self, x, y, slot=None):
        if slot is not None:
            self.event(EV_ABS, SLOT, slot)
        return self.event(EV_ABS, POS_X, x), self.event(EV_ABS, POS_Y, y)

    def button(self, value):
        self.event(EV_KEY, BTN_LEFT, value)


def run_synthetic(p, check):
    mm = lambda v: int(v * RES)

    p.configure(CONF_OFF)
    p.down(100, 100)
    p.button(1)
    check("off: position unchanged", p.move(100 + mm(3), 100) == (100 + mm(3), 100))
    p.button(0)
    check("off: unchanged after release", p.move(100, 100) == (100, 100))
    p.up()

    p.configure(CONF_ON)
    p.down(500, 500)
    check("before press: free", p.move(510, 500) == (510, 500))
    p.button(1)
    check("press: small roll held", p.move(510 + mm(0.4), 500) == (510, 500))
    check("press: past lock, no jump", p.move(510 + mm(0.6), 500) == (510 + mm(0.6) - mm(0.4), 500))
    check("press: then free with offset",
          p.move(510 + mm(5), 500) == (510 + mm(5) - mm(0.4), 500))
    p.up()
    p.button(0)

    p.ms += 1000
    p.down(500, 500)
    check("new touch: offset gone", p.move(600, 600) == (600, 600))
    p.button(1)
    p.ms += 300
    p.button(0)
    p.ms += 119
    check("release: snap-back held within 120 ms", p.move(600 + mm(3), 600) == (600, 600))
    p.ms += 1
    check("release: free after 120 ms, press lock gone",
          p.move(601 + mm(3), 600) == (601, 600))
    p.up()

    p.ms += 1000
    p.down(100, 100, slot=0, track=2)
    p.button(1)
    p.down(1000, 1000, slot=1, track=3)
    check("finger landing after press: free", p.move(1000 + mm(2), 1000, slot=1) == (1000 + mm(2), 1000))
    check("pressing finger: held", p.move(100 + mm(0.2), 100, slot=0) == (100, 100))
    p.up(1)
    p.up(0)
    p.button(0)

    p.ms += 1000
    p.down(100, 100, track=4)
    p.button(1)
    p.ms += 10
    p.event(EV_ABS, POS_X, 100 + mm(0.4), rc=1)
    check("sync events: filtered too", p.event(EV_ABS, POS_X, 100 + mm(0.3), rc=1) == 100)
    check("errors: event not touched", p.event(EV_ABS, POS_X, 777, rc=-11) == 777)
    p.button(0)
    p.up()

    p.fake.fake_clickpad(0)
    p.configure(CONF_ON)
    p.down(100, 100, track=5)
    p.button(1)
    check("not a clickpad: unchanged", p.move(100 + mm(0.2), 100) == (100 + mm(0.2), 100))
    p.button(0)
    p.up()
    p.fake.fake_clickpad(1)

    p.configure("motion_step 1\nmotion_points 0 1\nclick_lock_mm 99\n")
    p.down(100, 100, track=6)
    p.button(1)
    check("invalid config: lock off", p.move(100 + mm(0.2), 100) == (100 + mm(0.2), 100))
    p.button(0)
    p.up()
    p.configure(CONF_ON)


def load_recording(path):
    line = re.compile(r"-\s*\[\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(-?\d+)\]")
    with open(path) as f:
        return [tuple(map(int, m.groups())) for m in map(line.search, f) if m]


def replay(p, events, conf):
    """Returns (touch, axis, real position, reported position) per position event."""
    p.configure(conf)
    slot = 0
    touch = {}
    rows = []
    for sec, usec, etype, code, value in events:
        got = p.event(etype, code, value, time=sec * 1000 + usec / 1000)
        if etype == EV_ABS and code == SLOT:
            slot = value
        elif etype == EV_ABS and code == TRACKING:
            touch[slot] = (slot, sec, usec)
        elif etype == EV_ABS and code in (POS_X, POS_Y):
            rows.append((touch.get(slot), code - POS_X, value, got))
    return rows


def check_recording(p, path, check):
    events = load_recording(path)
    name = os.path.basename(path)
    off = replay(p, events, CONF_OFF)
    check(f"{name}: unchanged when off", all(r[2] == r[3] for r in off))

    rows = replay(p, events, CONF_ON)
    last = {}
    worst = 0
    held = 0
    for touch, axis, real, got in rows:
        before = last.get((touch, axis))
        if before is not None:
            worst = max(worst, abs(got - before[1]) - abs(real - before[0]))
            held += got == before[1] and real != before[0]
        last[(touch, axis)] = (real, got)
    check(f"{name}: no reported step larger than the real one", worst <= 0)
    check(f"{name}: lock held some steps ({held})", held > 0)
    moved = sum(1 for r in rows if r[2] != r[3])
    print(f"      {name}: {len(rows)} position events, {held} steps held, "
          f"{moved} reported with an offset")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        fake_c = os.path.join(tmp, "fake.c")
        fake_so = os.path.join(tmp, "libfake.so")
        shim_so = os.path.join(tmp, "shim.so")
        conf = os.path.join(tmp, "conf")
        with open(fake_c, "w") as f:
            f.write(FAKE)
        cc = os.environ.get("CC", "cc")
        flags = ["-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC"]
        subprocess.check_call([cc, *flags, "-o", fake_so, fake_c])
        subprocess.check_call([cc, *flags, f'-DCONF_PATH="{conf}"', "-o", shim_so,
                               os.path.join(HERE, "touchpad-accel.c")])
        env = dict(os.environ, LD_PRELOAD=f"{shim_so}:{fake_so}", TEST_HOOK_CONF=conf)
        return subprocess.call([sys.executable, "-I", __file__, "--child", *sys.argv[1:]], env=env)


def child(paths):
    g = ctypes.CDLL(None)
    p = Pad(g, g, os.environ["TEST_HOOK_CONF"])
    failures = []

    def check(name, ok):
        print(("ok  " if ok else "FAIL"), name)
        if not ok:
            failures.append(name)

    run_synthetic(p, check)
    for path in paths:
        check_recording(p, path, check)
    print(f"\nRESULT: {'FAILED: ' + str(len(failures)) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(child(sys.argv[2:]))
    sys.exit(main())
