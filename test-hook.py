#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests the click lock, device sections and profile restore in the compiled shim,
without a touchpad or root.

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
#include <stdio.h>
struct libevdev; struct libinput_device; struct libinput_config_accel;
static struct input_event queued; static int queued_rc, clickpad = 1;
static char name[128] = "fake";
static double steps[3], first[3];
static size_t counts[3];
static int applied, profile = 2, set_profile_calls;
void fake_queue(struct input_event e, int rc) { queued = e; queued_rc = rc; }
void fake_clickpad(int v) { clickpad = v; }
void fake_name(const char *n) { snprintf(name, sizeof name, "%%s", n); }
void fake_profile(int p) { profile = p; }
int fake_get_profile(void) { return profile; }
int fake_set_profile_calls(void) { return set_profile_calls; }
void fake_reset(void) {
  applied = 0; set_profile_calls = 0;
  for (int i = 0; i < 3; i++) { steps[i] = first[i] = 0; counts[i] = 0; } }
int fake_applied(void) { return applied; }
double fake_step(int t) { return steps[t]; }
double fake_first(int t) { return first[t]; }
size_t fake_count(int t) { return counts[t]; }
const char *libevdev_get_name(const struct libevdev *d) { (void)d; return name; }
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
  (void)d; profile = p; set_profile_calls++; return 0; }
int libinput_device_config_accel_get_profile(struct libinput_device *d) {
  (void)d; return profile; }
int libinput_device_config_accel_get_default_profile(struct libinput_device *d) {
  (void)d; return 2; }
double libinput_device_config_accel_get_speed(struct libinput_device *d) {
  (void)d; return 0.4; }
int libinput_device_config_tap_get_finger_count(struct libinput_device *d) {
  (void)d; return 1; }
const char *libinput_device_get_name(struct libinput_device *d) {
  (void)d; return name; }
struct libinput_config_accel *libinput_config_accel_create(int p) {
  (void)p; return (struct libinput_config_accel *)1; }
void libinput_config_accel_destroy(struct libinput_config_accel *a) { (void)a; }
int libinput_config_accel_set_points(struct libinput_config_accel *a, int t,
    double s, size_t n, double *v) {
  (void)a; steps[t] = s; counts[t] = n; first[t] = v[1]; return 0; }
int libinput_device_config_accel_apply(struct libinput_device *d,
    struct libinput_config_accel *a) { (void)d; (void)a; applied++; profile = 4; return 0; }
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
        self.shim = shim
        self.fake = fake
        self.fake.fake_queue.argtypes = [Event, ctypes.c_int]
        self.fake.fake_name.argtypes = [ctypes.c_char_p]
        self.fake.fake_step.restype = ctypes.c_double
        self.fake.fake_first.restype = ctypes.c_double
        self.fake.fake_count.restype = ctypes.c_size_t
        self.conf_path = conf_path
        self.dev = 0x1000
        self.ms = 1000.0

    def write_conf(self, conf):
        with open(self.conf_path, "w") as f:
            f.write(conf)

    def configure(self, conf):
        self.write_conf(conf)
        self.dev += 16  # a new device resets the shim's touch state
        self.set_speed(1, 0.0)

    def configure_keep_device(self, conf):
        with open(self.conf_path, "w") as f:
            f.write(conf)
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


