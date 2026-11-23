CC      ?= gcc
CFLAGS  ?= -O2 -g -Wall -Wextra
LDLIBS  ?= -ldl

all: hip_trace.so libfakehip.so selftest

hip_trace.so: hip_trace.c
	$(CC) $(CFLAGS) -fPIC -shared -pthread -o $@ $< $(LDLIBS)

# Stand-in HIP library for the CPU-only self-test: no ROCm, no GPU, no driver.
libfakehip.so: fakehip.c
	$(CC) $(CFLAGS) -fPIC -shared -o $@ $<

# The self-test links the stub HIP library, not the shim: the shim is loaded by
# LD_PRELOAD so that it precedes the executable's own symbol scope.
selftest: selftest.c libfakehip.so
	$(CC) $(CFLAGS) -o $@ $< -L. -lfakehip -Wl,-rpath,'$$ORIGIN' -ldl

check: all
	./selftest

clean:
	rm -f hip_trace.so libfakehip.so selftest

.PHONY: all check clean
