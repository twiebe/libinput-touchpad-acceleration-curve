# libinput-touchpad-acceleration-curve

A custom pointer acceleration curve for touchpads on **KDE Plasma (Wayland)**, even
though KWin does not (yet) offer libinput's custom acceleration profile. The goal is
a touchpad feel closer to a MacBook: slow movements stay precise, fast swipes travel
far.

It works as a small shim library (`touchpad-accel.so`) that hooks into KWin's calls
to libinput and applies a curve from `/etc/touchpad-accel.conf`. It comes with tools
to test it safely and to measure how your touchpad actually behaves.

> [!WARNING]
> This is a hack. The library is loaded into **every** process via
> `/etc/ld.so.preload`, including KWin and the login manager. A bug in it can take
> down the graphical login (black screen). Read [Recovery](#recovery) before
> installing.

## Is this for you?

- **KDE Plasma on Wayland:** yes, that is what it is made for.
- **Your compositor already exposes libinput's custom profile** (e.g. Hyprland's
  `accel_profile = custom`): use that instead.
- **Plasma on X11:** not needed. The Xorg libinput driver exposes the custom profile
  through `xinput` (properties `libinput Accel Custom Motion Points/Step`).
- **KDE adds it natively:** work on a curve editor is ongoing
  ([plasma-desktop!3569], [Bug 464868]). Once it ships, uninstall this.

## Compatibility

| | Status |
|---|---|
| Bazzite (Fedora Atomic 44), KWin 6.7.5, libinput 1.31.3, Framework 13 Pro | Tested, in daily use |
| Other Fedora-based systems with Plasma 6 | Expected to work (same layout) |
| Debian/Ubuntu, Arch | `install.sh`/`uninstall.sh` were tested on a Debian layout; the shim itself is untested there |
| Other Wayland compositors | The shim hooks libinput, not KWin, so it may work wherever a compositor calls `libinput_device_config_accel_set_speed`/`_set_profile`. Untested; `reload.sh` is KWin-only |

Requirements: glibc (for `/etc/ld.so.preload`), libinput ≥ 1.23 (custom profile),
x86_64. The shim needs at least the glibc version it was built against. Build on the
system you install on, or in a container whose glibc is not newer than the host's.
A build in a `fedora:44` distrobox (as below) needs glibc ≥ 2.38 on the host.

### Distributions

The mechanism is not tied to Bazzite: any glibc distribution honors
`/etc/ld.so.preload`, and `install.sh` handles the library layouts of Fedora,
Debian/Ubuntu and Arch. What decides is the libinput version:

| Distribution | libinput | Custom profile |
|---|---|---|
| Fedora 38+ / Bazzite, Kinoite | ≥ 1.23 | yes |
| Debian 13 (trixie) | 1.28 | yes |
| Debian 12 (bookworm) | 1.22 | **no** |
| Ubuntu 24.04 LTS and newer | ≥ 1.25 | yes |
| Ubuntu 22.04 LTS | 1.20 | **no** |
| Arch, openSUSE Tumbleweed | current | yes |
| musl-based (Alpine, Void musl) | – | **no**: musl ignores `/etc/ld.so.preload` |

`install.sh` checks both requirements before it installs anything. It looks for
`libinput_config_accel_create` in the system's `libinput.so.10` (only present from
1.23 on), and it test-loads the shim via `LD_PRELOAD`, so a build against a too-new
glibc is caught before it reaches `/etc/ld.so.preload`. Only the 64-bit build is
test-loaded. A 32-bit build against a too-new glibc would still break 32-bit programs
such as Steam, so always build both with `make` on the same system.

On distributions with a writable `/usr` you could patch libinput directly instead.
The shim has the advantage of surviving libinput updates as long as the API stays
the same.

## How it works

Since version 1.23, libinput supports a freely definable curve (the `custom`
profile). On Wayland, however, the compositor has to expose it, and KWin only
offers `flat` and `adaptive`.

`touchpad-accel.so` intercepts the two libinput calls KWin uses to set acceleration
(`libinput_device_config_accel_set_speed` / `_set_profile`). After each such call
for a **touchpad** (a device with tap support), the shim applies the curve from
`/etc/touchpad-accel.conf`. Other devices such as mice are left alone. If the
config file is missing or invalid, the shim just passes calls through and logs the
reason to the journal.

Why `/etc/ld.so.preload` instead of `LD_PRELOAD` for KWin only? `kwin_wayland` has
the file capability `cap_sys_nice`, so the dynamic linker ignores `LD_PRELOAD`
(secure-execution mode). `/etc/ld.so.preload` is honored even then. The preload
entry is `/usr/local/$LIB/touchpad-accel.so`. glibc expands `$LIB` per process to its
library directory, so 64-bit and 32-bit processes each load the matching build.
32-bit programs therefore don't print `wrong ELF class` errors.

