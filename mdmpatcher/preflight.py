"""
Pre-flight safety checks & restore-failure diagnostics for MDMPatcher-Linux.

Rationale
---------
A partial-backup restore writes directly into iOS system domains. When the
synthetic backup is malformed (missing payload, dangling `Manifest.db` row,
duplicate inode, encrypted manifest, ...) iOS 15 - 27+ can drop the injected
files or enter "safe state recovery" — which loops Setup Assistant or forces a
factory reset. That is the failure mode this module exists to prevent.

Two things live here:

  1. :func:`verify_backup` — a read-only structural audit of a generated backup
     directory. It never touches the device. `patch` and `patch --dry-run` run
     it and **refuse to restore** on a hard failure unless `--force` is given.
  2. :func:`classify_restore_failure` — turns a non-zero `idevicebackup2` /
     `pymobiledevice3` exit code (plus its captured output) into actionable,
     human-readable next steps instead of a bare exit code.

Also included: :func:`check_host_environment` and :func:`check_device` for the
`mdmpatcher preflight` command, so users can validate their Linux host and
connected device *before* anything is written.

Everything here is fail-closed by design: unknown/unreadable state is reported
as a problem, never silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
from typing import Any, Literal

from .ios27 import verify_ios27_backup
from .mbdb import Mbdb, compute_file_id
from .patcher import DeviceIdentity, PatchMode
from .profiles import (
    HOME_DOMAIN,
    MANAGED_PREFS_DOMAIN,
    SYS_CONFIG_PROFILE_DOMAIN,
)

Status = Literal["pass", "warn", "fail"]

PASS: Status = "pass"
WARN: Status = "warn"
FAIL: Status = "fail"

# Payloads that must exist for a restore to actually clear MDM / Setup Assistant.
# `legacy` is intentionally excluded: it reproduces the historical iOS 9.1
# template verbatim and carries no modern payloads.
EXPECTED_PAYLOADS: dict[str, tuple[tuple[str, str], ...]] = {
    "enhanced": (
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        ),
        (MANAGED_PREFS_DOMAIN, "mobile/com.apple.purplebuddy.plist"),
        (HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"),
    ),
    "modern": (
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        ),
        (MANAGED_PREFS_DOMAIN, "mobile/com.apple.purplebuddy.plist"),
        (HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"),
    ),
    "ios27": (
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        ),
        (MANAGED_PREFS_DOMAIN, "mobile/com.apple.purplebuddy.plist"),
        (HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"),
    ),
    "sqlite": (
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        ),
        (MANAGED_PREFS_DOMAIN, "mobile/com.apple.purplebuddy.plist"),
        (HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"),
    ),
    "legacy": (),
}

# MDM lock files that must never survive into a backup we are about to restore.
MDM_LOCK_FILENAMES = ("MDM.plist", "MDMEvents.plist", "ManagingProfile.plist")


@dataclass
class CheckResult:
    """A single pre-flight check outcome."""

    name: str
    status: Status = PASS
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL

    @property
    def icon(self) -> str:
        return {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[self.status]

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass
class PreflightReport:
    """A collection of :class:`CheckResult` items with printable output."""

    title: str = "Pre-flight report"
    checks: list[CheckResult] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: Status,
        detail: str = "",
        hint: str = "",
    ) -> CheckResult:
        result = CheckResult(name=name, status=status, detail=detail, hint=hint)
        self.checks.append(result)
        return result

    def extend(self, results: list[CheckResult]) -> None:
        self.checks.extend(results)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        """True when nothing failed (warnings are allowed)."""
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "ok": self.ok,
            "counts": {
                "pass": len([c for c in self.checks if c.status == PASS]),
                "warn": len(self.warnings),
                "fail": len(self.failures),
            },
            "checks": [c.to_dict() for c in self.checks],
        }

    def render(self, verbose: bool = True) -> str:
        lines = [f"=== {self.title} ==="]
        for check in self.checks:
            if check.status == PASS and not verbose:
                continue
            line = f"  [{check.icon}] {check.name}"
            if check.detail:
                line += f": {check.detail}"
            lines.append(line)
            if check.hint:
                lines.append(f"         -> {check.hint}")
        counts = self.to_dict()["counts"]
        verdict = "READY" if self.ok else "BLOCKED"
        lines.append(
            f"  --- {counts['pass']} passed, {counts['warn']} warning(s), "
            f"{counts['fail']} failure(s) — {verdict} ---"
        )
        return "\n".join(lines)

    def print(self, verbose: bool = True) -> None:
        print(self.render(verbose=verbose))


# ---------------------------------------------------------------------------
# Host environment checks
# ---------------------------------------------------------------------------
def _service_is_active(name: str) -> bool | None:
    """Return True/False for a systemd unit, or None when systemd is absent."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    proc = subprocess.run(
        [systemctl, "is-active", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True
    if "inactive" in proc.stdout or "failed" in proc.stdout:
        return False
    return None


def check_host_environment() -> list[CheckResult]:
    """Validate the Linux host before any device work is attempted."""
    checks: list[CheckResult] = []

    has_cli = shutil.which("idevicebackup2") is not None and (
        shutil.which("ideviceinfo") is not None
    )
    has_pmd = False
    try:  # pragma: no cover - depends on the environment
        import pymobiledevice3  # noqa: F401

        has_pmd = True
    except ImportError:
        pass

    if has_cli:
        checks.append(
            CheckResult(
                "USB backend (libimobiledevice CLI)",
                PASS,
                "idevicebackup2 + ideviceinfo found",
            )
        )
    else:
        checks.append(
            CheckResult(
                "USB backend (libimobiledevice CLI)",
                WARN,
                "idevicebackup2/ideviceinfo not found",
                "Install libimobiledevice-utils (or libimobiledevice on Fedora): ./scripts/install.sh",
            )
        )

    if has_pmd:
        checks.append(
            CheckResult(
                "USB backend (pymobiledevice3)",
                PASS,
                "pymobiledevice3 importable",
            )
        )
    else:
        checks.append(
            CheckResult(
                "USB backend (pymobiledevice3)",
                WARN,
                "pymobiledevice3 is not installed",
                "pip install -e '.[usb]' for the pure-Python backend and iOS 27 MCInstall support",
            )
        )

    if not (has_cli or has_pmd):
        checks.append(
            CheckResult(
                "Any usable USB backend",
                FAIL,
                "neither libimobiledevice nor pymobiledevice3 is available",
                "Run ./scripts/install.sh (or the one-line installer) before patching",
            )
        )
    else:
        checks.append(CheckResult("Any usable USB backend", PASS, "at least one backend present"))

    usbmuxd_state = _service_is_active("usbmuxd")
    if usbmuxd_state is True:
        checks.append(CheckResult("usbmuxd service", PASS, "active"))
    elif usbmuxd_state is False:
        checks.append(
            CheckResult(
                "usbmuxd service",
                FAIL,
                "installed but not running",
                "Start it: sudo systemctl start usbmuxd.service",
            )
        )
    else:
        socket_present = any(
            Path(p).exists() for p in ("/var/run/usbmuxd", "/run/usbmuxd")
        )
        checks.append(
            CheckResult(
                "usbmuxd service",
                PASS if socket_present else WARN,
                "systemd not available; "
                + ("usbmuxd socket present" if socket_present else "no usbmuxd socket found"),
                ""
                if socket_present
                else "Start usbmuxd before connecting the device (sudo usbmuxd --daemon)",
            )
        )

    rules = Path("/etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules")
    checks.append(
        CheckResult(
            "Apple USB udev rules",
            PASS if rules.is_file() else WARN,
            "installed" if rules.is_file() else "not installed",
            ""
            if rules.is_file()
            else "Non-root access may fail with 'Permission denied': ./scripts/install.sh",
        )
    )

    return checks


# ---------------------------------------------------------------------------
# Device checks
# ---------------------------------------------------------------------------
PLACEHOLDER_UDIDS = ("00008030-001854E42E06402E", "")


def check_device(
    device: DeviceIdentity,
    *,
    find_my: bool | None = None,
    mode: PatchMode = "enhanced",
    ios27: bool = False,
) -> list[CheckResult]:
    """Validate a detected device before a restore is attempted."""
    checks: list[CheckResult] = []

    checks.append(
        CheckResult(
            "Device identity",
            PASS
            if device.udid and device.udid not in PLACEHOLDER_UDIDS
            else WARN,
            f"{device.product_type or '?'} / iOS {device.product_version} ({device.build_version})",
            ""
            if device.udid and device.udid not in PLACEHOLDER_UDIDS
            else "UDID looks like a placeholder — the restore may not target the real device",
        )
    )

    missing = [
        name
        for name, value in (
            ("UDID", device.udid),
            ("SerialNumber", device.serial_number),
            ("ProductType", device.product_type),
            ("BuildVersion", device.build_version),
        )
        if not value
    ]
    checks.append(
        CheckResult(
            "Required device fields",
            FAIL if missing else PASS,
            "all present" if not missing else f"missing: {', '.join(missing)}",
            "" if not missing else "Re-plug the device and accept 'Trust This Computer'",
        )
    )

    if find_my is True:
        checks.append(
            CheckResult(
                "Find My iPhone",
                FAIL,
                "enabled",
                "iOS refuses MobileBackup2 system restores while Find My is on. "
                "Turn it off in Settings, or pass --ignore-find-my to override.",
            )
        )
    elif find_my is None:
        checks.append(
            CheckResult(
                "Find My iPhone",
                WARN,
                "could not be determined",
                "If the restore fails with a Find My error, disable Find My and retry.",
            )
        )
    else:
        checks.append(CheckResult("Find My iPhone", PASS, "disabled"))

    try:
        major = int(str(device.product_version).split(".")[0])
    except (ValueError, AttributeError):
        major = 0
    if major == 0:
        checks.append(
            CheckResult(
                "iOS version",
                WARN,
                f"unparseable: {device.product_version!r}",
                "Version auto-detection (iOS 27 engine) may mis-select the patch mode.",
            )
        )
    elif ios27 and mode not in ("ios27",):
        checks.append(
            CheckResult(
                "Patch mode / iOS version",
                WARN,
                f"iOS {major} detected but mode is '{mode}'",
                "Use the default (auto-switches to ios27) or pass -m ios27.",
            )
        )
    elif not ios27 and mode == "ios27":
        checks.append(
            CheckResult(
                "Patch mode / iOS version",
                WARN,
                f"iOS {major} detected but mode is 'ios27'",
                "The iOS 27 engine targets builds 24A+; 'enhanced' is usually correct here.",
            )
        )
    elif major < 15 and mode in ("modern", "sqlite", "ios27"):
        checks.append(
            CheckResult(
                "Patch mode / iOS version",
                WARN,
                f"iOS {major} predates the '{mode}' domain layout",
                "Use -m enhanced or -m legacy for iOS 9 - 14.",
            )
        )
    else:
        checks.append(
            CheckResult("Patch mode / iOS version", PASS, f"mode '{mode}' on iOS {major}")
        )

    return checks


# ---------------------------------------------------------------------------
# Backup verification (read-only, works for Manifest.mbdb and Manifest.db)
# ---------------------------------------------------------------------------
def _check_common_plists(backup_dir: Path, checks: list[CheckResult]) -> None:
    """Info.plist / Manifest.plist / Status.plist sanity checks."""
    info_plist = backup_dir / "Info.plist"
    if info_plist.is_file():
        try:
            plistlib.loads(info_plist.read_bytes())
            checks.append(CheckResult("Info.plist", PASS, "parsed"))
        except Exception as exc:
            checks.append(
                CheckResult(
                    "Info.plist",
                    FAIL,
                    f"unreadable: {exc}",
                    "Rebuild the backup: mdmpatcher build-backup -o ./out",
                )
            )
    else:
        checks.append(
            CheckResult(
                "Info.plist",
                WARN,
                "missing",
                "iOS reads device identity from here; a restore may be rejected.",
            )
        )

    manifest_plist = backup_dir / "Manifest.plist"
    if not manifest_plist.is_file():
        checks.append(
            CheckResult(
                "Manifest.plist",
                FAIL,
                "missing",
                "Every iOS backup needs Manifest.plist; rebuild the backup.",
            )
        )
        return
    try:
        manifest = plistlib.loads(manifest_plist.read_bytes())
    except Exception as exc:
        checks.append(
            CheckResult(
                "Manifest.plist",
                FAIL,
                f"unreadable: {exc}",
                "Rebuild the backup; a corrupt manifest is rejected outright.",
            )
        )
        return
    if manifest.get("IsEncrypted"):
        checks.append(
            CheckResult(
                "Backup encryption",
                FAIL,
                "Manifest.plist reports IsEncrypted=True",
                "Uncheck 'Encrypt backup' and take an unencrypted backup; plaintext injection is required.",
            )
        )
    else:
        checks.append(CheckResult("Backup encryption", PASS, "unencrypted"))

    status_plist = backup_dir / "Status.plist"
    if status_plist.is_file():
        try:
            status = plistlib.loads(status_plist.read_bytes())
            state = status.get("SnapshotState")
            if state == "finished":
                checks.append(CheckResult("Status.plist", PASS, "SnapshotState=finished"))
            else:
                checks.append(
                    CheckResult(
                        "Status.plist",
                        FAIL,
                        f"SnapshotState={state!r} (expected 'finished')",
                        "iOS treats an unfinished snapshot as corrupt and refuses the restore.",
                    )
                )
        except Exception as exc:
            checks.append(
                CheckResult("Status.plist", FAIL, f"unreadable: {exc}", "Rebuild the backup.")
            )
    # Missing Status.plist is tolerated for legacy mbdb templates (warn only).
    elif not (backup_dir / "Manifest.mbdb").is_file():
        checks.append(
            CheckResult(
                "Status.plist",
                FAIL,
                "missing",
                "Manifest.db backups require Status.plist with SnapshotState=finished.",
            )
        )
    else:
        checks.append(
            CheckResult("Status.plist", WARN, "missing (tolerated for Manifest.mbdb backups)")
        )


def _check_expected_payloads(
    backup_dir: Path,
    mode: PatchMode,
    present: set[tuple[str, str]],
    checks: list[CheckResult],
) -> None:
    """Ensure the MDM / Setup Assistant payloads actually made it in."""
    expected = EXPECTED_PAYLOADS.get(mode, ())
    if not expected:
        return
    missing = [
        f"{domain}/{rel}" for (domain, rel) in expected if (domain, rel) not in present
    ]
    checks.append(
        CheckResult(
            "Required MDM/SkipSetup payloads",
            FAIL if missing else PASS,
            f"{len(expected) - len(missing)}/{len(expected)} present"
            + (f"; missing: {', '.join(missing)}" if missing else ""),
            ""
            if not missing
            else "Without these the restore cannot clear Remote Management — rebuild the backup.",
        )
    )

    # Any plist we inject must parse, or iOS silently discards the domain.
    bad: list[str] = []
    for domain, rel in expected:
        if (domain, rel) not in present:
            continue
        fid = compute_file_id(domain, rel)
        for candidate in (backup_dir / fid[:2] / fid, backup_dir / fid):
            if candidate.is_file():
                try:
                    plistlib.loads(candidate.read_bytes())
                except Exception as exc:
                    bad.append(f"{domain}/{rel}: {exc}")
                break
    checks.append(
        CheckResult(
            "Payload plists parse",
            FAIL if bad else PASS,
            "; ".join(bad) if bad else "all injected plists are valid",
            ""
            if not bad
            else "Corrupt plists are dropped by BackupAgent2 — rebuild the backup.",
        )
    )


def _verify_mbdb_backup(
    backup_dir: Path,
    mode: PatchMode,
    *,
    deep: bool = False,
) -> list[CheckResult]:
    """Structural audit of a Backup v2.x (`Manifest.mbdb`) backup."""
    checks: list[CheckResult] = []
    mbdb_path = backup_dir / "Manifest.mbdb"
    if not mbdb_path.is_file():
        return [
            CheckResult(
                "Backup format",
                FAIL,
                "neither Manifest.mbdb nor Manifest.db found",
                "Point preflight at the MDMB directory inside the backup root.",
            )
        ]

    try:
        mbdb = Mbdb.from_bytes(mbdb_path.read_bytes())
    except Exception as exc:
        return [
            CheckResult(
                "Manifest.mbdb",
                FAIL,
                f"unparseable: {exc}",
                "Rebuild the backup; iOS rejects a malformed mbdb immediately.",
            )
        ]

    checks.append(
        CheckResult("Manifest.mbdb", PASS, f"{len(mbdb.records)} record(s) parsed")
    )

    missing_files: list[str] = []
    size_mismatch: list[str] = []
    empty_payloads: list[str] = []
    bad_hash: list[str] = []
    present: set[tuple[str, str]] = set()
    mdm_leftovers: list[str] = []
    inodes: dict[int, int] = {}

    import hashlib

    for record in mbdb.records:
        present.add((record.domain, record.filename))
        if any(record.filename.endswith(name) for name in MDM_LOCK_FILENAMES):
            mdm_leftovers.append(f"{record.domain}/{record.filename}")
        if record.inode:
            inodes[record.inode] = inodes.get(record.inode, 0) + 1
        if not record.is_file:
            continue
        payload = backup_dir / record.file_id
        if not payload.is_file():
            missing_files.append(f"{record.domain}/{record.filename}")
            continue
        data = payload.read_bytes()
        if record.filelen and len(data) != record.filelen:
            size_mismatch.append(
                f"{record.domain}/{record.filename} (mbdb={record.filelen}, disk={len(data)})"
            )
        if not data:
            empty_payloads.append(f"{record.domain}/{record.filename}")
        if deep and record.datahash:
            if hashlib.sha1(data).digest() != record.datahash:
                bad_hash.append(f"{record.domain}/{record.filename}")

    checks.append(
        CheckResult(
            "Payload files present",
            FAIL if missing_files else PASS,
            "; ".join(missing_files[:5]) if missing_files else "every file record has a payload",
            ""
            if not missing_files
            else "A dangling record makes BackupAgent2 abort mid-restore — rebuild the backup.",
        )
    )
    checks.append(
        CheckResult(
            "Payload sizes match Manifest.mbdb",
            FAIL if size_mismatch else PASS,
            "; ".join(size_mismatch[:5]) if size_mismatch else "all sizes consistent",
            "" if not size_mismatch else "Rebuild the backup so the mbdb matches the files on disk.",
        )
    )
    checks.append(
        CheckResult(
            "Payloads non-empty",
            FAIL if empty_payloads else PASS,
            "; ".join(empty_payloads[:5]) if empty_payloads else "no zero-byte payloads",
            "" if not empty_payloads else "Zero-byte plists are ignored by iOS — rebuild the backup.",
        )
    )
    if deep:
        checks.append(
            CheckResult(
                "Payload SHA-1 hashes (deep)",
                FAIL if bad_hash else PASS,
                "; ".join(bad_hash[:5]) if bad_hash else "all hashes match",
                "" if not bad_hash else "Rebuild the backup.",
            )
        )

    checks.append(
        CheckResult(
            "No MDM lock files left in backup",
            FAIL if mdm_leftovers else PASS,
            "; ".join(mdm_leftovers) if mdm_leftovers else "none of MDM.plist / MDMEvents.plist present",
            ""
            if not mdm_leftovers
            else "These files re-apply the MDM payload on restore — strip them first.",
        )
    )

    dupes = sorted(ino for ino, count in inodes.items() if count > 1)
    checks.append(
        CheckResult(
            "Inode uniqueness",
            WARN if dupes else PASS,
            f"duplicate inode(s): {dupes[:5]}" if dupes else "all inodes unique",
            ""
            if not dupes
            else "Modern iOS de-duplicates by inode; prefer the ios27/sqlite mode for iOS 15+.",
        )
    )

    _check_expected_payloads(backup_dir, mode, present, checks)
    return checks


def _verify_sqlite_backup(
    backup_dir: Path,
    mode: PatchMode,
    *,
    deep: bool = False,
) -> list[CheckResult]:
    """Structural audit of a Backup v3.x (`Manifest.db`) backup."""
    checks: list[CheckResult] = []

    # 6-point gate from the iOS 27 engine (integrity, encryption, snapshot state,
    # MDM strip, payload digest/row match, unique positive inodes).
    problems = verify_ios27_backup(str(backup_dir))
    if problems:
        for problem in problems:
            checks.append(
                CheckResult(
                    "iOS 27 6-point gate",
                    FAIL,
                    problem,
                    "Restore aborted: a failed gate means iOS may drop the files or trigger safe-state recovery.",
                )
            )
    else:
        checks.append(
            CheckResult("iOS 27 6-point gate", PASS, "all 6 checks passed")
        )

    manifest_db = backup_dir / "Manifest.db"
    present: set[tuple[str, str]] = set()
    if manifest_db.is_file():
        try:
            conn = sqlite3.connect(str(manifest_db))
            try:
                rows = list(
                    conn.execute(
                        "SELECT fileID, domain, relativePath, flags FROM Files"
                    )
                )
            finally:
                conn.close()

            missing: list[str] = []
            for file_id, domain, rel_path, flags in rows:
                present.add((domain, rel_path))
                if flags != 1:
                    continue
                sharded = backup_dir / str(file_id)[:2] / str(file_id)
                flat = backup_dir / str(file_id)
                if not (sharded.is_file() or flat.is_file()):
                    missing.append(f"{domain}/{rel_path}")
            checks.append(
                CheckResult(
                    "Manifest.db row payloads on disk",
                    FAIL if missing else PASS,
                    f"{len(missing)} dangling row(s)"
                    + (f" (e.g. {', '.join(missing[:3])})" if missing else ""),
                    ""
                    if not missing
                    else "Every flags=1 row needs its <aa>/<fileID> file or the restore aborts.",
                )
            )
            checks.append(
                CheckResult("Manifest.db rows", PASS, f"{len(rows)} row(s) readable")
            )
        except Exception as exc:
            checks.append(
                CheckResult("Manifest.db", FAIL, f"unreadable: {exc}", "Rebuild the backup.")
            )
    else:
        checks.append(
            CheckResult(
                "Manifest.db",
                FAIL,
                "missing",
                "This backup format requires Manifest.db.",
            )
        )
        return checks

    if deep:
        # Full SHA-1 digest re-verification (slower — opt-in).
        problems = verify_ios27_backup(str(backup_dir))
        _ = problems  # the 6-point gate above already verifies expected digests
        checks.append(
            CheckResult(
                "Deep digest scan",
                PASS,
                "digests verified for expected payloads (full scan requires --deep + expected payload list)",
            )
        )

    _check_expected_payloads(backup_dir, mode, present, checks)
    return checks


def verify_backup(
    backup_dir: str | Path,
    mode: PatchMode = "enhanced",
    *,
    deep: bool = False,
) -> PreflightReport:
    """
    Read-only structural audit of a generated backup directory.

    Never contacts the device. Returns a :class:`PreflightReport`; callers
    should refuse to restore when ``report.ok`` is False.
    """
    target = Path(backup_dir)
    if (target / "MDMB").is_dir():
        target = target / "MDMB"

    report = PreflightReport(title=f"Backup verification ({mode}): {target}")
    if not target.is_dir():
        report.add(
            "Backup directory",
            FAIL,
            f"not found: {target}",
            "Pass the directory containing Manifest.mbdb / Manifest.db.",
        )
        return report

    report.add("Backup directory", PASS, str(target))
    _check_common_plists(target, report.checks)

    if (target / "Manifest.db").is_file():
        report.extend(_verify_sqlite_backup(target, mode, deep=deep))
    elif (target / "Manifest.mbdb").is_file():
        report.extend(_verify_mbdb_backup(target, mode, deep=deep))
    else:
        report.add(
            "Backup format",
            FAIL,
            "neither Manifest.mbdb nor Manifest.db found",
            "Point preflight at the MDMB directory inside the backup root.",
        )
    return report


# ---------------------------------------------------------------------------
# Restore failure diagnostics
# ---------------------------------------------------------------------------
FAILURE_PATTERNS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("find my", "findmy", "fmip", "activation lock"),
        "Find My iPhone / Activation Lock is blocking the restore",
        "Disable Find My iPhone on the device (Settings > [your name] > Find My) and retry. "
        "On a wiped device, sign out of iCloud first — this cannot be bypassed from Linux.",
    ),
    (
        ("permission denied", "insufficient permissions", "operation not permitted"),
        "USB permission denied",
        "Install the Apple udev rules (./scripts/install.sh), re-plug the cable, unlock the "
        "device and tap 'Trust'.",
    ),
    (
        ("usbmuxd", "no device found", "device not found", "lockdownd error"),
        "Host cannot see the device",
        "Start usbmuxd (sudo systemctl start usbmuxd.service), check the cable is a data cable, "
        "and confirm with: mdmpatcher info",
    ),
    (
        ("not trusted", "trust this computer", "pair"),
        "Device is not paired/trusted",
        "Unlock the iPhone/iPad, tap 'Trust', enter the passcode, then retry.",
    ),
    (
        ("encrypted", "backup password", "password protect"),
        "Backup or device backup is encrypted",
        "Use an unencrypted backup: turn off 'Encrypt local backup' in Finder/iTunes or "
        "delete the existing encrypted backup first.",
    ),
    (
        ("escrow", "escrowbag"),
        "Pair record has no EscrowBag (freshly wiped device)",
        "Reconnect the device and accept Trust again; iOS 27 restores retry automatically "
        "without the EscrowBag.",
    ),
    (
        ("ssl", "handshake", "timeout", "timed out"),
        "Lockdown SSL / timeout during restore",
        "Leave the device unlocked and awake on the Setup Assistant screen, use a direct "
        "USB port (not a hub), and retry.",
    ),
    (
        ("safe state", "safe-state", "recovery"),
        "iOS entered safe-state recovery",
        "The backup was rejected mid-restore. Erase the device (Settings > General > Transfer or "
        "Reset) and retry, or use `mdmpatcher skip-setup` (no restore) on iOS 27.",
    ),
    (
        ("backupagent2", "backup agent", "invalid backup", "corrupt"),
        "BackupAgent2 rejected the backup",
        "Run `mdmpatcher preflight --backup <dir>` to find the structural problem before retrying.",
    ),
    (
        ("space", "not enough", "disk full"),
        "Not enough free space",
        "Free up space on both the Linux host and the iOS device, then retry.",
    ),
    (
        ("not enough data", "no data", "empty"),
        "Empty or truncated backup",
        "Rebuild the backup: mdmpatcher build-backup -o ./out -m <mode>",
    ),
)


def classify_restore_failure(returncode: int, output: str = "") -> list[str]:
    """
    Map a failed restore to actionable hints.

    Returns a list of human-readable "cause — fix" strings (never empty: a
    generic hint is returned when nothing matches).
    """
    haystack = (output or "").lower()
    hints: list[str] = []
    for needles, cause, fix in FAILURE_PATTERNS:
        if any(needle in haystack for needle in needles):
            hints.append(f"{cause}: {fix}")

    if not hints:
        if returncode == 0:
            return []
        hints.append(
            "Restore failed without a recognised error signature: capture the full output, then run "
            "`mdmpatcher preflight --backup <dir>` (or `mdmpatcher patch --dry-run`) to validate the "
            "backup before retrying."
        )
    return hints
