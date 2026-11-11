// SPDX-License-Identifier: MIT
//
// touchpad-accel: forces a libinput custom acceleration curve onto touchpads,
// even if the compositor (GNOME, KDE Plasma, ...) does not offer the custom
// profile.
//
// How it works: the library is loaded via /etc/ld.so.preload and intercepts
// libinput_device_config_accel_set_speed / _set_profile. Whenever the compositor
// calls these for a touchpad (at startup, on hotplug, when the speed slider changes),
// the curve from CONF_PATH is applied afterwards. If the file is missing or
// invalid, calls are only passed through (the reason is logged to the journal).
//
// Numbers are ALWAYS parsed in the "C" locale (decimal point): the compositor runs with
// the user's locale, and with e.g. LC_NUMERIC=de_DE strtod("0.5") returns 0.
//
// Optional click lock (off unless configured): pressing a clickpad rolls the
// finger, and the finger snaps back on release. libinput only pins a pressed
// finger for 1.5 mm, so the shim also intercepts libevdev_next_event, which
// libinput uses to read the touchpad, and holds the reported position still:
// from the press until the finger moved click_lock_mm, and for release_lock_ms
// after the release. It then continues with an offset, so the pointer never jumps.
//
// Touchpads differ: the curve is evaluated in raw device units, so a touchpad with
// twice the resolution moves the pointer more than twice as far. [device "..."]
// sections in the config override settings for devices whose name contains the text.
//
// Log messages: journalctl -b | grep touchpad-accel
// No libinput headers needed; the required declarations are below.

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <linux/input.h>
#include <locale.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>

#ifndef CONF_PATH // test-hook.py builds with its own path
#define CONF_PATH "/etc/touchpad-accel.conf"
#endif
#define MAX_POINTS 64
#define MAX_SLOTS 16
#define MAX_CLICK_LOCK_MM 20.0
#define MAX_RELEASE_LOCK_MS 1000.0

struct libinput_device;
struct libinput_config_accel;

// Values from libinput.h (stable ABI)
enum { PROFILE_CUSTOM = 1 << 2 };
enum { ACCEL_TYPE_FALLBACK = 0, ACCEL_TYPE_MOTION = 1, ACCEL_TYPE_SCROLL = 2 };

typedef int (*set_speed_fn)(struct libinput_device *, double);
typedef int (*set_profile_fn)(struct libinput_device *, int);
typedef int (*finger_count_fn)(struct libinput_device *);
typedef const char *(*get_name_fn)(struct libinput_device *);
typedef struct libinput_config_accel *(*accel_create_fn)(int);
typedef void (*accel_destroy_fn)(struct libinput_config_accel *);
typedef int (*accel_set_points_fn)(struct libinput_config_accel *, int, double,
                                   size_t, double *);
typedef int (*accel_apply_fn)(struct libinput_device *,
                              struct libinput_config_accel *);

#define LOOKUP(var, type, name)                                                \
  static type var;                                                             \
  if (!var)                                                                    \
    var = (type)dlsym(RTLD_NEXT, name);

struct curve {
  double step;
  size_t n;
  double points[MAX_POINTS];
};

struct click_conf {
  double lock_mm;
  double release_ms;
};

// Log to the journal (syslog, independent of the compositor's stderr); when running in a
// terminal, also print to stderr.
static void logmsg(const char *fmt, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  syslog(LOG_USER | LOG_INFO, "touchpad-accel: %s", buf);
  if (isatty(STDERR_FILENO))
    fprintf(stderr, "touchpad-accel: %s\n", buf);
}

static locale_t c_locale(void) {
  static locale_t loc;
  if (!loc)
    loc = newlocale(LC_ALL_MASK, "C", (locale_t)0);
  return loc;
}

// Strict number parsing: the whole token must be a number.
static int parse_num(const char *tok, double *out) {
  char *end;
  double v = strtod_l(tok, &end, c_locale());
  if (end == tok || *end != '\0')
    return -1;
  *out = v;
  return 0;
}

// Format (comments start with #):
//   motion_step   1.0
//   motion_points 0 0.6 1.5 ...
//   scroll_step   1.0            (optional)
//   scroll_points 0 1 2 ...      (optional)
//   [device "text"]              (optional) the following lines, up to the next
//                                section, only apply if the device name contains text
static int parse_points(char *s, struct curve *c) {
  c->n = 0;
  char *save;
  char *tok = strtok_r(s, " \t\r\n", &save);
  while (tok) {
    if (c->n >= MAX_POINTS || parse_num(tok, &c->points[c->n]) != 0)
      return -1;
    c->n++;
    tok = strtok_r(NULL, " \t\r\n", &save);
  }
  return c->n >= 2 ? 0 : -1;
}

