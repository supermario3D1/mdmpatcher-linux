# Contributing to MDMPatcher-Linux

Thank you for your interest in contributing to **MDMPatcher-Linux**!

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/supermario3D1/mdmpatcher-linux.git
   cd mdmpatcher-linux
   ```
2. Install system packages (Fedora, Ubuntu/Debian, Arch, or openSUSE):
   ```bash
   ./scripts/install.sh --venv
   ```
3. Run the automated test suite:
   ```bash
   make test
   ```

## Pre-Restore Safety Requirements (verified on iOS 9 - 26.x; iOS 27+ experimental)

Any changes touching `mdmpatcher/ios26.py`, `mdmpatcher/manifest_db.py`, or `mdmpatcher/patcher.py` **must** preserve all 6 checks in `verify_ios26_backup()`:
1. Valid SQLite3 `Manifest.db` schema.
2. Unencrypted `Manifest.plist` (`IsEncrypted = False`) written atomically via `atomic_write_plist()`.
3. `Status.plist` `SnapshotState == "finished"`.
4. Complete removal of `MDM.plist` and `MDMEvents.plist` rows before injection.
5. Strictly unique, positive `InodeNumber` values across all `flags IN (1, 2)` rows and valid `SPRINGBOARD_EXT_ATTR_BYTES` on regular files.
6. Complete parent directory row chains (`flags = 2`) for every injected file.