def run_sections(p, check):
    MOTION, SCROLL = 1, 2
    INTERNAL, EXTERNAL = b"PIXA3854:00 093A:0274 Touchpad", b"Apple Inc. Magic Trackpad"
    BASE = "motion_step 0.25\nmotion_points 0 0.1 0.25\n"

    def curve(conf, name):
        """Configures a device; returns (step, first point, count) or None if not applied."""
        p.fake.fake_name(name)
        p.fake.fake_reset()
        p.configure(conf)
        if not p.fake.fake_applied():
            return None
        return (p.fake.fake_step(MOTION), p.fake.fake_first(MOTION), p.fake.fake_count(MOTION))

    conf = BASE + '[device "Magic Trackpad"]\nmotion_step 0.49\n'
    check("section: other device keeps base curve", curve(conf, INTERNAL) == (0.25, 0.1, 3))
    check("section: matching device overrides, inherits points",
          curve(conf, EXTERNAL) == (0.49, 0.1, 3))

    conf = BASE + '[device "Magic Trackpad"] # comment\nmotion_points 0 0.2\n'
    check("section: points override, inherits step, comment allowed",
          curve(conf, EXTERNAL) == (0.25, 0.2, 2))

    conf = BASE + '[device "Trackpad"]\nmotion_step 0.3\n[device "Magic"]\nmotion_step 0.4\n'
    check("section: several matches, later one wins", curve(conf, EXTERNAL)[0] == 0.4)

    conf = BASE + '[device "magic trackpad"]\nmotion_step 0.49\n'
    check("section: match is case-sensitive", curve(conf, EXTERNAL)[0] == 0.25)

    conf = BASE + '[device "Pad #1"]\nmotion_step 0.49\n'
    check("section: name may contain #", curve(conf, b"My Pad #1")[0] == 0.49)
    check("section: name with # does not match others", curve(conf, EXTERNAL)[0] == 0.25)

    conf = BASE + 'scroll_step 2\nscroll_points 0 1\n[device "Magic"]\nscroll_step 3\n'
    curve(conf, EXTERNAL)
    check("section: scroll override", p.fake.fake_step(SCROLL) == 3)
    curve(conf, INTERNAL)
    check("section: scroll base for other device", p.fake.fake_step(SCROLL) == 2)

    for label, bad in [("typo in another device's section", '[device "Magic"]\nmotion_step abc\n'),
                       ("unknown key in a section", '[device "Magic"]\nfoo 1\n'),
                       ("no closing bracket", '[device "Magic"\n'),
                       ("empty name", '[device ""]\n'),
                       ("unknown section type", '[mouse "Magic"]\n'),
                       ("junk after section", '[device "Magic"] x\n')]:
        check(f"section invalid, config dropped: {label}", curve(BASE + bad, INTERNAL) is None)

    check("section: full curve only in section, other device passes through",
          curve('[device "Magic"]\n' + BASE, INTERNAL) is None)
    check("section: full curve only in section, matching device works",
          curve('[device "Magic"]\n' + BASE, EXTERNAL) == (0.25, 0.1, 3))

    mm = lambda v: int(v * RES)
    conf = CONF_ON + '[device "Magic Trackpad"]\nclick_lock_mm 0\nrelease_lock_ms 0\n'
    curve(conf, INTERNAL)
    p.down(100, 100, track=20)
    p.button(1)
    check("section: click lock active for base device",
          p.move(100 + mm(0.2), 100) == (100, 100))
    p.button(0)
    p.up()
    curve(conf, EXTERNAL)
    p.down(100, 100, track=21)
    p.button(1)
    check("section: click lock off for matching device",
          p.move(100 + mm(0.2), 100) == (100 + mm(0.2), 100))
    p.button(0)
    p.up()

    conf = CONF_OFF + '[device "Magic Trackpad"]\nclick_lock_mm 0.5\n'
    curve(conf, EXTERNAL)
    p.down(100, 100, track=22)
    p.button(1)
    check("section: click lock only for matching device",
          p.move(100 + mm(0.2), 100) == (100, 100))
    # Same open device, only a reload: the held finger must stay held.
    p.configure_keep_device(conf)
    check("reload: held finger stays held", p.move(100 + mm(0.3), 100) == (100, 100))
    p.configure_keep_device(CONF_OFF)
    check("reload: lock follows config", p.move(100 + mm(0.4), 100) == (100 + mm(0.4), 100))
    p.button(0)
    p.up()

    p.fake.fake_name(b"fake")
    p.configure(CONF_ON)


def run_restore(p, check):
    FLAT, ADAPTIVE, CUSTOM = 1, 2, 4
    set_profile = p.shim.libinput_device_config_accel_set_profile
    set_profile.argtypes = [ctypes.c_void_p, ctypes.c_int]
    p.fake.fake_name(b"PIXA3854:00 093A:0274 Touchpad")

    p.fake.fake_reset()
    p.write_conf(CONF_OFF)
    set_profile(1, FLAT)  # the compositor asks for flat, the shim makes it custom
    check("restore: curve applied", p.fake.fake_get_profile() == CUSTOM)

    p.fake.fake_reset()
    os.remove(p.conf_path)
    p.set_speed(1, 0.0)  # reload.sh: only the speed changes
    check("restore: config missing -> requested profile back",
          p.fake.fake_get_profile() == FLAT and not p.fake.fake_applied())

    p.fake.fake_reset()
    p.set_speed(1, 0.0)
    check("restore: not custom -> profile left alone",
          p.fake.fake_get_profile() == FLAT and p.fake.fake_set_profile_calls() == 0)

    p.write_conf(CONF_OFF)
    p.set_speed(1, 0.0)
    check("restore: config back -> curve again", p.fake.fake_get_profile() == CUSTOM)

    p.write_conf("motion_step 1\nmotion_points 0 x\n")
    p.set_speed(1, 0.0)
    check("restore: invalid config -> requested profile back", p.fake.fake_get_profile() == FLAT)

    p.write_conf(CONF_OFF)
    p.set_speed(3, 0.0)  # another device, no profile requested for it
    os.remove(p.conf_path)
    p.set_speed(3, 0.0)
    check("restore: nothing requested -> libinput default",
          p.fake.fake_get_profile() == ADAPTIVE)
    p.write_conf(CONF_ON)


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
    run_sections(p, check)
    run_restore(p, check)
    for path in paths:
        check_recording(p, path, check)
    print(f"\nRESULT: {'FAILED: ' + str(len(failures)) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(child(sys.argv[2:]))
    sys.exit(main())
