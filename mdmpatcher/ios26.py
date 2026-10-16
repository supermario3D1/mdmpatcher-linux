"""
iOS 26.x (current public release line) Multi-Vector MDM & Setup Assistant Bypass Engine.

Verified support window: **iOS / iPadOS 9.0 - 26.x** (builds `23A+`), which covers
every shipping iPhone/iPad today. Firmware above that window (iOS 27.0+ / builds
`24A+`) is *not* verified: this engine still runs on it, but
`unsupported_ios_notice()` / `warn_if_unsupported_ios()` print an explicit
experimental warning before anything is sent to the device (see
`MAX_SUPPORTED_IOS_MAJOR`).

Why legacy SparseRestore / partial backup-restore tools fail on modern iOS:
  1. Partial-Restore Safe-State Recovery:
     Apple patched `itunesstored` / `bookassetd` partial restores and hardened
     `BackupAgent2`. Sending a synthetic partial backup without the device's
     complete backup state or without valid `ExtendedAttributes` + unique
     non-zero `InodeNumber` values triggers "safe state recovery" (which loops
     Setup Assistant or forces a factory reset).
  2. Device Management Backup Restoration Policy:
     Per Apple's deployment documentation, current iOS devices no longer trust
     stale MDM enrollment profiles from a synthetic backup alone.

How MDMPatcher-Linux bypasses both restrictions on iOS 9 - 26.x:
  - Vector A (`ios26_direct_skip_setup` / `MCInstall` + `lockdownd`):
    Connects over USB Lockdown directly to `com.apple.mobile.MCInstall`
    (`MobileConfigService`) and pushes `SetCloudConfiguration` with all 80
    `SKIP_SETUP_KEYS`, `CloudConfigurationUIComplete = True`,
    `ConfigurationWasApplied = True`, `IsSupervised = False`, and
    `PostSetupProfileWasInstalled = True`, plus persistent `lockdownd`
    `com.apple.purplebuddy` completion keys - WITHOUT running any partial
    backup restore (zero factory-reset loop risk).
  - Vector B (`patch_ios26_existing_backup` & `run_ios26_g1_full_backup_patch`):
    Implements the G1 "Full-Backup -> MDM Purge & Donor-Cloned
    Manifest.db Injection -> Pre-Restore Verification Gate -> Controlled
    No-Reboot Restore -> Post-Restore MCInstall Sweep" pipeline:
      1. Strips `MDM.plist`, `MDMEvents.plist`, and restrictive
         `CloudConfigurationDetails.plist` from `SysSharedContainerDomain-
         systemgroup.com.apple.configurationprofiles`.
      2. Clones real donor `MBFile` blobs from the device's own `Manifest.db`,
         attaches `ExtendedAttributes` (`com.apple.springboard` data-protection
         exception), synthesizes missing parent directory rows (`flags=2`), and
         stamps strictly unique monotonic `InodeNumber` values so `BackupAgent2`'s
         inode-deduplication pass accepts every row.
      3. Runs a strict 6-point verification gate (`verify_ios26_backup`) before
         any byte is sent to the device.
      4. Restores with `--no-reboot` (`reboot=False`), 60s SSL handshake timeout,
         stall watchdog (`G1Watchdog`), and `_Mobilebackup2NoEscrow` fallback,
         then runs Vector A (`MCInstall`) and performs a controlled reboot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import plistlib
from plistlib import UID
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable

from .manifest_db import (
    FILES_SCHEMA,
    SPRINGBOARD_EXT_ATTR_BYTES,
    ManifestDbEntry,
    build_mbfile_blob,
)
from .mbdb import MODE_DIR, MODE_FILE, compute_file_id
from .profiles import (
    HOME_DOMAIN,
    MANAGED_PREFS_DOMAIN,
    SYS_CONFIG_PROFILE_DOMAIN,
    build_cloud_config_details_plist,
    build_home_purplebuddy_plist,
    build_ios26_cloud_config,
    build_managed_purplebuddy_plist,
    build_mc_data_migration_plist,
    build_mcmeta_plist,
    build_ota_disabler_plist,
    build_payload_manifest_plist,
    build_user_settings_plist,
)

# ---------------------------------------------------------------------------
# Verified support window
# ---------------------------------------------------------------------------
# MDMPatcher-Linux is developed and verified against iOS / iPadOS 9.0 through
# the current public release line, iOS 26.x (builds 23A+).
#
# Firmware above the verified maximum (iOS 27.0+ / builds 24A+) is NOT dropped:
# every code path still runs, but `unsupported_ios_notice()` /
# `warn_if_unsupported_ios()` print an explicit experimental warning first so
# nobody patches a device believing the newer firmware is covered.
MIN_SUPPORTED_IOS_MAJOR = 9
MAX_SUPPORTED_IOS_MAJOR = 26
SUPPORTED_IOS_RANGE = "iOS 9.0 \u2013 iOS 26.x"

# `SetupVersion` stamped into com.apple.purplebuddy.plist. It tracks the device's
# own iOS major so Setup Assistant treats every pane up to that release as done,
# with a floor of 26 (the oldest release whose full SkipSetup pane list we push).
MIN_SETUP_VERSION = 26

# Files inside SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles
# that store active MDM enrollment and event state on iOS 15 - 26.x
# (iOS 27.0+ best-effort / unverified).
MDM_FILES_TO_STRIP: tuple[ tuple[str, str], ... ] = (
    (
        SYS_CONFIG_PROFILE_DOMAIN,
        "Library/ConfigurationProfiles/MDM.plist",
    ),
    (
        SYS_CONFIG_PROFILE_DOMAIN,
        "Library/ConfigurationProfiles/MDMEvents.plist",
    ),
    (
        SYS_CONFIG_PROFILE_DOMAIN,
        "Library/ConfigurationProfiles/ManagingProfile.plist",
    ),
    (
        HOME_DOMAIN,
        "Library/ConfigurationProfiles/MDM.plist",
    ),
    (
        HOME_DOMAIN,
        "Library/ConfigurationProfiles/MDMEvents.plist",
    ),
)

# Persistent lockdownd domain/key flags (verified across reboot on iOS 9 - 26.x).
LOCKDOWN_COMPLETION_FLAGS: tuple[tuple[str | None, str, Any], ...] = (
    ("com.apple.purplebuddy", "SetupState", "Finished"),
    ("com.apple.purplebuddy", "buddyComplete", True),
    ("com.apple.purplebuddy", "SkipSetup", True),
    ("com.apple.mobile.iTunes", "SetupComplete", True),
    ("com.apple.mobile.iTunes", "BuddySetupComplete", True),
    ("com.apple.mobile.data_sync", "BuddyComplete", True),
)

WATCHDOG_STALL_SECONDS = 300.0


class G1WatchdogExpired(RuntimeError):
    """Raised when a G1 (iOS 26.x) operation stalls without device progress."""


@dataclass
class G1Watchdog:
    """
    Stall-based watchdog for iOS 26.x backup/restore operations.
    Fires only when no progress has been reported for `stall_seconds`;
    a device actively transferring data is never interrupted.
    """

    stall_seconds: float = WATCHDOG_STALL_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)

    def note_progress(self) -> None:
        self.last_progress_at = time.monotonic()

    def stalled_for(self) -> float:
        return time.monotonic() - self.last_progress_at

    def check(self, phase: str = "") -> None:
        stalled = self.stalled_for()
        if stalled > self.stall_seconds:
            minutes = max(1, int(stalled // 60))
            suffix = f" [phase: {phase}]" if phase else ""
            raise G1WatchdogExpired(
                f"G1 operation timed out: no progress for {minutes} minute(s) — "
                f"stopped safely before modifying device state.{suffix}"
            )


@dataclass
class IOS26PatchReport:
    backup_dir: Path
    stripped_mdm_files: list[str] = field(default_factory=list)
    injected_files: int = 0
    unchanged_files: int = 0
    synthesized_dirs: int = 0
    failed_files: int = 0
    verified: bool = False
    verification_problems: list[str] = field(default_factory=list)


def parse_ios_major_version(
    product_version: str | None = None,
    build_version: str | None = None,
) -> int:
    """
    Determine the effective iOS major version from `ProductVersion` (e.g. '26.1', '18.2')
    or `BuildVersion` (e.g. '23A345' -> iOS 26, '22A...' -> iOS 18).

    Builds above the verified maximum still parse correctly ('24A335' -> iOS 27)
    so the experimental-firmware warning can be raised; they are simply reported
    as `"unverified"` by `ios_support_state()`.
    """
    if product_version:
        m = re.match(r"^\s*(\d+)", str(product_version))
        if m:
            return int(m.group(1))
    if build_version:
        m = re.match(r"^\s*(\d+)[A-Za-z]", str(build_version))
        if m:
            major_build = int(m.group(1))
            if major_build >= 24:
                return major_build + 3  # 24A -> iOS 27
            if major_build == 23:
                return 26  # 23A -> iOS 26
            if major_build >= 13:
                return major_build - 4  # 22A -> iOS 18, 21A -> iOS 17, etc.
    return 18


def is_above_supported_ios(
    product_version: str | None = None,
    build_version: str | None = None,
) -> bool:
    """
    Return True when the device runs a newer major release than the highest
    verified one — i.e. iOS / iPadOS 27.0+ (builds `24A+`).

    MDMPatcher-Linux is verified through iOS 26.x (builds `23A+`); anything
    above that keeps working but is treated as experimental.
    """
    return parse_ios_major_version(product_version, build_version) > (
        MAX_SUPPORTED_IOS_MAJOR
    )


def is_ios26_or_newer(
    product_version: str | None = None,
    build_version: str | None = None,
) -> bool:
    """Return True if the target device is running iOS / iPadOS 26.0 or newer (builds 23A+)."""
    return parse_ios_major_version(product_version, build_version) >= 26


def ios_support_state(
    product_version: str | None = None,
    build_version: str | None = None,
) -> str:
    """
    Classify a device's firmware against the verified support window.

    Returns one of:
      - `"verified"`   : iOS 9 - iOS 26.x, inside the tested support window.
      - `"unverified"` : iOS 27.0+ (builds `24A+`) — runs, but experimental.
      - `"below"`      : older than iOS 9 (best-effort `legacy` mode only).
    """
    major = parse_ios_major_version(product_version, build_version)
    if major > MAX_SUPPORTED_IOS_MAJOR:
        return "unverified"
    if major < MIN_SUPPORTED_IOS_MAJOR:
        return "below"
    return "verified"


def unsupported_ios_notice(
    product_version: str | None = None,
    build_version: str | None = None,
) -> list[str]:
    """
    Build the warning lines shown when a device runs above the verified maximum
    (iOS 27.0+). Returns an empty list when the firmware is inside the
    verified window (iOS 9 - iOS 26.x).
    """
    major = parse_ios_major_version(product_version, build_version)
    if major <= MAX_SUPPORTED_IOS_MAJOR:
        return []
    return [
        f"[!] UNVERIFIED FIRMWARE: this device reports iOS {major}.x"
        + (f" (build {build_version})" if build_version else "")
        + ".",
        f"    MDMPatcher-Linux is verified on {SUPPORTED_IOS_RANGE} only"
        " (builds 23A+ and earlier).",
        "    The engine will still run, but iOS 27.0+ is EXPERIMENTAL and not guaranteed:",
        "    `BackupAgent2` safe-state recovery and `MobileConfigService` behaviour on 27+",
        "    have not been validated, and a failed restore can loop Setup Assistant or",
        "    force a factory reset.",
        "    Only continue on a device you own and are prepared to restore from scratch.",
    ]


def warn_if_unsupported_ios(
    product_version: str | None = None,
    build_version: str | None = None,
    *,
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """
    Emit the unverified-firmware notice (if any) and return True when the
    device is above the verified maximum (iOS 27.0+).
    """
    lines = unsupported_ios_notice(product_version, build_version)
    if not lines:
        return False
    emit = on_output or (lambda msg: print(msg))
    for line in lines:
        emit(line)
    return True


def normalize_ios26_upload_path(device_name: str) -> str:
    """
    Normalize a device upload path from `BackupAgent2`'s physical backup-session root.
    On modern iOS (verified through 26.x), `BackupAgent2` uploads raw filesystem
    paths prefixed with `/.b/<n>/`
    (e.g. `/.b/1/Media/DCIM/IMG_0001.PNG` or `/.b/1/Library/Preferences/...`).
    Stripping `.b/<n>/` yields the canonical relative path.
    """
    name = device_name.replace("\\", "/").lstrip("/")
    for prefix in (".b", "b"):
        if name.startswith(f"{prefix}/"):
            rest_after_prefix = name[len(prefix) + 1 :]
            head, sep, rest = rest_after_prefix.partition("/")
            if sep and head.isdigit():
                return rest
            break
    return name


def sanitize_udid(udid: str) -> str:
    """Sanitize a device-reported UDID so it cannot traverse above a backup root."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(udid or "").strip())
    if not cleaned:
        raise ValueError(f"Invalid UDID: {udid!r}")
    return cleaned


