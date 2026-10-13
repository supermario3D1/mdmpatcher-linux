PYTHON ?= python3

.PHONY: all test deobfuscate build-c clean

all: test

test:
	$(PYTHON) -m unittest discover -s tests -v

deobfuscate:
	$(PYTHON) mdmpatcher.py deobfuscate -a assets -o assets/deobfuscated

build-c:
	$(MAKE) -C src/c

clean:
	rm -rf assets/deobfuscated output_backup build dist *.egg-info
	find . -name "__pycache__" -type d -exec rm -rf {} +
	$(MAKE) -C src/c clean
