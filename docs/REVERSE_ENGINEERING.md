# Reverse Engineering Analysis: MDMPatcher-Universal, MDMPatcher-Enhanced & Modern iOS Architecture (verified iOS 9 – 26.x)

This document details the complete reverse engineering of [`tazMeah/MDMPatcher-Universal`](https://github.com/tazMeah/MDMPatcher-Universal) and [`fled-dev/MDMPatcher-Enhanced`](https://github.com/fled-dev/MDMPatcher-Enhanced), the root causes of why the original macOS tools fail on modern iOS versions (iOS 15 through iOS 26.x), and how **MDMPatcher-Linux v2.8.0** resolves them reliably across Linux distributions (Fedora, Ubuntu/Debian, Arch, openSUSE).

> **Verified support window: iOS 9.0 – iOS 26.x (builds `23A+`).** Everything below iOS 27 is covered.
> iOS 27.0+ (builds `24A+`) is **experimental**: the same engine runs, but the CLI prints an
> `UNVERIFIED FIRMWARE` warning (`warn_if_unsupported_ios()` in `mdmpatcher/ios26.py`) before any
> device is touched, because the `BackupAgent2` and `MobileConfigService` changes described in
> §4.2 have not been validated against released firmware.

---

## 1. Deobfuscation of `MDMPatcher-Universal` Assets

`MDMPatcher-Universal` bundles three disguised resource files in its macOS app bundle:

| Obfuscated Resource | Size (bytes) | Decrypted Output | Decrypted Size | Purpose |
| :--- | ---: | :--- | ---: | :--- |
| `extension1.pdf` | 9,975 | `clean_info.plist` | 9,903 | Backup `Info.plist` template |
| `extension2.pdf` | 3,958 | `clean_manifest.plist` | 3,870 | Backup `Manifest.plist` template |
| `libiMobileeDevice.dylib` | 108,043 | `clean_archive.zip` | 107,965 | Zip archive containing `MDMB/` backup files & `Manifest.mbdb` |

### 1.1 Layer 1 & 3: 6-Pair Fixed-Index Byte Swapping

In `ContentView.swift` (`fuckTheArray1` through `fuckTheArray6`), the developer implemented a symmetric byte-permutation step that swaps six hardcoded index pairs **both before encryption and after decryption**:

```
(3, 5), (8, 17), (128, 345), (15, 65), (33, 133), (16, 64)
```

Because none of the 12 indices overlap, the permutation is an involution (`apply_byte_swaps(apply_byte_swaps(data)) == data`).

### 1.2 Layer 2: RNCryptor v3 Encryption & Swift Floating-Point Password Derivation

Between the two byte-swap passes, the payload is encrypted with **RNCryptor Format v3**:
- **Header (`34` bytes)**:
  - `byte[0] = 0x03` (RNCryptor version 3)
  - `byte[1] = 0x01` (options: password-based key derivation)
  - `bytes[2..9]` (`8` bytes): Encryption PBKDF2 salt
  - `bytes[10..17]` (`8` bytes): HMAC PBKDF2 salt
  - `bytes[18..33]` (`16` bytes): AES-256-CBC IV
- **Body**: AES-256-CBC ciphertext with PKCS#7 padding
- **Trailer (`32` bytes)**: `HMAC-SHA256(hmac_key, header || ciphertext)`
- **Key derivation**: `PBKDF2-HMAC-SHA1(password, salt, iterations=10000, dkLen=32)`

#### Bug Found in `MDMPatcher-Enhanced/deobfuscation.swift`

In `MDMPatcher-Universal/ContentView.swift`, the password is constructed at runtime via:

```swift
var i: Double = 4*2*4*6
i = i * 7/5 + 23
i = i - 546*5464564*64635645*4536454*462
// i evaluates in IEEE-754 Double to -4.0418364248546965e+26
var key = ("qepkwotkgpeqgpeokqgokgqoe\(i)fdlgkdlgfklsdöfdgsj\(i)gfdads23ji4jgi3vqewö")
    .replacingOccurrences(of: "q", with: "r")
```

Thus the true RNCryptor v3 password is:
```
repkwotkgpergpeokrgokgroe-4.0418364248546965e+26fdlgkdlgfklsdöfdgsj-4.0418364248546965e+26gfdads23ji4jgi3vrewö
```
*(In `fled-dev/MDMPatcher-Enhanced/deobfuscation.swift`, a manual comment substituted `-3.711304287948089e+24`, which fails HMAC verification if executed directly.)*

`mdmpatcher/crypto.py` reproduces this exact IEEE-754 `Double` pipeline and verifies that decrypting `extension1.pdf`, `extension2.pdf`, and `libiMobileeDevice.dylib` matches `clean_info.plist`, `clean_manifest.plist`, and `clean_archive.zip` byte-for-byte.

---

## 2. Anatomy of the Legacy `MDMB` Backup Archive

Inspecting `clean_archive.zip` (`MDMB/Manifest.mbdb`, Backup v2.4 format) reveals 117 records originally captured from an iOS 9.1 backup created by **iMyFone** (`UserAssignedDeviceName = "“iMyFone”的 iPhone"`, `Locale = zh_CN`, `LastMigratedBuild = 10B329`), with 7 ConfigurationProfile files appended at records `#52`–`#59`:

| Record # | Domain | Relative Path | SHA-1 Filename (`SHA1(domain + "-" + path)`) |
| ---: | :--- | :--- | :--- |
| 13 | `HomeDomain` | `Library/Preferences/com.apple.purplebuddy.plist` | `80fa782261261e54bb73c0253e5b6beefb6a2f39` |
| 52 | `HomeDomain` | `Library/ConfigurationProfiles/UserSettings.plist` | `3432b051212e4311c705efa56fc4db8c58aed802` |
| 54 | `HomeDomain` | `Library/ConfigurationProfiles/PublicInfo/MCMeta.plist` | `10c0b06595e6ff4e95ee09e742f9797c5367385e` |
| 55 | `HomeDomain` | `Library/ConfigurationProfiles/PublicInfo/EffectiveUserSettings.plist` | `f23461ec2e507af102a699e5e1fb5080608024b5` |
| 56 | `HomeDomain` | `Library/ConfigurationProfiles/ProfileTruth.plist` | `f7bbe63e61427d2ee896496f5726b81289cfda38` |
| 57 | `HomeDomain` | `Library/ConfigurationProfiles/PayloadManifest.plist` | `45b1a377feee16fa8e2fbf128469e2b9e5403e6f` |
| 58 | `HomeDomain` | `Library/ConfigurationProfiles/MCDataMigration.plist` | `d1c4ed94e4c4421c3093d117201ac557f1ee5236` |
| 59 | `HomeDomain` | `Library/ConfigurationProfiles/ClientTruth.plist` | `8d4ce80ec7465dc7a4f1ee1b50856c3159adfce3` |

---

## 3. Why the Original Tools Fail on iOS 15 – 26

Our analysis identified **five architectural flaws** in `MDMPatcher-Universal` and `MDMPatcher-Enhanced`:

1. **Missing `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`**:
   Starting in iOS 13+ and enforced strictly in iOS 15–26, `profiled` and `Setup.app` read DEP/MDM configuration from:
   - `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles/Library/ConfigurationProfiles/CloudConfigurationDetails.plist` (`fileID`: `1e6c0783f9b33d00b152067a0661c8fc8841073f`)
   - `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles/Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist` (`fileID`: `dbf354b95ce3e067684b5c4b66d19d5c8cac4d80`)
2. **Missing `ManagedPreferencesDomain`**:
   Modern `Setup.app` (`purplebuddy`) checks `ManagedPreferencesDomain/mobile/com.apple.purplebuddy.plist` (`fileID`: `eb25d9cd0f9fa7b7f648596a1652163bbf4771de`) for `SetupDone = true` and `SetupFinishedAllSteps = true`.
3. **Missing `CMD_FLAG_RESTORE_SYSTEM_FILES` (`RestoreSystemFiles: true`)**:
   In `MDMPatcher-Universal/idevicebackup2.c`, `mainLOL()` omitted `CMD_FLAG_RESTORE_SYSTEM_FILES`, causing iOS `backupd` to ignore non-`HomeDomain` system containers.
4. **Hardcoded iOS 9.1 / 6.1.3 Metadata & Side Effects**:
   - `MCMeta.plist` hardcoded `LastMigratedBuild = 10B329` (iOS 6.1.3), triggering profile migration wipes when `LastMigratedBuild != BuildVersion`.
   - `purplebuddy.plist` hardcoded `Locale = zh_CN` and `SetupVersion = 7`.
   - `com.apple.mobilegestalt.plist` overwrote the user's device name with `"“iMyFone”的 iPhone"`.
5. **macOS-Only USB Detection (`IOKit`)**:
   `USBDetection.swift` used macOS `IOKit` and only matched USB PIDs `0x12a8` and `0x12ab`.

---

## 4. What Apple Changed in iOS 26.2+ (and in the iOS 27 betas, Builds `24A+`)

In iOS 26.2+ — and continuing in the iOS 27.0 betas (`24A335` and newer) — Apple introduced three additional security hardening layers that broke legacy SparseRestore and BookRestore tools:

### 4.1 Partial-Restore Safe-State Recovery & `BackupAgent2` Validation
Apple patched the partial-restore vectors (`SparseRestore` and `bookassetd` `BookRestore`). When `BackupAgent2` receives a synthetic partial `Manifest.db`:
- If regular file (`flags=1`) or directory (`flags=2`) rows lack a strictly unique, positive `InodeNumber`, `BackupAgent2`'s inode-deduplication pass either overwrites the file with a donor's content or skips the row outright.
- If a regular file's `MBFile` keyed archive lacks `ExtendedAttributes` (`com.apple.dataprotection.policy.exception-applied-by = b"com.apple.springboard"`), `BackupAgent2` rejects the row.
- If any ancestor directory in `relativePath` lacks an explicit `flags=2` directory row in `Manifest.db`, the child file is silently ignored.
- Worse, pushing an unverified synthetic partial backup to an already-booted device triggers **"safe state recovery"**, which wipes `HomeDomain` or forces a factory reset loop. (This is why iOS 27+ runs are labelled experimental rather than supported.)

### 4.2 Device Management Backup Restoration Policy (unverified on iOS 27+)
Per Apple's official **Device Management Updates**:
> *"On devices with iOS 27, iPadOS 27, and visionOS 27, devices no longer restore device management information from backup. This includes the enrollment profile, management configuration, and supervision status."*

### 4.3 New iOS 26 Setup Assistant (`purplebuddy`) Panes
Apple added new Setup Assistant panes in iOS 26 (`OSShowcase`, `SafetyAndHandling`, `AgeBasedSafetySettings`, `EnableLockdownMode`, `AdditionalPrivacySettings`, `WebContentFiltering`, `CameraButton`, `Intelligence`, `Tips`, `Welcome`, `RestoreCompleted`, `UpdateCompleted`, etc.).

---

## 5. How `MDMPatcher-Linux v2.8.0` Works (`mdmpatcher/ios26.py`, verified on iOS 9 – 26.x)

To make the bypass reliable on iOS 9 – 26.x and immune to partial-restore factory-reset loops,
**MDMPatcher-Linux** implements a **Dual-Vector Engine** (the same vectors run on experimental
iOS 27+ firmware, behind the unverified-firmware warning):

### 5.1 Vector A: Direct USB `MobileConfigService` (`com.apple.mobile.MCInstall`) + `lockdownd` Push (`ios26_direct_skip_setup`)
- Connects over USB Lockdown directly to `com.apple.mobile.MCInstall` (`MobileConfigService`) and executes `GetCloudConfiguration` followed by `SetCloudConfiguration` with:
  - `SkipSetup`: All **80 canonical setup pane identifiers** (`SKIP_SETUP_KEYS`), including `OSShowcase`, `SafetyAndHandling`, `AgeBasedSafetySettings`, `EnableLockdownMode`, `AdditionalPrivacySettings`, `WebContentFiltering`, `CameraButton`, `Intelligence`, etc.
  - `AllowPairing = True`
  - `ConfigurationWasApplied = True`
  - `CloudConfigurationUIComplete = True`
  - `IsSupervised = False` (or removable supervised mode with `IsMDMUnremovable = False`)
  - `ConfigurationSource = 0`
  - `PostSetupProfileWasInstalled = True`
- Simultaneously sets persistent `lockdownd` completion flags (`com.apple.purplebuddy`: `SetupState = "Finished"`, `buddyComplete = True`, `SkipSetup = True`; `com.apple.mobile.iTunes`: `SetupComplete = True`, `BuddySetupComplete = True`; `com.apple.mobile.data_sync`: `BuddyComplete = True`).
- Because Vector A operates directly over `MCInstall` and `lockdownd`, it **never invokes a partial backup restore**, completely eliminating any risk of triggering the partial-restore reset loop.

### 5.2 Vector B: G1 "Protective Full-Backup $\rightarrow$ MDM Purge & Donor-Cloned Injection $\rightarrow$ 6-Point Verification Gate $\rightarrow$ Controlled Restore" Pipeline
When modifying or restoring a backup with the hardened engine (`--mode ios26`, `--full-backup-g1`, or `patch-backup`):
1. **MDM Profile Purge (`strip_mdm_from_backup`)**:
   Deletes `MDM.plist`, `MDMEvents.plist`, and `ManagingProfile.plist` from `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles/Library/ConfigurationProfiles/` in both `Manifest.db` and the `<aa>/<fileID>` payload store.
2. **Donor-Blob Cloning & Unique Monotonic Inodes (`inject_files_into_ios26_backup`)**:
   - Scans `MAX(InodeNumber)` across `Manifest.db`.
   - Clones a trusted regular-file donor `MBFile` blob from the same domain (or constructs an `MBFile` with `SPRINGBOARD_EXT_ATTR_BYTES`), deep-merges staged plist keys onto any existing backup plist, synthesizes all missing parent directory rows (`flags=2`), and stamps strictly unique monotonic `InodeNumber` values.
3. **6-Point Pre-Restore Verification Gate (`verify_ios26_backup`)**:
   Verifies SQLite integrity, unencrypted `Manifest.plist`, `Status.plist` (`SnapshotState == "finished"`), absence of `MDM.plist`/`MDMEvents.plist`, SHA-1 `Digest` and byte equality on disk, unique positive `InodeNumber` values, and complete parent directory chains before sending a single packet to the device.
4. **Controlled `--no-reboot` Restore + 60s SSL Watchdog + Phase-5 `MCInstall` Sweep**:
   Restores with `reboot=False` (`--no-reboot`), 60-second Lockdown SSL handshake timeout, `G1Watchdog` stall detection, and `_Mobilebackup2NoEscrow` fallback for post-wipe devices lacking `EscrowBag`, followed by Vector A (`ios26_direct_skip_setup`) and a controlled restart.

### 5.3 Companion On-Device Research Helper (`src/c/bad_query_ios26.c`)
For security researchers studying on-device sandbox escapes on iOS 26.0 – 26.6.1 and iOS 27.0b1 – 27.0b4, `src/c/bad_query_ios26.c` provides a reference implementation of the `libsystem_containermanager.dylib` Class-13 (`MCMSharedSystemDataContainer` via `systemgroup.com.apple.mobilegestaltcache` path traversal) sandbox token generator and `fsgetpath` inode scanner targeting `/var/containers/Shared/SystemGroup/<UUID>/Library/ConfigurationProfiles/CloudConfigurationDetails.plist`.