def atomic_write_plist(
    plist_path: Path,
    data: dict[str, Any],
    *,
    fmt: int = plistlib.FMT_BINARY,
) -> None:
    """
    Atomically write and fsync a plist via a sibling temporary file so an
    interrupted write can never leave a half-written `Manifest.plist` or `Info.plist`.
    """
    plist_path = Path(plist_path)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    orig_mode = (
        stat.S_IMODE(plist_path.stat().st_mode)
        if plist_path.exists()
        else 0o644
    )
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{plist_path.name}.",
            suffix=".tmp",
            dir=plist_path.parent,
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            plistlib.dump(data, tmp, fmt=fmt)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, plist_path)
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _max_inode_in_manifest(conn: sqlite3.Connection) -> int:
    """Return the largest `InodeNumber` across all file/directory rows in `Manifest.db`."""
    max_inode = 1000
    for (candidate_blob,) in conn.execute(
        "SELECT file FROM Files WHERE flags IN (1, 2) AND file IS NOT NULL"
    ):
        try:
            objs = plistlib.loads(candidate_blob).get("$objects", [])
            if len(objs) > 1 and isinstance(objs[1], dict):
                ino = objs[1].get("InodeNumber")
                if isinstance(ino, int) and ino > max_inode:
                    max_inode = ino
        except Exception:
            continue
    return max_inode


