<div align="center">

<img src="docs/assets/banner.svg" alt="MDMPatcher-Linux Official Banner" width="100%" />

<br />

[![Release](https://img.shields.io/badge/Release-v2.8.0-06b6d4?style=for-the-badge&logo=github)](https://github.com/supermario3D1/mdmpatcher-linux)
[![iOS Support](https://img.shields.io/badge/iOS_%26_iPadOS-9.0_–_26.x-10b981?style=for-the-badge&logo=apple&logoColor=white)](#ios--ipados-compatibility-matrix-ios-9--ios-26x)
[![Fedora Linux](https://img.shields.io/badge/Fedora_Linux-39_–_43%2B_Supported-51a2da?style=for-the-badge&logo=fedora&logoColor=white)](#fedora-linux-workstation--kde--server--silverblue)
[![Ubuntu / Debian](https://img.shields.io/badge/Ubuntu_%2F_Debian-Supported-E95420?style=for-the-badge&logo=ubuntu&logoColor=white)](#ubuntu--debian--linux-mint--pop_os--kali)
[![Arch Linux](https://img.shields.io/badge/Arch_Linux-Supported-1793d1?style=for-the-badge&logo=arch-linux&logoColor=white)](#arch-linux--manjaro--endeavouros--cachyos)
[![License](https://img.shields.io/badge/License-MIT-818cf8?style=for-the-badge)](LICENSE)

<p align="center">
  <strong>The Complete Linux Port &amp; Reverse-Engineered iOS 9 – 26.x Upgrade of <code>MDMPatcher-Universal</code> &amp; <code>MDMPatcher-Enhanced</code></strong><br />
  <a href="#quick-install-one-command">Quick Install</a> •
  <a href="#how-the-engine-works-dual-vector-verified-on-ios-9--26x">Engine</a> •
  <a href="#fedora-linux-workstation--kde--server--silverblue">Fedora Guide</a> •
  <a href="#step-by-step-device-patching-guide">Usage Guide</a> •
  <a href="docs/REVERSE_ENGINEERING.md">Reverse Engineering Docs</a> •
  <a href="docs/index.html">GitHub Pages Site</a>
</p>

</div>

---

## Overview

**MDMPatcher-Linux** is a full-featured Linux port and reverse-engineered modernization of [`fled-dev/MDMPatcher-Enhanced`](https://github.com/fled-dev/MDMPatcher-Enhanced) and [`tazMeah/MDMPatcher-Universal`](https://github.com/tazMeah/MDMPatcher-Universal). Originally restricted to macOS (`IOKit`) and designed around an obfuscated iOS 9.1 backup archive that failed on modern iOS releases, **MDMPatcher-Linux v2.8.0** brings native USB device detection, RNCryptor v3 deobfuscation, and a **Dual-Vector Engine** to **Fedora Linux**, **Ubuntu/Debian**, **Arch Linux**, and **openSUSE**.

> **✅ Supported iOS versions: iOS 9.0 – iOS 26.x.**
> MDMPatcher-Linux is verified on every iOS / iPadOS release from `9.0` through `26.x` (builds `23A+`) — the
> newest iOS currently shipping on supported iPhones and iPads. **Anything below iOS 27 is covered.**
> **iOS 27.0+ (builds `24A+`) is experimental**: the engine still runs, but every command prints an
> `UNVERIFIED FIRMWARE` warning before it touches the device and the outcome is not guaranteed.

### What's New in v2.8.0 (iOS 26.x Support Window & Fedora Edition)

| Capability | Original `MDMPatcher-Universal` | `MDMPatcher-Linux v2.8.0` |
| :--- | :--- | :--- |
| **Host OS Support** | macOS only (`IOKit` hardcoded PIDs `0x12a8`/`0x12ab`) | **Linux Native** (**Fedora**, Ubuntu, Debian, Arch, openSUSE) via `usbmuxd` + `udev` |
| **iOS 9 – 14 Support** | `HomeDomain/Library/ConfigurationProfiles/*` | Full support (`enhanced` & `legacy` modes) |
| **iOS 15 – 26.x Support** | ❌ Fails (missing `SysSharedContainerDomain` & `--system`) | ✅ **Verified** (`SysSharedContainerDomain` + `ManagedPreferencesDomain` + `--system --skip-apps`) |
| **iOS 27.0+ (`24A+`) Support** | ❌ Fails / Partial restore triggers Safe-State Recovery | ⚠️ **Experimental** — engine runs, `UNVERIFIED FIRMWARE` warning printed first: Vector A (`MCInstall` Direct 80-Pane Push) + Vector B (G1 Donor-Cloned `Manifest.db` + 6-Point Verification Gate) |
| **Legacy Archive Bugs** | ❌ Renames iPhone to `"“iMyFone”的 iPhone"`, sets `zh_CN` locale & `10B329` build | ✅ Cleaned device name, `en_US` locale, `SetupVersion` = the device's own iOS major (min `26`), and synced `LastMigratedBuild` |
| **Crypto Dependency** | macOS Swift `RNCryptor` framework | Zero external crypto packages — uses system OpenSSL `libcrypto.so.3` (`/lib64/libcrypto.so.3` on Fedora) |

---

## How the Engine Works (Dual-Vector, Verified on iOS 9 – 26.x)

<div align="center">
  <img src="docs/assets/ios26-architecture.svg" alt="MDMPatcher-Linux Dual-Vector Architecture" width="100%" />
</div>

On modern iOS — **verified through iOS 26.x** (builds `23A+`), and best-effort above it — Apple introduced two major changes that broke traditional SparseRestore/BookRestore and partial-backup tools:
1. **`BackupAgent2` Safe-State Recovery & Strict `Manifest.db` Validation**: Pushing a synthetic partial backup lacking valid `ExtendedAttributes`, parent directory rows, or unique non-zero `InodeNumber` values causes `BackupAgent2` to either drop the injected files or trigger **"safe state recovery"** (which loops Setup Assistant or forces a factory reset).
2. **Device Management Backup Restoration Policy**: Per Apple's deployment documentation, current iOS devices no longer trust stale MDM enrollment state from a synthetic backup alone.

**MDMPatcher-Linux v2.8.0** solves both restrictions using two complementary vectors (`mdmpatcher/ios26.py`):

### ⚡ Vector A: Direct USB `MobileConfigService` (`com.apple.mobile.MCInstall`) + `lockdownd` Push
- Connects over USB Lockdown directly to `com.apple.mobile.MCInstall` (`MobileConfigService`) and executes `SetCloudConfiguration` with all **80 canonical `SkipSetup` pane identifiers** (including `OSShowcase`, `SafetyAndHandling`, `AgeBasedSafetySettings`, `EnableLockdownMode`, `AdditionalPrivacySettings`, `WebContentFiltering`, `CameraButton`, and `Intelligence`), `CloudConfigurationUIComplete = True`, `ConfigurationWasApplied = True`, `IsSupervised = False`, and `PostSetupProfileWasInstalled = True`.
- Simultaneously writes persistent `lockdownd` completion flags (`com.apple.purplebuddy`: `SetupState = "Finished"`, `buddyComplete = True`, `SkipSetup = True`; `com.apple.mobile.iTunes`: `SetupComplete = True`, `BuddySetupComplete = True`).
- **Zero Partial-Restore Risk**: Can be run standalone via `./mdmpatcher.py skip-setup` without performing any backup restore at all.

### 🛡️ Vector B: G1 Donor-Cloned `Manifest.db` Pipeline + 6-Point Verification Gate
- **MDM Profile Purge**: Strips active `MDM.plist`, `MDMEvents.plist`, and `ManagingProfile.plist` from `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles/Library/ConfigurationProfiles/`.
- **Donor-Blob Cloning & Unique Monotonic Inodes**: Clones trusted `MBFile` blobs from the backup, attaches `SPRINGBOARD_EXT_ATTR_BYTES` (`com.apple.dataprotection.policy.exception-applied-by = com.apple.springboard`), synthesizes all ancestor directory rows (`flags=2`), and stamps strictly unique monotonic `InodeNumber` values so `BackupAgent2`'s inode-deduplication pass accepts every file.
- **6-Point Pre-Restore Verification Gate (`verify_ios26_backup`)**: Verifies SQLite integrity, unencrypted `Manifest.plist`, `Status.plist` (`SnapshotState == "finished"`), SHA-1 `Digest` equality on disk, unique positive inodes, and parent directory chains **before** sending a single byte to the device.
- **Controlled `--no-reboot` Restore + 60s SSL Watchdog**: Restores with `--no-reboot` (`reboot=False`), a 60-second Lockdown SSL handshake timeout, `G1Watchdog` stall protection, and `_Mobilebackup2NoEscrow` fallback for freshly wiped devices, followed automatically by Vector A and a controlled restart.

---

## iOS & iPadOS Compatibility Matrix (iOS 9 – iOS 26.x)

| iOS / iPadOS Version | Build Numbers | Recommended Command | Active Engine | Status |
| :--- | :--- | :--- | :--- | :---: |
| **iOS / iPadOS 26.0 – 26.x** *(latest iOS)* | `23A+ – 23G+` | `./mdmpatcher.py patch` | `enhanced` (default) + `MCInstall`, or `ios26` | ✅ **Verified** |
| **iOS / iPadOS 27.0+** | `24A335+` | `./mdmpatcher.py patch` *(auto-selects `ios26`)* or `./mdmpatcher.py skip-setup` | Vector A (`MCInstall`) + Vector B (G1 `Manifest.db`) | ⚠️ **Experimental** |
| **iOS / iPadOS 17.0 – 18.6** | `21A+ – 22G+` | `./mdmpatcher.py patch` | `enhanced` (`SysSharedContainerDomain` + `ManagedPreferencesDomain`) | ✅ **Supported** |
| **iOS / iPadOS 15.0 – 16.7.x** | `19A+ – 20H+` | `./mdmpatcher.py patch` | `enhanced` / `modern` | ✅ **Supported** |
| **iOS / iPadOS 9.0 – 14.8.1** | `13A+ – 18H+` | `./mdmpatcher.py patch` | `enhanced` / `legacy` (`Manifest.mbdb`) | ✅ **Supported** |

---

## iOS Support Policy (Verified: iOS 9 – iOS 26.x)

| Range | Status | Behaviour |
| :--- | :--- | :--- |
| **iOS / iPadOS 9.0 – 26.x** (builds `23A+` and earlier) | ✅ **Verified** | Fully supported — every iPhone/iPad on the current iOS release is covered. `enhanced` mode by default. |
| **iOS / iPadOS 27.0+** (builds `24A+`) | ⚠️ **Experimental** | The engine still runs, but every command prints an `UNVERIFIED FIRMWARE` warning before touching the device. |
| **Below iOS 9.0** | ⚠️ **Best-effort** | Use `legacy` mode. |

The policy lives in one place — `mdmpatcher/ios26.py`:

```python
MIN_SUPPORTED_IOS_MAJOR = 9
MAX_SUPPORTED_IOS_MAJOR = 26
SUPPORTED_IOS_RANGE = "iOS 9.0 – iOS 26.x"

ios_support_state(product_version, build_version)   # "verified" | "unverified" | "below"
unsupported_ios_notice(product_version, build_version)
warn_if_unsupported_ios(product_version, build_version)  # prints + returns True on iOS 27+
```

Every command that touches a device (`patch`, `skip-setup`, `patch-backup`, `build-backup`,
`info`) calls `warn_if_unsupported_ios()` first, so an iOS 27+ run is **warned about, never
silently assumed to be supported** — and never hard-blocked, because the `MCInstall` and G1
pipeline code paths are the ones most likely to keep working on newer firmware.

> **v2.8.0 breaking rename.** The engine previously branded `ios27` is now `ios26`, matching the
> verified maximum: `mdmpatcher/ios27.py` → `mdmpatcher/ios26.py`, `--mode ios27` → `--mode ios26`,
> `src/c/bad_query_ios27.c` → `src/c/bad_query_ios26.c`, and the helpers were renamed to match
> (`verify_ios27_backup` → `verify_ios26_backup`, `patch_ios27_existing_backup` →
> `patch_ios26_existing_backup`, `is_ios27_or_newer` → `is_above_supported_ios`, and so on).
> Update any scripts or imports that referenced the old names.

---

## Installation Instructions (Linux)

### Quick Install (One Command — Auto-Detects Fedora, Ubuntu, Debian, Arch, openSUSE)

```bash
git clone https://github.com/supermario3D1/mdmpatcher-linux.git
cd mdmpatcher-linux
./scripts/install.sh --venv
source .venv/bin/activate
```

The `./scripts/install.sh` script automatically:
1. Detects your Linux distribution (`dnf`, `rpm-ostree`, `apt`, `pacman`, or `zypper`) and installs `usbmuxd`, `libimobiledevice`, `openssl`, `python3`, and Tkinter.
2. Installs `/etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules` so non-root users can communicate with iPhones/iPads in Normal, Recovery, and Composite USB modes without `Permission denied` errors.
3. Starts the `usbmuxd` systemd service.
4. Creates a `.venv` virtual environment with `pymobiledevice3` (used for Direct `MCInstall` communication) and compiles the optional native C binary (`src/c/mdm_patch`).

---

### Fedora Linux (Workstation / KDE / Server / Silverblue)

**MDMPatcher-Linux** treats **Fedora Linux (Fedora 39, 40, 41, 42, 43, Rawhide)** as well as Fedora derivatives (**Nobara**, **Ultramarine**, **Bazzite**, **RHEL / AlmaLinux / Rocky Linux**) as first-class targets, including native lookup of `/lib64/libcrypto.so.3`.

#### Standard Fedora (`dnf`)
```bash
# 1. Install required Fedora packages
sudo dnf install -y \
  usbmuxd \
  libimobiledevice \
  libimobiledevice-utils \
  libplist \
  openssl \
  openssl-libs \
  python3 \
  python3-pip \
  python3-tkinter \
  gcc \
  make \
  pkgconf-pkg-config \
  libimobiledevice-devel \
  libimobiledevice-glue-devel \
  libplist-devel \
  openssl-devel \
  libzip-devel \
  readline-devel

# 2. Install Apple USB udev rules and reload udev + usbmuxd
sudo cp scripts/39-usbmuxd-mdmpatcher.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac
sudo systemctl start usbmuxd.service

# 3. Install Python USB backend (recommended for Direct MCInstall support)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[usb]"

# 4. (Optional) Compile the native C binary
make build-c
```

#### Fedora Atomic / Silverblue / Kinoite / Bazzite (`rpm-ostree`)
```bash
sudo rpm-ostree install --idempotent --allow-inactive \
  usbmuxd libimobiledevice libimobiledevice-utils python3 python3-tkinter openssl
sudo cp scripts/39-usbmuxd-mdmpatcher.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[usb]"
```

---

### Ubuntu / Debian / Linux Mint / Pop!_OS / Kali

```bash
sudo apt update
sudo apt install -y \
  usbmuxd \
  libimobiledevice6 \
  libimobiledevice-utils \
  ideviceinstaller \
  libplist-utils \
  openssl \
  python3 \
  python3-pip \
  python3-venv \
  python3-tk \
  build-essential \
  pkg-config \
  libimobiledevice-dev \
  libplist-dev \
  libssl-dev \
  libzip-dev \
  libreadline-dev

sudo cp scripts/39-usbmuxd-mdmpatcher.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo systemctl start usbmuxd.service

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[usb]"
```

---

### Arch Linux / Manjaro / EndeavourOS / CachyOS

```bash
sudo pacman -Sy --needed \
  usbmuxd \
  libimobiledevice \
  libplist \
  openssl \
  libzip \
  readline \
  python \
  python-pip \
  tk \
  base-devel \
  pkgconf

sudo cp scripts/39-usbmuxd-mdmpatcher.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo systemctl start usbmuxd.service

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[usb]"
```

---

### openSUSE Tumbleweed / Leap

```bash
sudo zypper install -y \
  usbmuxd \
  libimobiledevice-tools \
  python3 \
  python3-pip \
  python3-tk \
  gcc \
  make \
  pkg-config \
  libimobiledevice-devel \
  libplist-devel \
  libopenssl-devel \
  libzip-devel \
  readline-devel
```

---

## Step-by-Step Device Patching Guide

### Step 1: Prepare the iPhone or iPad
1. Ensure the device is freshly erased/restored on the **Setup Assistant ("Hello")** screen (or unlocked on the Home Screen with **Find My iPhone Disabled**).
2. Connect the iPhone or iPad to your Linux machine with a data-capable USB cable.
3. If prompted on the device screen with **"Trust This Computer?"**, tap **Trust** and enter your passcode if applicable.
4. Verify your Linux machine sees the device:
   ```bash
   ./mdmpatcher.py info
   ```

### Step 2: Run the Patcher

#### Standard One-Command Auto-Patch (iOS 9 – iOS 26.x)
Automatically detects your iOS version and uses `enhanced` mode on iOS 9–26.x, or the hardened `ios26`
mode when the device reports firmware above the verified maximum (iOS 27+, builds `24A+`) — printing an
unverified-firmware warning first:
```bash
./mdmpatcher.py patch
```

#### Vector A Only (Direct USB `MCInstall` + `lockdownd` SkipSetup — Zero Backup Restore)
If you are on the Setup Assistant screen and want to bypass Remote Management & all 80 Setup Assistant panes **without running any backup restore**:
```bash
./mdmpatcher.py skip-setup
```

#### Vector B Full Protective G1 Route (Full Backup $\rightarrow$ MDM Strip & Donor Injection $\rightarrow$ Verified Restore)
Takes a real unencrypted backup of your connected device, strips `MDM.plist` & `MDMEvents.plist`, injects donor-cloned payloads with unique inodes, passes the 6-point verification gate, restores with `--no-reboot`, runs Vector A, and restarts:
```bash
./mdmpatcher.py patch --full-backup-g1
```

#### Also Block OTA Software Updates
```bash
./mdmpatcher.py patch --block-ota
```

#### Launch the Linux Desktop GUI (or Interactive Terminal UI)
```bash
./mdmpatcher.py gui
```

### Step 3: On-Device Step After Reboot (iOS 17.2, iOS 18 & iOS 26.x)
> **IMPORTANT (`iOS 17.2 – iOS 26.x`, and experimental on iOS 27+)**:
> After the device reboots, Apple's Setup Assistant may show a screen titled **"iPhone Partially Set Up"**.
> - Tap **"Continue with Partial Setup"** (the smaller text button at the bottom of the screen).
> - **DO NOT** tap the blue *"Finish Setting Up"* button (which would re-enter the Wi-Fi / Remote Management flow).
> - Your device will immediately transition to the iOS Home Screen with Remote Management bypassed.

---

## Offline Backup Generation, Deobfuscation & Inspection

You can generate, patch, deobfuscate, and inspect backups offline without a connected iOS device:

```bash
# 1. Deobfuscate MDMPatcher-Universal's encrypted assets (extension1.pdf, extension2.pdf, libiMobileeDevice.dylib)
./mdmpatcher.py deobfuscate -a assets -o assets/deobfuscated

# 2. Build a BackupAgent2-hardened backup offline (verified target: iOS 26.x)
./mdmpatcher.py build-backup \
  --output ./my_ios26_backup \
  --mode ios26 \
  --udid 00008130-0011223344556677 \
  --serial G6TZ1234ABCD \
  --product-type iPhone17,2 \
  --build-version 23A345 \
  --ios-version 26.0 \
  --block-ota

# 3. Strip MDM.plist/MDMEvents.plist and inject the bypass payloads into an existing Manifest.db backup
./mdmpatcher.py patch-backup ./my_ios26_backup/MDMB --block-ota

# 4. Inspect any backup directory (Manifest.mbdb or Manifest.db)
./mdmpatcher.py inspect ./my_ios26_backup
```

---

## Repository Layout

```text
mdmpatcher-linux/
├── assets/                          # Obfuscated & clean reference templates from MDMPatcher-Universal/Enhanced
├── docs/
│   ├── index.html                   # Official GitHub Pages interactive documentation site
│   ├── REVERSE_ENGINEERING.md       # Full RNCryptor v3, Manifest.mbdb, Manifest.db & modern iOS analysis
│   └── assets/                      # Official SVG logo, banner, and engine architecture diagram
├── mdmpatcher/
│   ├── crypto.py                    # Reverse-engineered RNCryptor v3 + 6-pair byte swapper (libcrypto.so.3)
│   ├── mbdb.py                      # Binary parser, editor & builder for Manifest.mbdb (Backup v2.4)
│   ├── manifest_db.py               # SQLite3 Manifest.db builder with ExtendedAttributes & unique inodes
│   ├── profiles.py                  # iOS 15–26.x CloudConfigurationDetails & 80-pane SkipSetup generators
│   ├── ios26.py                     # Dual-Vector Engine (MCInstall + G1 Donor-Cloned Pipeline + 6-Point Gate) + support window policy
│   ├── patcher.py                   # Unified backup builder (enhanced, ios26, modern, sqlite, legacy)
│   ├── device.py                    # Linux USB device detector & MobileBackup2 restore engine
│   └── cli.py                       # CLI, Tkinter GUI & interactive TUI
├── scripts/
│   ├── install.sh                   # Cross-distro installer (Fedora, Ubuntu/Debian, Arch, openSUSE)
│   └── 39-usbmuxd-mdmpatcher.rules  # Apple USB udev rules for non-root Linux access
├── src/c/                           # Native C port (main.c, patch_logic.c, idevicebackup2.c, bad_query_ios26.c)
└── tests/
    └── test_mdmpatcher.py           # Unit & integration test suite
```

---

## Running the Test Suite

```bash
make test
# or directly:
python3 -m unittest discover -s tests -v
```

---

## Disclaimer & License

Released under the [MIT License](LICENSE). This project is provided strictly for educational, interoperability, security research, and authorized device recovery purposes on devices you legally own or have explicit permission to test.