static int parse_step(char *s, double *step) {
  char *save;
  char *tok = strtok_r(s, " \t\r\n", &save);
  if (!tok || parse_num(tok, step) != 0 || strtok_r(NULL, " \t\r\n", &save))
    return -1;
  return *step > 0 ? 0 : -1;
}

static int parse_limit(char *s, double max, double *out) {
  char *save;
  char *tok = strtok_r(s, " \t\r\n", &save);
  if (!tok || parse_num(tok, out) != 0 || strtok_r(NULL, " \t\r\n", &save))
    return -1;
  return *out >= 0 && *out <= max ? 0 : -1;
}

// Parses [device "text"] with an optional trailing comment and returns text, or
// NULL if malformed. Runs before comments are stripped: text may contain a '#'.
static const char *parse_section(char *s) {
  static const char prefix[] = "[device \"";
  if (strncmp(s, prefix, sizeof prefix - 1) != 0)
    return NULL;
  char *text = s + sizeof prefix - 1;
  char *end = strchr(text, '"');
  if (!end || end == text || end[1] != ']')
    return NULL;
  *end = '\0';
  s = end + 2;
  s += strspn(s, " \t\r\n");
  return *s == '\0' || *s == '#' ? text : NULL;
}

// Loads the settings for the device called name. Sections for other devices are
// parsed as well, so a typo invalidates the config no matter which device reads it.
static int load_conf(const char *name, struct curve *motion, struct curve *scroll,
                     struct click_conf *click) {
  FILE *f = fopen(CONF_PATH, "re");
  if (!f) {
    logmsg("cannot read %s (%s) - passing through", CONF_PATH, strerror(errno));
    return -1;
  }
  memset(motion, 0, sizeof *motion);
  memset(scroll, 0, sizeof *scroll);
  memset(click, 0, sizeof *click);
  struct curve other_motion = {0}, other_scroll = {0};
  struct click_conf other_click = {0};
  struct curve *m = motion, *s = scroll; // where the current section's values go
  struct click_conf *c = click;
  char line[2048];
  int lineno = 0, err = 0;
  while (fgets(line, sizeof line, f)) {
    lineno++;
    char *start = line + strspn(line, " \t\r\n");
    if (*start == '[') {
      const char *text = parse_section(start);
      if (!text) {
        logmsg("%s line %d invalid (expected [device \"name\"])", CONF_PATH, lineno);
        err = 1;
        continue;
      }
      int match = strstr(name, text) != NULL;
      m = match ? motion : &other_motion;
      s = match ? scroll : &other_scroll;
      c = match ? click : &other_click;
      continue;
    }
    char *hash = strchr(line, '#');
    if (hash)
      *hash = '\0';
    char key[32];
    int off = 0;
    if (sscanf(line, " %31s %n", key, &off) != 1)
      continue;
    char *rest = line + off;
    int rc;
    if (!strcmp(key, "motion_step"))
      rc = parse_step(rest, &m->step);
    else if (!strcmp(key, "scroll_step"))
      rc = parse_step(rest, &s->step);
    else if (!strcmp(key, "motion_points"))
      rc = parse_points(rest, m);
    else if (!strcmp(key, "scroll_points"))
      rc = parse_points(rest, s);
    else if (!strcmp(key, "click_lock_mm"))
      rc = parse_limit(rest, MAX_CLICK_LOCK_MM, &c->lock_mm);
    else if (!strcmp(key, "release_lock_ms"))
      rc = parse_limit(rest, MAX_RELEASE_LOCK_MS, &c->release_ms);
    else
      rc = -1;
    if (rc != 0) {
      logmsg("%s line %d invalid (%s)", CONF_PATH, lineno, key);
      err = 1;
    }
  }
  fclose(f);
  if (err || motion->step <= 0 || motion->n < 2) {
    logmsg("%s invalid (motion_step/motion_points missing or malformed) "
           "- passing through", CONF_PATH);
    return -1;
  }
  return 0;
}

// Bumped whenever a touchpad is configured, which tells the libevdev hook (possibly
// on another thread) to reload its settings. 0 = no touchpad configured here yet, so
// the hook stays off in programs that never set an acceleration.
static _Atomic int conf_generation;

static int is_touchpad(struct libinput_device *dev) {
  LOOKUP(finger_count, finger_count_fn,
         "libinput_device_config_tap_get_finger_count");
  return finger_count && finger_count(dev) > 0;
}

// Reentrancy guard: libinput_device_config_accel_apply() internally calls the
// public libinput_device_config_accel_set_profile(), which lands in our hook
// again. Without the guard -> infinite recursion -> stack overflow (SEGV).
static __thread int in_apply;