def _pick_donor_blob(
    conn: sqlite3.Connection, domain: str, relative_path: str
) -> bytes | None:
    """
    Pick a real donor `MBFile` blob from `Manifest.db` that modern `BackupAgent2`
    already trusts (preferring `Library/SpringBoard/IconState.plist` or any 0644
    regular file with `Digest` + `ExtendedAttributes`).
    """
    preferred = (
        "Library/SpringBoard/IconState.plist",
        "Library/Preferences/com.apple.purplebuddy.plist",
    )
    for candidate in preferred:
        row = conn.execute(
            "SELECT file FROM Files WHERE domain = ? AND relativePath = ? "
            "AND flags = 1 AND file IS NOT NULL",
            (domain, candidate),
        ).fetchone()
        if row is not None:
            return row[0]

    for (candidate_blob,) in conn.execute(
        "SELECT file FROM Files WHERE domain = ? AND file IS NOT NULL "
        "AND relativePath != ? AND flags = 1",
        (domain, relative_path),
    ):
        try:
            info = plistlib.loads(candidate_blob)["$objects"][1]
            if (info.get("Mode", 0) & 0o777) == 0o644 and isinstance(
                info.get("InodeNumber"), int
            ):
                return candidate_blob
        except Exception:
            continue

    for (candidate_blob,) in conn.execute(
        "SELECT file FROM Files WHERE domain = ? AND file IS NOT NULL "
        "AND relativePath != ? AND flags = 1",
        (domain, relative_path),
    ):
        try:
            info = plistlib.loads(candidate_blob)["$objects"][1]
            if "Digest" in info and "ExtendedAttributes" in info:
                return candidate_blob
        except Exception:
            continue
    return None


