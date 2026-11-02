# SPDX-License-Identifier: MIT
CC     ?= cc
CFLAGS ?= -O2 -Wall -Wextra

all: touchpad-accel.so touchpad-accel-32.so

touchpad-accel.so: touchpad-accel.c
	$(CC) $(CFLAGS) -shared -fPIC -o $@ $<

# The 32-bit build needs a 32-bit toolchain (headers and link libraries). Probe it
# with a trivial library first, so a missing package gives a clear message instead
# of a cryptic compiler or linker error.
touchpad-accel-32.so: touchpad-accel.c
	@printf '#include <dlfcn.h>\nint x;\n' | \
	  $(CC) -m32 -shared -fPIC -x c - -o /dev/null 2>/dev/null || { \
	  echo "ERROR: no working 32-bit toolchain (-m32). Install:"; \
	  echo "  Fedora:        sudo dnf install glibc-devel.i686 libgcc.i686 libatomic.i686"; \
	  echo "  Debian/Ubuntu: sudo apt install gcc-multilib"; \
	  echo "  Arch:          sudo pacman -S lib32-glibc lib32-gcc-libs"; \
	  echo "Details: printf 'int x;\\\\n' | $(CC) -m32 -shared -fPIC -x c - -o /dev/null"; \
	  exit 1; }
	$(CC) -m32 $(CFLAGS) -shared -fPIC -o $@ $<

clean:
	rm -f touchpad-accel.so touchpad-accel-32.so

.PHONY: all clean
