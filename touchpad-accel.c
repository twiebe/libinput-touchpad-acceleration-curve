// SPDX-License-Identifier: MIT
//
// touchpad-accel: forces a libinput custom acceleration curve onto touchpads,
// even if the compositor (KWin) does not offer the custom profile.
//
// How it works: the library is loaded via /etc/ld.so.preload and intercepts
// libinput_device_config_accel_set_speed / _set_profile. Whenever KWin calls
// these for a touchpad (at startup, on hotplug, when the speed slider changes),
// the curve from CONF_PATH is applied afterwards. If the file is missing or
// invalid, calls are only passed through (the reason is logged to the journal).
//
// Numbers are ALWAYS parsed in the "C" locale (decimal point): KWin runs with
// the user's locale, and with e.g. LC_NUMERIC=de_DE strtod("0.5") returns 0.
//
// Log messages: journalctl -b | grep touchpad-accel
// No libinput headers needed; the required declarations are below.

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <locale.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>

#define CONF_PATH "/etc/touchpad-accel.conf"
#define MAX_POINTS 64

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

// Log to the journal (syslog, independent of KWin's stderr); when running in a
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

static int load_conf(struct curve *motion, struct curve *scroll) {
  FILE *f = fopen(CONF_PATH, "re");
  if (!f) {
    logmsg("cannot read %s (%s) - passing through", CONF_PATH, strerror(errno));
    return -1;
  }
  memset(motion, 0, sizeof *motion);
  memset(scroll, 0, sizeof *scroll);
  char line[2048];
  int lineno = 0, err = 0;
  while (fgets(line, sizeof line, f)) {
    lineno++;
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
      rc = parse_step(rest, &motion->step);
    else if (!strcmp(key, "scroll_step"))
      rc = parse_step(rest, &scroll->step);
    else if (!strcmp(key, "motion_points"))
      rc = parse_points(rest, motion);
    else if (!strcmp(key, "scroll_points"))
      rc = parse_points(rest, scroll);
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

  struct curve motion, scroll;
  if (load_conf(&motion, &scroll) != 0)
    return;

  const char *name = get_name ? get_name(dev) : "?";
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
  logmsg("%s: custom curve (%zu points, step %s) %s", name, motion.n, stepbuf,
         rc == 0 ? "applied" : "FAILED");
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