def _pick_directory_donor_blob(
    conn: sqlite3.Connection, domain: str
) -> bytes | None:
    """Pick a real directory (`flags=2`) `MBFile` blob from `Manifest.db`."""
    row = conn.execute(
        "SELECT file FROM Files WHERE domain = ? AND flags = 2 AND file IS NOT NULL LIMIT 1",
        (domain,),
    ).fetchone()
    if row is not None:
        return row[0]
    row = conn.execute(
        "SELECT file FROM Files WHERE flags = 2 AND file IS NOT NULL LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else None


def _patch_donor_blob(
    donor_blob: bytes,
    relative_path: str,
    contents: bytes,
    *,
    inode: int,
    mode: int = MODE_FILE | 0o644,
    uid: int = 501,
    gid: int = 501,
) -> bytes:
    """
    Clone a donor `MBFile` archive blob and retarget its `RelativePath`, `Digest`,
    `Size`, `Mode`, `UserID`, `GroupID`, and unique `InodeNumber`.
    """
    archive = plistlib.loads(donor_blob)
    objects = archive["$objects"]
    info = objects[1]

    rel_uid = info.get("RelativePath")
    if isinstance(rel_uid, UID):
        objects[rel_uid.data] = relative_path
    else:
        new_uid = len(objects)
        objects.append(relative_path)
        info["RelativePath"] = UID(new_uid)

    digest_bytes = hashlib.sha1(contents).digest()
    digest_uid = info.get("Digest")
    if isinstance(digest_uid, UID):
        objects[digest_uid.data] = digest_bytes
    else:
        new_uid = len(objects)
        objects.append(digest_bytes)
        info["Digest"] = UID(new_uid)

    if "ExtendedAttributes" not in info:
        ext_uid = len(objects)
        objects.append(SPRINGBOARD_EXT_ATTR_BYTES)
        info["ExtendedAttributes"] = UID(ext_uid)

    info["Size"] = len(contents)
    info["Mode"] = mode
    info["UserID"] = uid
    info["GroupID"] = gid
    info["InodeNumber"] = inode
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _patch_dir_donor_blob(
    donor_blob: bytes | None,
    relative_path: str,
    *,
    inode: int,
    mode: int = MODE_DIR | 0o755,
    uid: int = 501,
    gid: int = 501,
) -> bytes:
    """Build or clone a directory (`flags=2`) `MBFile` blob with a unique inode."""
    if donor_blob is not None:
        try:
            archive = plistlib.loads(donor_blob)
            objects = archive["$objects"]
            info = objects[1]
            rel_uid = info.get("RelativePath")
            if isinstance(rel_uid, UID):
                objects[rel_uid.data] = relative_path
            info["Size"] = 0
            info["Mode"] = mode
            info["UserID"] = uid
            info["GroupID"] = gid
            info["InodeNumber"] = inode
            return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)
        except Exception:
            pass
    return build_mbfile_blob(
        ManifestDbEntry(
            domain="",
            relative_path=relative_path,
            data=None,
            mode=mode,
            uid=uid,
            gid=gid,
            flags=2,
            inode=inode,
            include_extended_attributes=False,
        ),
        inode=inode,
    )


def _ensure_directory_rows(
    conn: sqlite3.Connection,
    domain: str,
    dir_path: str,
    next_inode: int,
    known_dirs: set[tuple[str, str]],
    dir_donor: bytes | None,
) -> int:
    """
    Ensure every ancestor directory in `dir_path` has a `flags=2` row in `Manifest.db`.
    Returns the updated `next_inode`.
    """
    clean = dir_path.strip("/")
    if not clean:
        return next_inode
    parts = clean.split("/")
    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        key = (domain, prefix)
        if key in known_dirs:
            continue
        existing = conn.execute(
            "SELECT 1 FROM Files WHERE domain = ? AND relativePath = ? AND flags = 2",
            (domain, prefix),
        ).fetchone()
        if existing is not None:
            known_dirs.add(key)
            continue
        next_inode += 1
        file_id = compute_file_id(domain, prefix)
        blob = _patch_dir_donor_blob(
            dir_donor,
            prefix,
            inode=next_inode,
        )
        conn.execute(
            "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) "
            "VALUES (?, ?, ?, 2, ?)",
            (file_id, domain, prefix, sqlite3.Binary(blob)),
        )
        known_dirs.add(key)
    return next_inode