### Click lock (optional)

Pressing a clickpad rolls the finger, and the finger snaps back on release, which
shifts the pointer. libinput pins a pressed finger, but only for 1.5 mm. The click
lock adds to that. It is off unless you add these lines to `/etc/touchpad-accel.conf`:

```
click_lock_mm   0.5   # extra travel after a press before the pointer moves
release_lock_ms 120   # pointer stays still this long after the release
```

The values come from `libinput record` files of a Framework 13 Pro (37 clicks and
drags); the snap-back was over after about 130 ms. The costs: drags start 0.5 mm later, and
motion in the first 120 ms after a release is lost, even if you meant it.

For this, the shim also intercepts `libevdev_next_event`, which libinput uses to read
the touchpad. It only changes finger positions on clickpads, and it continues with an
offset after a hold, so the pointer doesn't jump. This code runs on every touchpad
event inside KWin, so run both tests before your next login.

**Add these lines only after you logged in with the new version.** An older shim
treats the unknown keys as an invalid config and drops the whole curve.

## Installation

Build. You need `gcc`, `make` and a 32-bit toolchain (32-bit glibc headers and link
libraries). If it is incomplete, `make` stops before the 32-bit build and lists the
packages to install. On Atomic systems (Bazzite,
Silverblue, Kinoite), build in a distrobox:

```bash
git clone https://github.com/twiebe/libinput-touchpad-acceleration-curve.git
cd libinput-touchpad-acceleration-curve

distrobox create -i fedora:44 -n build && distrobox enter build   # Atomic systems only
sudo dnf install -y gcc make glibc-devel glibc-devel.i686 libgcc.i686 libatomic.i686
                                       # Debian/Ubuntu: gcc make gcc-multilib
                                       # Arch: gcc make lib32-glibc lib32-gcc-libs (multilib repo)
make
exit                                                                # leave the distrobox
```

Install, test, log in again:

```bash
sudo ./install.sh              # libraries to /usr/local, entry in /etc/ld.so.preload
sudo python3 -I test-real.py   # check against the real libinput (see below)
# log out and back in
journalctl -b | grep 'custom curve' | tail -1
```

