#!/usr/bin/env python3
"""
Entry-point script for MDMPatcher-Linux.

Works no matter how it is invoked:

    ./mdmpatcher.py patch            # from a checkout
    mdmpatcher patch                 # via the launcher installed on your PATH
    python3 /path/to/mdmpatcher.py patch
    python3 -m mdmpatcher patch
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from mdmpatcher.cli import main
except ImportError as exc:  # pragma: no cover - defensive
    print(f"[!] Could not import the 'mdmpatcher' package from {REPO_ROOT}: {exc}", file=sys.stderr)
    print("    Re-run the installer: ./scripts/install.sh", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