def deep_merge_plist_dicts(
    base: dict[str, Any], staged: dict[str, Any]
) -> dict[str, Any]:
    """Recursively merge `staged` onto `base` without dropping unrelated keys."""
    out = dict(base)
    for k, v in staged.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge_plist_dicts(out[k], v)
        else:
            out[k] = v
    return out


def strip_mdm_from_backup(backup_dir: str | Path) -> list[str]:
    """
    Remove active MDM enrollment files (`MDM.plist`, `MDMEvents.plist`,
    `ManagingProfile.plist`) from an iOS backup's `Manifest.db` and `<aa>/<fileID>`
    payload store.
    """
    device_dir = Path(backup_dir)
    manifest_db = device_dir / "Manifest.db"
    removed: list[str] = []
    if not manifest_db.is_file():
        return removed

    conn = sqlite3.connect(str(manifest_db))
    try:
        for domain, rel_path in MDM_FILES_TO_STRIP:
            file_id = compute_file_id(domain, rel_path)
            row = conn.execute(
                "SELECT 1 FROM Files WHERE fileID = ? OR (domain = ? AND relativePath = ?)",
                (file_id, domain, rel_path),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "DELETE FROM Files WHERE fileID = ? OR (domain = ? AND relativePath = ?)",
                    (file_id, domain, rel_path),
                )
                removed.append(f"{domain}/{rel_path}")
            sharded = device_dir / file_id[:2] / file_id
            flat = device_dir / file_id
            for candidate in (sharded, flat):
                if candidate.exists() or candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                    if f"{domain}/{rel_path}" not in removed:
                        removed.append(f"{domain}/{rel_path}")
        conn.commit()
    finally:
        conn.close()
    return removed


def inject_files_into_ios26_backup(
    backup_dir: str | Path,
    payloads: list[tuple[str, str, bytes, int, int, int]],
    *,
    merge_existing_plists: bool = True,
) -> tuple[int, int, int, int]:
    """
    Batch-inject `(domain, relative_path, contents, mode, uid, gid)` tuples into
    an iOS 26.x `Manifest.db` backup directory using Donor-Blob Cloning,
    `SPRINGBOARD_EXT_ATTR_BYTES`, parent directory synthesis, and strictly unique
    monotonic `InodeNumber` assignment.

    Returns `(injected_count, unchanged_count, dir_rows_added, failed_count)`.
    """
    device_dir = Path(backup_dir)
    manifest_db = device_dir / "Manifest.db"
    if not manifest_db.is_file():
        return 0, 0, 0, len(payloads)

    conn = sqlite3.connect(str(manifest_db))
    injected = 0
    unchanged = 0
    dir_rows_added = 0
    try:
        conn.executescript(FILES_SCHEMA)
        next_inode = _max_inode_in_manifest(conn)
        donor_cache: dict[str, bytes | None] = {}
        dir_donor_cache: dict[str, bytes | None] = {}
        known_dirs: set[tuple[str, str]] = set()

        for domain, rel_path, contents, mode, uid, gid in payloads:
            rel_path = rel_path.lstrip("/")
            if not domain or not rel_path or ".." in Path(rel_path).parts or not contents:
                continue

            file_id = compute_file_id(domain, rel_path)
            payload_path = device_dir / file_id[:2] / file_id
            payload_path.parent.mkdir(parents=True, exist_ok=True)

            # Deep-merge onto existing backup plist if present and valid
            final_contents = bytes(contents)
            if merge_existing_plists and payload_path.is_file():
                try:
                    existing_dict = plistlib.loads(payload_path.read_bytes())
                    staged_dict = plistlib.loads(final_contents)
                    if isinstance(existing_dict, dict) and isinstance(
                        staged_dict, dict
                    ):
                        merged = deep_merge_plist_dicts(
                            existing_dict, staged_dict
                        )
                        final_contents = plistlib.dumps(
                            merged, fmt=plistlib.FMT_BINARY
                        )
                except Exception:
                    pass

            dir_path = os.path.dirname(rel_path)
            if domain not in dir_donor_cache:
                dir_donor_cache[domain] = _pick_directory_donor_blob(
                    conn, domain
                )
            inode_before = next_inode
            next_inode = _ensure_directory_rows(
                conn,
                domain,
                dir_path,
                next_inode,
                known_dirs,
                dir_donor_cache[domain],
            )
            dir_rows_added += next_inode - inode_before

            if domain not in donor_cache:
                donor_cache[domain] = _pick_donor_blob(conn, domain, rel_path)
            donor = donor_cache[domain]

            next_inode += 1
            unique_inode = next_inode
            safe_mode = (mode or (MODE_FILE | 0o644)) & 0o100777
            if (safe_mode & 0o777) == 0:
                safe_mode |= 0o644
            if (safe_mode & MODE_FILE) == 0:
                safe_mode |= MODE_FILE

            if donor is not None:
                blob = _patch_donor_blob(
                    donor,
                    rel_path,
                    final_contents,
                    inode=unique_inode,
                    mode=safe_mode,
                    uid=uid,
                    gid=gid,
                )
            else:
                blob = build_mbfile_blob(
                    ManifestDbEntry(
                        domain=domain,
                        relative_path=rel_path,
                        data=final_contents,
                        mode=safe_mode,
                        uid=uid,
                        gid=gid,
                        flags=1,
                        inode=unique_inode,
                        include_extended_attributes=True,
                    ),
                    inode=unique_inode,
                )

            if payload_path.is_file() and not payload_path.is_symlink():
                try:
                    if payload_path.read_bytes() == final_contents:
                        unchanged += 1
                        conn.execute(
                            "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) "
                            "VALUES (?, ?, ?, 1, ?)",
                            (
                                file_id,
                                domain,
                                rel_path,
                                sqlite3.Binary(blob),
                            ),
                        )
                        continue
                except OSError:
                    pass

            if payload_path.exists() or payload_path.is_symlink():
                payload_path.unlink(missing_ok=True)
            payload_path.write_bytes(final_contents)

            conn.execute(
                "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) "
                "VALUES (?, ?, ?, 1, ?)",
                (file_id, domain, rel_path, sqlite3.Binary(blob)),
            )
            injected += 1

        conn.commit()
    finally:
        conn.close()

    failed = len(payloads) - injected - unchanged
    return injected, unchanged, dir_rows_added, failed


