# Security & Responsible Use Policy

## Authorized Use Only

**MDMPatcher-Linux** is developed for security research, interoperability analysis, and authorized recovery of personal devices you legally own or have explicit written authorization to administer.

## Device Safety Controls (Zero-Brick / Zero-Loop Architecture)

Unlike legacy SparseRestore/BookRestore tools that can trigger iOS 27's partial-restore "safe state recovery" factory-reset loop:
1. **No Firmware or Boot-Chain Modifications**: MDMPatcher-Linux never modifies SecureROM, iBoot, SEP, or kernel images.
2. **Pre-Restore 6-Point Verification Gate**: Injected backups are verified on disk (`verify_ios27_backup`) before a single packet is transmitted to the device.
3. **Controlled `--no-reboot` Restore**: The device is never instructed to reboot mid-restore; reboot happens only after `MobileBackup2` and `MCInstall` report completion.