static void apply_custom(struct libinput_device *dev) {
  if (in_apply)
    return;
  LOOKUP(get_name, get_name_fn, "libinput_device_get_name");
  LOOKUP(create, accel_create_fn, "libinput_config_accel_create");
  LOOKUP(destroy, accel_destroy_fn, "libinput_config_accel_destroy");
  LOOKUP(set_points, accel_set_points_fn, "libinput_config_accel_set_points");
  LOOKUP(apply, accel_apply_fn, "libinput_device_config_accel_apply");
  if (!create || !destroy || !set_points || !apply)
    return; // libinput too old (< 1.23)

  if (!is_touchpad(dev))
    return;

  conf_generation++;

  const char *name = get_name ? get_name(dev) : "?";
  struct curve motion, scroll;
  struct click_conf click;
  if (load_conf(name, &motion, &scroll, &click) != 0)
    return;

  char stepbuf[32];
  locale_t old = uselocale(c_locale());
  snprintf(stepbuf, sizeof stepbuf, "%g", motion.step);
  uselocale(old);
  struct libinput_config_accel *a = create(PROFILE_CUSTOM);
  if (!a)
    return;
  int ok = set_points(a, ACCEL_TYPE_MOTION, motion.step, motion.n,
                      motion.points) == 0;
  if (ok && scroll.step > 0 && scroll.n >= 2)
    ok = set_points(a, ACCEL_TYPE_SCROLL, scroll.step, scroll.n,
                    scroll.points) == 0;
  in_apply = 1;
  int rc = ok ? apply(dev, a) : -1;
  in_apply = 0;
  destroy(a);
  logmsg("%s: custom curve (%zu points, step %s) %s, click lock %d um, "
         "release lock %d ms",
         name, motion.n, stepbuf, rc == 0 ? "applied" : "FAILED",
         (int)(click.lock_mm * 1000), (int)click.release_ms);
}

int libinput_device_config_accel_set_speed(struct libinput_device *dev,
                                           double speed) {
  LOOKUP(real, set_speed_fn, "libinput_device_config_accel_set_speed");
  int rc = real ? real(dev, speed) : 1;
  apply_custom(dev);
  return rc;
}

int libinput_device_config_accel_set_profile(struct libinput_device *dev,
                                             int profile) {
  LOOKUP(real, set_profile_fn, "libinput_device_config_accel_set_profile");
  int rc = real ? real(dev, profile) : 1;
  apply_custom(dev);
  return rc;
}

// --- Click lock: position filter between libevdev and libinput ---

struct libevdev;

typedef int (*next_event_fn)(struct libevdev *, unsigned int,
                             struct input_event *);
typedef int (*has_property_fn)(const struct libevdev *, unsigned int);
typedef int (*get_int_fn)(const struct libevdev *);
typedef int (*get_abs_fn)(const struct libevdev *, unsigned int);
typedef const char *(*evdev_name_fn)(const struct libevdev *);

struct touch {
  int active;
  int locked;           // held until it moved far enough or the release lock ends
  int64_t release_end;  // event time; 0 = no release so far
  int raw[2], offset[2], center[2], have[2]; // index 0 = x, 1 = y
};

// One clickpad at a time. Switching devices resets the state and rereads the config,
// which at worst means one small pointer jump if two clickpads are touched at the
// same moment.
static struct {
  struct libevdev *dev;
  int generation; // conf_generation these settings were loaded for
  int lock_um, release_us;
  int slot;
  int usable;
  int resolution[2], min[2], max[2];
  struct touch touches[MAX_SLOTS];
} pad;

static void pad_init(struct libevdev *dev) {
  LOOKUP(current_slot, get_int_fn, "libevdev_get_current_slot");
  memset(&pad, 0, sizeof pad);
  pad.dev = dev;
  pad.slot = current_slot ? current_slot(dev) : 0;
}

// Settings of this device. Keeps the touches: a reload must not disturb a held finger.
static void pad_load(struct libevdev *dev, int generation) {
  LOOKUP(get_name, evdev_name_fn, "libevdev_get_name");
  LOOKUP(resolution, get_abs_fn, "libevdev_get_abs_resolution");
  LOOKUP(minimum, get_abs_fn, "libevdev_get_abs_minimum");
  LOOKUP(maximum, get_abs_fn, "libevdev_get_abs_maximum");
  pad.generation = generation;
  pad.lock_um = pad.release_us = 0;
  pad.usable = 0;

  struct curve motion, scroll;
  struct click_conf click;
  const char *name = get_name ? get_name(dev) : NULL;
  if (!name || load_conf(name, &motion, &scroll, &click) != 0 ||
      (click.lock_mm <= 0 && click.release_ms <= 0))
    return;
  pad.lock_um = (int)(click.lock_mm * 1000);
  pad.release_us = (int)(click.release_ms * 1000);

  if (resolution && minimum && maximum) {
    static const unsigned int codes[2] = {ABS_MT_POSITION_X, ABS_MT_POSITION_Y};
    for (int i = 0; i < 2; i++) {
      pad.resolution[i] = resolution(dev, codes[i]);
      pad.min[i] = minimum(dev, codes[i]);
      pad.max[i] = maximum(dev, codes[i]);
    }
    pad.usable = pad.resolution[0] > 0 && pad.resolution[1] > 0;
  }
  logmsg("click lock: reading events of %s%s", name,
         pad.usable ? "" : ", but its axis resolution is unknown - passing through");
}