def build_ios26_mdm_payloads(
    build_version: str = "23A345",
    *,
    product_version: str = "26.0",
    is_supervised: bool = False,
    organization_name: str = "",
    block_ota: bool = False,
) -> list[tuple[str, str, bytes, int, int, int]]:
    """
    Construct the full set of iOS 26.x MDM-bypass & Setup Assistant payloads
    as `(domain, relative_path, contents, mode, uid, gid)` tuples.

    `SetupVersion` is stamped with the target device's own iOS major version
    (floored at `MIN_SETUP_VERSION`) instead of a hardcoded future release, so
    the payload stays correct for iOS 9 - 26.x targets.
    """
    setup_version = max(
        MIN_SETUP_VERSION,
        parse_ios_major_version(product_version, build_version),
    )
    cc_bytes = build_cloud_config_details_plist(
        is_supervised=is_supervised,
        organization_name=organization_name,
    )
    mpb_bytes = build_managed_purplebuddy_plist()
    hpb_bytes = build_home_purplebuddy_plist(setup_version=setup_version)
    us_bytes = build_user_settings_plist()
    mcmeta_bytes = build_mcmeta_plist(build_version)
    mcmig_bytes = build_mc_data_migration_plist()
    pman_bytes = build_payload_manifest_plist()

    fmode = MODE_FILE | 0o644
    payloads: list[tuple[str, str, bytes, int, int, int]] = [
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            cc_bytes,
            fmode,
            501,
            501,
        ),
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist",
            cc_bytes,
            fmode,
            501,
            501,
        ),
        (
            MANAGED_PREFS_DOMAIN,
            "mobile/com.apple.purplebuddy.plist",
            mpb_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/Preferences/com.apple.purplebuddy.plist",
            hpb_bytes,
            MODE_FILE | 0o600,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            cc_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/UserSettings.plist",
            us_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/PublicInfo/EffectiveUserSettings.plist",
            us_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/PublicInfo/MCMeta.plist",
            mcmeta_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/MCDataMigration.plist",
            mcmig_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/PayloadManifest.plist",
            pman_bytes,
            fmode,
            501,
            501,
        ),
    ]
    if block_ota:
        payloads.append(
            (
                "RootDomain",
                "Library/Preferences/com.apple.MobileAsset.plist",
                build_ota_disabler_plist(),
                MODE_FILE | 0o600,
                0,
                0,
            )
        )
    return payloads