`install.sh` determines the library directories from where your libc lives (Fedora:
`lib64`/`lib`, Debian: `lib/x86_64-linux-gnu`/`lib32`, Arch: `lib`/`lib32`). Before
installing, it checks that libinput is ≥ 1.23 and that the shim loads on this system
(see [Distributions](#distributions)). Afterwards it checks that a newly started
process loads the shim. If not, it removes its preload
entry again and stops. It doesn't overwrite an existing `/etc/touchpad-accel.conf`.
`/etc` and `/usr/local` survive rpm-ostree/bootc updates.

**Updating:** `git pull`, `make`, `sudo ./install.sh`, test, log in again.

## Tools

| File | Purpose |
|---|---|
| `test-real.py` | Opens the touchpad in a **separate** libinput context (KWin is not affected), makes the same calls as KWin under the user's locale, and checks that the custom profile becomes active. If the test crashes, KWin would have crashed too, but your login stays intact. **Run it after every change to the shim and after every libinput update, before your next login.** |
| `test-hook.py` | Tests the click lock without a touchpad or root, against a fake libinput/libevdev. Optionally replays `libinput record` files: `python3 -I test-hook.py recording.yml`. |
| `measure.py` | Records for 15 s how finger speed is translated into pointer motion (table by mm/s, gain, swallowed events) and shows your touchpad's resolution. Useful to compare `adaptive` with your own curve. |
| `reload.sh` | Makes KWin re-apply the curve without logging in again: nudges the touchpad speed via D-Bus by 0.01 and back. No `sudo` needed. |
| `uninstall.sh` | Removes the libraries and the preload entry. The config is kept. |

## The curve

### Format

```
motion_step   0.25                    # spacing of the points on the x axis
motion_points 0 0.1 0.25 ...          # y values at x = 0, step, 2*step, ...
#scroll_step / scroll_points           # optional separate curve for scrolling
```

- **x** = finger speed, **y** = pointer speed. libinput interpolates linearly
  between points and extrapolates past the last point using the last slope.
- **Units:** for touchpads, libinput evaluates the custom curve in **raw touchpad
  units per ms**, *not* normalized to 1000 dpi like the other profiles. x is in
  touchpad units/ms, y in logical pixels/ms.
- **Gain** (y/x) is the more intuitive quantity: 1 means 1:1, below 1 slows down,
  above 1 speeds up.
- Always write numbers with a **decimal point**, regardless of your locale.

### Bundled curves

The bundled curves were tuned by hand on a **Framework 13 Pro** (touchpad PIXA3854,
24 units/mm, so 1 u/ms ≈ 42 mm/s), starting from measurements of KWin's `adaptive`
profile on the same hardware. They are a starting point, not a universal answer:
how a curve feels depends on the touchpad, the screen and personal taste.

All of them are identical in the mid and upper range (gain ~1.0 at 50 mm/s, ~1.5 at
120 mm/s, ~2.3 at 200 mm/s). They differ only for precise work:

| File | Gain for slow movement (< ~15 mm/s) |
|---|---|
| `touchpad-accel.conf` (default) | 0.4 |
| `examples/gain-0.5.conf` | 0.5 |
| `examples/gain-0.62.conf` | 0.62 |

For comparison, `adaptive` at slider position 0.4 measured ~0.75–0.95 across the
entire slow and medium range on the same hardware, and only rose slightly above
120 mm/s (~1.3).

### Adapting to another touchpad

Run `sudo python3 -I measure.py` to see your touchpad's resolution R (units/mm). To
get the same feel as on the Framework 13 Pro (same pixels per millimeter of finger travel at
the same finger speed), multiply `motion_step` by **R / 24** and keep the points
unchanged. Example: R = 40 → `motion_step 0.4167` instead of `0.25`.

Then tune to taste:

```bash
sudoedit /etc/touchpad-accel.conf
./reload.sh                    # KWin re-applies the curve, no new login needed
sudo python3 -I measure.py     # optional: check the resulting gains
```

- Precise work too fast or twitchy: lower the first few points.
- Normal movement too slow or fast: adjust the middle points.
- Fast swipes don't travel far enough: raise the last points.

libinput's custom profile doesn't smooth the speed across events, so a curve that is
steep in the slow range feels jittery. Keep the slow range flat.

## Recovery

If the screen stays black after login or the login manager doesn't start:

1. Press **Ctrl+Alt+F3** and log in on the text console (bigger font: `setfont -d`)
2. `sudo rm /etc/ld.so.preload` (or `sudo ./uninstall.sh`)
3. `sudo reboot`

On rpm-ostree/bootc systems, booting the previous deployment only helps as long as no
system update has been installed since you installed the shim. After that, the
previous deployment contains the preload entry as well.

To disable the curve temporarily without uninstalling:
`sudo mv /etc/touchpad-accel.conf{,.off} && ./reload.sh`

## Pitfalls found during development

- **Recursion:** `libinput_device_config_accel_apply()` internally calls the public
  `libinput_device_config_accel_set_profile()`, which lands in the hook again.
  Without a guard, this recurses infinitely and crashes KWin. The shim uses a
  thread-local reentrancy guard.
- **Locale:** KWin runs with the user's locale. With `LC_NUMERIC=de_DE`,
  `strtod("0.5")` returns `0`, which turned the first curve into a dead zone. The
  shim therefore parses with `strtod_l` in the C locale and rejects partially parsed
  numbers.
- **Units:** touchpads use raw device units instead of 1000 dpi (see above).
- **Library directories:** `$LIB` expands differently per distribution (see
  [Installation](#installation)).

## Known limitations

- Every device with tap support gets the curve, including an externally connected
  touchpad.
- The speed slider in System Settings has no effect while the curve is active. It
  only serves as a trigger to reload (which is what `reload.sh` uses).
- libinput's custom profile does not smooth the speed across events (see above).
- The click lock needs a libinput that links libevdev as a shared library
  (`test-real.py` shows this). The lock can't tell a large roll from a slow drag: a
  roll beyond `click_lock_mm` still moves the pointer.

[plasma-desktop!3569]: https://invent.kde.org/plasma/plasma-desktop/-/merge_requests/3569
[Bug 464868]: https://bugs.kde.org/show_bug.cgi?id=464868

## References

- [libinput: Pointer acceleration](https://wayland.freedesktop.org/libinput/doc/latest/pointer-acceleration)
- [libinput MR !775: Introduce custom acceleration profile](https://gitlab.freedesktop.org/libinput/libinput/-/merge_requests/775)
- libinput 1.31.3: `src/libinput.c` (`libinput_device_config_accel_apply`),
  `src/filter-custom.c`, `src/evdev-mt-touchpad.c` (`tp_filter_motion`)
- KWin 6.7.5: `src/backends/libinput/device.cpp` (`Device::setPointerAcceleration`)

## License

[MIT](LICENSE) © 2026 Thomas Wiebe