static int64_t event_time_us(const struct input_event *ev) {
  return (int64_t)ev->input_event_sec * 1000000 + ev->input_event_usec;
}

// Has the finger moved at least click_lock_um from where it was pressed?
static int moved_past_lock(const struct touch *t, int lock_um) {
  // Micrometers per axis; compared as squares to avoid libm (not linked here).
  int64_t x = (int64_t)(t->raw[0] - t->center[0]) * 1000 / pad.resolution[0];
  int64_t y = (int64_t)(t->raw[1] - t->center[1]) * 1000 / pad.resolution[1];
  return x * x + y * y >= (int64_t)lock_um * lock_um;
}

static int is_held(struct touch *t, int64_t now, int lock_um) {
  if (t->release_end) {
    if (now < t->release_end)
      return 1;
    t->locked = 0; // the press lock ends with the release lock
  }
  if (!t->locked)
    return 0;
  if (moved_past_lock(t, lock_um)) {
    t->locked = 0;
    return 0;
  }
  return 1;
}

static void filter_position(struct input_event *ev, int axis, int lock_um) {
  if (pad.slot < 0 || pad.slot >= MAX_SLOTS)
    return;
  struct touch *t = &pad.touches[pad.slot];
  if (!t->active)
    return;
  int delta = t->have[axis] ? ev->value - t->raw[axis] : 0;
  t->raw[axis] = ev->value;
  t->have[axis] = 1;
  if (is_held(t, event_time_us(ev), lock_um))
    t->offset[axis] += delta;
  int v = ev->value - t->offset[axis];
  ev->value = v < pad.min[axis] ? pad.min[axis]
            : v > pad.max[axis] ? pad.max[axis]
                                : v;
}

static void filter_button(const struct input_event *ev, int lock_um,
                          int release_us) {
  for (int i = 0; i < MAX_SLOTS; i++) {
    struct touch *t = &pad.touches[i];
    if (!t->active)
      continue;
    if (ev->value == 1) { // pressed; fingers landing later are not locked
      t->locked = lock_um > 0;
      t->release_end = 0;
      t->center[0] = t->raw[0];
      t->center[1] = t->raw[1];
    } else if (ev->value == 0) {
      t->release_end = event_time_us(ev) + release_us;
    }
  }
}

static void filter_event(struct libevdev *dev, struct input_event *ev) {
  LOOKUP(has_property, has_property_fn, "libevdev_has_property");
  int generation = conf_generation;
  if (generation == 0 || !has_property ||
      (ev->type != EV_ABS && ev->type != EV_KEY) ||
      !has_property(dev, INPUT_PROP_BUTTONPAD))
    return;
  if (dev != pad.dev)
    pad_init(dev);
  if (pad.generation != generation)
    pad_load(dev, generation);
  if (!pad.usable)
    return;
  int lock_um = pad.lock_um, release_us = pad.release_us;

  if (ev->type == EV_KEY) {
    if (ev->code == BTN_LEFT)
      filter_button(ev, lock_um, release_us);
    return;
  }
  switch (ev->code) {
  case ABS_MT_SLOT:
    pad.slot = ev->value;
    break;
  case ABS_MT_TRACKING_ID:
    if (pad.slot >= 0 && pad.slot < MAX_SLOTS) {
      memset(&pad.touches[pad.slot], 0, sizeof pad.touches[pad.slot]);
      pad.touches[pad.slot].active = ev->value != -1;
    }
    break;
  case ABS_MT_POSITION_X:
    filter_position(ev, 0, lock_um);
    break;
  case ABS_MT_POSITION_Y:
    filter_position(ev, 1, lock_um);
    break;
  default:
    break;
  }
}

int libevdev_next_event(struct libevdev *dev, unsigned int flags,
                        struct input_event *ev) {
  LOOKUP(real, next_event_fn, "libevdev_next_event");
  if (!real)
    return -ENOSYS;
  int rc = real(dev, flags, ev);
  if (rc == 0 || rc == 1) // LIBEVDEV_READ_STATUS_SUCCESS / _SYNC
    filter_event(dev, ev);
  return rc;
}