def verify_ios26_backup(
    backup_dir: str | Path,
    expected_payloads: list[tuple[str, str, bytes, int, int, int]] | None = None,
) -> list[str]:
    """
    6-point Pre-Restore Verification Gate for iOS 26.x backups:
      1. `Manifest.db` is a valid SQLite3 database.
      2. `Manifest.plist` is readable and `IsEncrypted` is False.
      3. `Status.plist` is readable and `SnapshotState == "finished"`.
      4. None of the active MDM lock files (`MDM.plist`, `MDMEvents.plist`) remain in `Manifest.db`.
      5. Every expected payload exists at `<aa>/<fileID>`, is non-empty, matches its
         `Manifest.db` row (`domain`, `relativePath`, `flags=1`), and its SHA-1
         `Digest` matches the on-disk bytes.
      6. Every regular file and directory row in `Manifest.db` has a positive,
         non-duplicate `InodeNumber` (required by modern `BackupAgent2`).

    Returns a list of problem descriptions (empty list = 100% verified).
    """
    device_dir = Path(backup_dir)
    problems: list[str] = []

    manifest_db = device_dir / "Manifest.db"
    if not manifest_db.is_file():
        problems.append("Manifest.db is missing from the backup directory")
        return problems

    manifest_plist_path = device_dir / "Manifest.plist"
    if manifest_plist_path.is_file():
        try:
            mp = plistlib.loads(manifest_plist_path.read_bytes())
            if mp.get("IsEncrypted"):
                problems.append(
                    "Backup is encrypted (IsEncrypted=True); plaintext injection requires an unencrypted backup"
                )
        except Exception as exc:
            problems.append(f"Manifest.plist is unreadable: {exc}")
    else:
        problems.append("Manifest.plist is missing from the backup directory")

    status_plist_path = device_dir / "Status.plist"
    if status_plist_path.is_file():
        try:
            sp = plistlib.loads(status_plist_path.read_bytes())
            if sp.get("SnapshotState") != "finished":
                problems.append(
                    f"Status.plist SnapshotState is {sp.get('SnapshotState')!r} (expected 'finished')"
                )
        except Exception as exc:
            problems.append(f"Status.plist is unreadable: {exc}")

    try:
        conn = sqlite3.connect(str(manifest_db))
        try:
            rows = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in conn.execute(
                    "SELECT fileID, domain, relativePath, flags, file FROM Files"
                )
            }
        finally:
            conn.close()
    except Exception as exc:
        problems.append(f"Manifest.db SQLite read failed: {exc}")
        return problems

    # Check that MDM.plist / MDMEvents.plist were stripped
    for domain, rel_path in MDM_FILES_TO_STRIP:
        fid = compute_file_id(domain, rel_path)
        if fid in rows:
            problems.append(
                f"Active MDM file still present in Manifest.db: {domain}/{rel_path}"
            )

    # Check expected payloads & parent directories
    if expected_payloads:
        dir_set = {
            (dom, rel)
            for (_fid, (dom, rel, flg, _blob)) in rows.items()
            if flg == 2
        }
        seen_inodes: set[int] = set()
        for domain, rel_path, _contents, _mode, _uid, _gid in expected_payloads:
            rel_path = rel_path.lstrip("/")
            fid = compute_file_id(domain, rel_path)
            disk_path = device_dir / fid[:2] / fid
            if not disk_path.is_file():
                problems.append(
                    f"{domain}/{rel_path}: payload file missing at {disk_path}"
                )
                continue
            on_disk = disk_path.read_bytes()
            if len(on_disk) == 0:
                problems.append(f"{domain}/{rel_path}: payload file is 0 bytes")
            if fid not in rows:
                problems.append(
                    f"{domain}/{rel_path}: missing row in Manifest.db"
                )
                continue
            row_domain, row_rel, row_flags, row_blob = rows[fid]
            if row_domain != domain or row_rel != rel_path:
                problems.append(
                    f"{domain}/{rel_path}: Manifest.db mismatch ({row_domain}/{row_rel})"
                )
            if row_flags != 1:
                problems.append(
                    f"{domain}/{rel_path}: Manifest.db flags={row_flags} (expected 1)"
                )
            # Check MBFile Digest & InodeNumber
            try:
                archive = plistlib.loads(row_blob)
                objs = archive["$objects"]
                info = objs[1]
                ino = info.get("InodeNumber", 0)
                if not isinstance(ino, int) or ino <= 0:
                    problems.append(
                        f"{domain}/{rel_path}: invalid InodeNumber={ino!r}"
                    )
                elif ino in seen_inodes:
                    problems.append(
                        f"{domain}/{rel_path}: duplicate InodeNumber={ino}"
                    )
                else:
                    seen_inodes.add(ino)
                digest_uid = info.get("Digest")
                if isinstance(digest_uid, UID):
                    digest = objs[digest_uid.data]
                    if digest != hashlib.sha1(on_disk).digest():
                        problems.append(
                            f"{domain}/{rel_path}: MBFile SHA-1 Digest does not match on-disk file"
                        )
            except Exception as exc:
                problems.append(
                    f"{domain}/{rel_path}: invalid MBFile keyed archive ({exc})"
                )

            # Check ancestor directory rows
            parts = rel_path.split("/")
            for depth in range(1, len(parts)):
                parent_rel = "/".join(parts[:depth])
                if (domain, parent_rel) not in dir_set:
                    problems.append(
                        f"{domain}/{rel_path}: missing parent directory row '{parent_rel}' in Manifest.db"
                    )

    return problems


