CC ?= gcc
PKG_CONFIG ?= pkg-config

CFLAGS ?= -O2 -Wall -Wextra -Wno-unused-parameter -Wno-deprecated-declarations -D_GNU_SOURCE -I.
DEPS_CFLAGS := $(shell $(PKG_CONFIG) --cflags libimobiledevice-1.0 libimobiledevice-glue-1.0 libplist-2.0 openssl libzip 2>/dev/null)
DEPS_LIBS := $(shell $(PKG_CONFIG) --libs libimobiledevice-1.0 libimobiledevice-glue-1.0 libplist-2.0 openssl libzip 2>/dev/null) -lreadline -lm

TARGET = mdm_patch
SRCS = main.c patch_logic.c idevicebackup2.c libidevicefunctions.c utils.c bad_query_ios27.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) $(DEPS_CFLAGS) -o $@ $(SRCS) $(DEPS_LIBS)

clean:
	rm -f $(TARGET) *.o
