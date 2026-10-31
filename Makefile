# SPDX-License-Identifier: MIT
CC     ?= cc
CFLAGS ?= -O2 -Wall -Wextra

all: touchpad-accel.so touchpad-accel-32.so

touchpad-accel.so: touchpad-accel.c
	$(CC) $(CFLAGS) -shared -fPIC -o $@ $<

# needs 32-bit glibc headers (Fedora: glibc-devel.i686, Debian: gcc-multilib)
touchpad-accel-32.so: touchpad-accel.c
	$(CC) -m32 $(CFLAGS) -shared -fPIC -o $@ $<

clean:
	rm -f touchpad-accel.so touchpad-accel-32.so

.PHONY: all clean