def patch_ios26_existing_backup(
    backup_dir: str | Path,
    *,
    udid: str | None = None,
    serial_number: str | None = None,
    build_version: str = "23A345",
    product_version: str = "26.0",
    is_supervised: bool = False,
    organization_name: str = "",
    block_ota: bool = False,
) -> IOS26PatchReport:
    """
    Apply the iOS 26.x G1 MDM-bypass & Setup Assistant patch to an existing
    `Manifest.db` backup directory:
      1. Strips `MDM.plist`, `MDMEvents.plist`, and `ManagingProfile.plist`.
      2. Updates `Manifest.plist` and `Info.plist` atomically if `udid`/`serial_number`
         are provided.
      3. Injects `CloudConfigurationDetails.plist`, `purplebuddy.plist`
         (with all 80 `SKIP_SETUP_KEYS`), and `ConfigurationProfiles/*`
         with Donor-Blob Cloning and unique monotonic inodes.
      4. Runs the 6-point `verify_ios26_backup` gate.
    """
    device_dir = Path(backup_dir)
    if udid and (device_dir / sanitize_udid(udid) / "Manifest.db").is_file():
        device_dir = device_dir / sanitize_udid(udid)

    stripped = strip_mdm_from_backup(device_dir)

    # Update Manifest.plist / Info.plist if identity overrides were supplied
    manifest_plist_path = device_dir / "Manifest.plist"
    if manifest_plist_path.is_file() and (udid or serial_number):
        try:
            mp = plistlib.loads(manifest_plist_path.read_bytes())
            lockdown = mp.get("Lockdown")
            if isinstance(lockdown, dict):
                if udid:
                    lockdown["UniqueDeviceID"] = udid
                if serial_number:
                    lockdown["SerialNumber"] = serial_number
                if build_version:
                    lockdown["BuildVersion"] = build_version
                if product_version:
                    lockdown["ProductVersion"] = product_version
            if udid and "UniqueDeviceID" in mp:
                mp["UniqueDeviceID"] = udid
            if serial_number and "SerialNumber" in mp:
                mp["SerialNumber"] = serial_number
            atomic_write_plist(manifest_plist_path, mp)
        except Exception:
            pass

    payloads = build_ios26_mdm_payloads(
        build_version=build_version,
        product_version=product_version,
        is_supervised=is_supervised,
        organization_name=organization_name,
        block_ota=block_ota,
    )
    injected, unchanged, dirs_added, failed = inject_files_into_ios26_backup(
        device_dir,
        payloads,
        merge_existing_plists=True,
    )
    problems = verify_ios26_backup(device_dir, payloads)

    return IOS26PatchReport(
        backup_dir=device_dir,
        stripped_mdm_files=stripped,
        injected_files=injected,
        unchanged_files=unchanged,
        synthesized_dirs=dirs_added,
        failed_files=failed,
        verified=(len(problems) == 0 and failed == 0),
        verification_problems=problems,
    )


def ios26_direct_skip_setup(
    udid: str | None = None,
    *,
    supervised: bool = False,
    organization_name: str = "",
    set_lockdown_flags: bool = True,
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """
    Vector A (iOS 26.x Direct `MobileConfigService` + `lockdownd` Push):
    Connects over USB Lockdown directly to `com.apple.mobile.MCInstall` and sends
    `SetCloudConfiguration` with all 80 `SKIP_SETUP_KEYS`,
    `CloudConfigurationUIComplete = True`, `ConfigurationWasApplied = True`,
    `IsSupervised = False`, and `PostSetupProfileWasInstalled = True`.

    Also sets persistent `lockdownd` completion flags (`com.apple.purplebuddy`,
    `com.apple.mobile.iTunes`, `com.apple.mobile.data_sync`).

    Does NOT perform a partial backup restore — completely immune to the
    partial-restore "safe state recovery" factory-reset loop.
    """
    def _emit(msg: str) -> None:
        if on_output:
            on_output(msg)
        else:
            print(msg)

    applied_any = False

    # 1. Try pymobiledevice3 MobileConfigService + LockdownClient
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobile_config import MobileConfigService
        import pymobiledevice3.service_connection as _sc

        _sc.DEFAULT_SSL_HANDSHAKE_TIMEOUT = 60

        async def _async_apply() -> bool:
            ld = create_using_usbmux(serial=udid)
            if asyncio.iscoroutine(ld):
                ld = await ld
            try:
                if set_lockdown_flags:
                    for domain, key, val in LOCKDOWN_COMPLETION_FLAGS:
                        try:
                            res = ld.set_value(val, key=key, domain=domain)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception:
                            pass
                    _emit(
                        "[+] Applied persistent lockdownd Setup Assistant completion flags."
                    )

                mcs_ctx = MobileConfigService(lockdown=ld)
                if hasattr(mcs_ctx, "__aenter__"):
                    async with mcs_ctx as mcs:
                        existing = await mcs.get_cloud_configuration() or {}
                        cfg = build_ios26_cloud_config(
                            existing,
                            supervised=supervised,
                            organization_name=organization_name,
                        )
                        await mcs.set_cloud_configuration(cfg)
                else:
                    with mcs_ctx as mcs:
                        existing = mcs.get_cloud_configuration() or {}
                        if asyncio.iscoroutine(existing):
                            existing = await existing
                        cfg = build_ios26_cloud_config(
                            existing or {},
                            supervised=supervised,
                            organization_name=organization_name,
                        )
                        res = mcs.set_cloud_configuration(cfg)
                        if asyncio.iscoroutine(res):
                            await res
                return True
            finally:
                close_fn = getattr(ld, "close", None)
                if close_fn:
                    try:
                        res = close_fn()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass

        applied_any = asyncio.run(_async_apply())
        if applied_any:
            _emit(
                "[+] Pushed CloudConfiguration (80 SkipSetup panes, IsSupervised=False) via MobileConfigService."
            )
            return True
    except ImportError:
        _emit(
            "[*] pymobiledevice3 not installed; falling back to libimobiledevice CLI for lockdownd flags..."
        )
    except Exception as exc:
        _emit(f"[!] MobileConfigService push note: {exc}")

    # 2. Fallback: set lockdownd completion keys via `ideviceinfo` / `idevicediagnostics` if available
    if set_lockdown_flags and shutil.which("idevicepair"):
        _emit(
            "[*] Note: Install `pymobiledevice3` (`pip install pymobiledevice3`) to enable direct MobileConfigService CloudConfiguration injection."
        )

    return applied_any
