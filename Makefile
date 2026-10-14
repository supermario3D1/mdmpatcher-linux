PYTHON ?= python3

.PHONY: all test install update deobfuscate build-c clean

all: test

test:
	$(PYTHON) -m unittest discover -s tests -v

# One-command install: clones/updates from git `main` and puts `mdmpatcher` on PATH
install:
	./scripts/install.sh

# Pull the latest code from the tracked branch (default: main)
update:
	$(PYTHON) mdmpatcher.py update

deobfuscate:
	$(PYTHON) mdmpatcher.py deobfuscate -a assets -o assets/deobfuscated

build-c:
	$(MAKE) -C src/c

clean:
	rm -rf assets/deobfuscated output_backup build dist *.egg-info
	find . -name "__pycache__" -type d -exec rm -rf {} +
	$(MAKE) -C src/c clean
