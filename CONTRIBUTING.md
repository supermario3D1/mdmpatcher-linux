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
   ./scripts/install.sh            # also works as: curl -fsSL .../main/scripts/install.sh | bash
   ```
3. Run the automated test suite:
   ```bash
   make test
   ```

> The installer creates a `.venv` and a `mdmpatcher` launcher on your PATH by default.
> Use `--no-venv` to run against the system Python instead.

## Pre-Restore Safety Requirements (iOS 27+)

**The safety gate lives in `mdmpatcher/preflight.py` and is enforced in code, not by convention.**
`mdmpatcher patch` runs `verify_backup()` after building a backup and refuses to restore on any
hard failure unless the user passes `--force`. `mdmpatcher preflight` exposes the same audit
(read-only) for users, and `classify_restore_failure()` turns non-zero restore exits into
actionable hints.

When you add or change a patch mode:

1. Add its required payloads to `EXPECTED_PAYLOADS` in `preflight.py` so the audit can detect a
   silently-missing payload.
2. Add a test to `TestPreflightSafety` in `tests/test_mdmpatcher.py` covering both the clean case
   *and* a corrupted case (missing payload, zero-byte payload, leftover `MDM.plist`, encrypted
   manifest) — the gate must never pass on a broken backup.
3. Keep the gate read-only: verification must never write to the backup or the device.

Any changes touching `mdmpatcher/ios27.py`, `mdmpatcher/manifest_db.py`, or `mdmpatcher/patcher.py` **must** preserve all 6 checks in `verify_ios27_backup()`:
1. Valid SQLite3 `Manifest.db` schema.
2. Unencrypted `Manifest.plist` (`IsEncrypted = False`) written atomically via `atomic_write_plist()`.
3. `Status.plist` `SnapshotState == "finished"`.
4. Complete removal of `MDM.plist` and `MDMEvents.plist` rows before injection.
5. Strictly unique, positive `InodeNumber` values across all `flags IN (1, 2)` rows and valid `SPRINGBOARD_EXT_ATTR_BYTES` on regular files.
6. Complete parent directory row chains (`flags = 2`) for every injected file.
