"""
Core MDM Backup Patcher & Generator for Linux.

Verified support window: iOS 9.0 - iOS 26.x (builds 23A+). iOS 27.0+ (builds
24A+) is experimental: it still builds and restores, but every CLI command
prints an unverified-firmware warning first (see `mdmpatcher/ios26.py`).

Supports five patch modes:
  1. `enhanced` (default for iOS 9 - 26):
     Starts from the reverse-engineered `MDMPatcher-Universal` / `MDMPatcher-Enhanced`
     backup template (`MDMB/Manifest.mbdb`), customizes `Info.plist` and `Manifest.plist`
     with the connected device's UDID, SerialNumber, ProductType, BuildVersion,
     ProductVersion, and IMEI, AND upgrades `Manifest.mbdb` + backup files for
     modern iOS:
       - Injects `CloudConfigurationDetails.plist` and `CloudConfigurationSetAsideDetails.plist`
         into `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`
         (with all 80 iOS 15-26.x `SkipSetup` pane identifiers).
       - Injects `mobile/com.apple.purplebuddy.plist` into `ManagedPreferencesDomain`.
       - Updates `HomeDomain/Library/Preferences/com.apple.purplebuddy.plist` (removes
         legacy `zh_CN` locale override and sets `SetupDone=True`, `SetupFinishedAllSteps=True`,
         and `SetupVersion` to the device's own iOS major (minimum 26)).
       - Updates `HomeDomain/Library/ConfigurationProfiles/PublicInfo/MCMeta.plist`
         so `LastMigratedBuild` matches the device's actual `BuildVersion`.
       - Cleans `SystemPreferencesDomain/SystemConfiguration/com.apple.mobilegestalt.plist`
         so the user's device name is not overwritten with `"“iMyFone”的 iPhone"`.
       - Optionally injects `RootDomain/Library/Preferences/com.apple.MobileAsset.plist`
         to block OTA software updates from re-triggering Setup Assistant.

  2. `ios26` (hardened `Manifest.db` engine; auto-selected when a device reports
     firmware above the verified iOS 26.x maximum, i.e. iOS 27.0+ / builds 24A+):
     Implements the iOS 26 G1 + `MCInstall` Hybrid Pipeline:
       - Generates an iOS 26.x `BackupAgent2`-hardened `Manifest.db` (or modifies a real
         device backup via `patch_ios26_existing_backup`), stripping `MDM.plist` and
         `MDMEvents.plist`, attaching `SPRINGBOARD_EXT_ATTR_BYTES`, synthesizing all
         ancestor directory rows (`flags=2`), and stamping strictly unique monotonic
         `InodeNumber` values across every row.
       - Runs the 6-point `verify_ios26_backup` verification gate before touching the device.
       - Pairs with direct USB `MobileConfigService.set_cloud_configuration` (`MCInstall`)
         and `lockdownd` completion flags.

  3. `modern` (surgical `Manifest.mbdb` for iOS 15 - 26.x):
     Builds a minimal `Manifest.mbdb` backup containing ONLY the MDM / DEP / Setup
     Assistant bypass domains without any legacy iOS 9.1 files.

  4. `sqlite` (`Manifest.db` SQLite3 format):
     Builds a modern Backup v3.3 (`Status.plist` Version=3.3, `Manifest.db` SQLite3
     database with `NSKeyedArchiver` `MBFile` blobs and 2-char SHA-1 subdirectories).

  5. `legacy`:
     Reproduces the exact original `MDMPatcher-Enhanced` / `MDMPatcher-Universal`
     backup for historical comparison or testing.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import io
import os
from pathlib import Path
import plistlib
import shutil
from typing import Literal
import uuid
import zipfile

from .crypto import deobfuscate_payload
from .ios26 import (
    MIN_SETUP_VERSION,
    atomic_write_plist,
    build_ios26_mdm_payloads,
    inject_files_into_ios26_backup,
    parse_ios_major_version,
    verify_ios26_backup,
)
from .manifest_db import ManifestDbEntry, write_manifest_db
from .mbdb import MODE_DIR, MODE_FILE, Mbdb
from .profiles import (
    HOME_DOMAIN,
    MANAGED_PREFS_DOMAIN,
    ROOT_DOMAIN,
    SYS_CONFIG_PROFILE_DOMAIN,
    SYSTEM_PREFS_DOMAIN,
    build_cloud_config_details_plist,
    build_empty_dict_plist,
    build_home_purplebuddy_plist,
    build_managed_purplebuddy_plist,
    build_mc_data_migration_plist,
    build_mcmeta_plist,
    build_ota_disabler_plist,
    build_payload_manifest_plist,
    build_user_settings_plist,
)

PatchMode = Literal["enhanced", "ios26", "modern", "sqlite", "legacy"]


@dataclass
class DeviceIdentity:
    udid: str = "00008030-001854E42E06402E"
    serial_number: str = "F17F4MLSPLK2"
    product_type: str = "iPhone12,8"
    build_version: str = "18C66"
    product_version: str = "18.0"
    imei: str = ""
    device_name: str = "iPhone"


@dataclass
class PatchConfig:
    mode: PatchMode = "enhanced"
    block_ota: bool = False
    is_supervised: bool = False
    organization_name: str = ""
    assets_dir: Path | None = None


def _target_setup_version(device: DeviceIdentity) -> int:
    """
    `SetupVersion` to stamp into `com.apple.purplebuddy.plist`: the device's own
    iOS major version, never below `MIN_SETUP_VERSION` (26).
    """
    return max(
        MIN_SETUP_VERSION,
        parse_ios_major_version(device.product_version, device.build_version),
    )


def default_assets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets"


def load_template_assets(
    assets_dir: Path | None = None,
) -> tuple[bytes, bytes, bytes]:
    """
    Load `(info_plist_bytes, manifest_plist_bytes, archive_zip_bytes)`.
    Uses the deobfuscated `clean_*` files if present, or dynamically decrypts
    the obfuscated `extension1.pdf`, `extension2.pdf`, and `libiMobileeDevice.dylib`
    using our reverse-engineered RNCryptor v3 + byte-swap pipeline.
    """
    adir = Path(assets_dir) if assets_dir else default_assets_dir()

    clean_info = adir / "clean_info.plist"
    clean_manifest = adir / "clean_manifest.plist"
    clean_zip = adir / "clean_archive.zip"

    if clean_info.exists() and clean_manifest.exists() and clean_zip.exists():
        return (
            clean_info.read_bytes(),
            clean_manifest.read_bytes(),
            clean_zip.read_bytes(),
        )

    return (
        deobfuscate_payload((adir / "extension1.pdf").read_bytes()),
        deobfuscate_payload((adir / "extension2.pdf").read_bytes()),
        deobfuscate_payload((adir / "libiMobileeDevice.dylib").read_bytes()),
    )


def patch_info_plist_bytes(
    raw_info_xml: bytes,
    device: DeviceIdentity,
    *,
    modernize_versions: bool = True,
) -> bytes:
    """
    Patch `Info.plist` with the target device's identity.
    Preserves the exact placeholder replacement logic from `MDMPatcher-Universal`
    and additionally updates `Product Version` and `Device Name` when `modernize_versions=True`.
    """
    text = raw_info_xml.decode("utf-8")
    text = text.replace("18C66", device.build_version)
    if not device.imei:
        text = text.replace(
            "\t<key>IMEI</key>\n\t<string>357145413514797</string>\n", ""
        )
        text = text.replace(
            "<key>IMEI</key>\n\t<string>357145413514797</string>", ""
        )
    else:
        text = text.replace("357145413514797", device.imei)
    text = text.replace("iPhone12,8", device.product_type)
    text = text.replace("F17F4MLSPLK2", device.serial_number)
    text = text.replace("00008030-001854E42E06402E", device.udid)

    info_bytes = text.encode("utf-8")
    if modernize_versions:
        try:
            plist_data = plistlib.loads(info_bytes)
            if device.product_version:
                plist_data["Product Version"] = device.product_version
            if device.device_name:
                plist_data["Device Name"] = device.device_name
                plist_data["Display Name"] = device.device_name
            return plistlib.dumps(plist_data, fmt=plistlib.FMT_XML)
        except Exception:
            pass
    return info_bytes


def patch_manifest_plist_bytes(
    raw_manifest_xml: bytes,
    device: DeviceIdentity,
    *,
    modernize_versions: bool = True,
    use_sqlite_version: bool = False,
) -> bytes:
    """
    Patch `Manifest.plist` with the target device's identity.
    """
    text = raw_manifest_xml.decode("utf-8")
    text = text.replace("18C66", device.build_version)
    text = text.replace("iPhone12,8", device.product_type)
    text = text.replace("F17F4MLSPLK2", device.serial_number)
    text = text.replace("00008030-001854E42E06402E", device.udid)

    manifest_bytes = text.encode("utf-8")
    if modernize_versions or use_sqlite_version:
        try:
            plist_data = plistlib.loads(manifest_bytes)
            lockdown = plist_data.get("Lockdown", {})
            if isinstance(lockdown, dict):
                if device.product_version:
                    lockdown["ProductVersion"] = device.product_version
                if device.device_name:
                    lockdown["DeviceName"] = device.device_name
                lockdown["BuildVersion"] = device.build_version
                lockdown["ProductType"] = device.product_type
                lockdown["SerialNumber"] = device.serial_number
                lockdown["UniqueDeviceID"] = device.udid
                plist_data["Lockdown"] = lockdown
            if modernize_versions:
                plist_data["SystemDomainsVersion"] = "20.0"
            if use_sqlite_version:
                plist_data["Version"] = "10.2"
                plist_data["SystemDomainsVersion"] = "24.0"
                plist_data["Applications"] = {}
            return plistlib.dumps(plist_data, fmt=plistlib.FMT_BINARY)
        except Exception:
            pass
    return manifest_bytes


def _write_backup_file(
    backup_dir: Path,
    file_id: str,
    data: bytes,
    *,
    sharded: bool = False,
) -> Path:
    if sharded:
        sub = backup_dir / file_id[:2]
        sub.mkdir(parents=True, exist_ok=True)
        target = sub / file_id
    else:
        target = backup_dir / file_id
    target.write_bytes(data)
    return target


def build_backup(
    output_root: str | Path,
    device: DeviceIdentity,
    config: PatchConfig | None = None,
) -> Path:
    """
    Build a complete MDM-patched backup inside `output_root/MDMB` and return
    the path to the `MDMB` backup directory.

    Note: `idevicebackup2 restore --system --settings <output_root>` (with `-s MDMB`)
    expects `<output_root>/MDMB/{Info.plist, Manifest.plist, Status.plist, Manifest.mbdb|Manifest.db, ...}`.
    """
    cfg = config or PatchConfig()
    output_root = Path(output_root)
    mdmb_dir = output_root / "MDMB"
    if mdmb_dir.exists():
        shutil.rmtree(mdmb_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    info_tpl, manifest_tpl, archive_zip_bytes = load_template_assets(
        cfg.assets_dir
    )

    if cfg.mode in ("legacy", "enhanced"):
        # Extract clean_archive.zip (creates output_root/MDMB/*)
        with zipfile.ZipFile(io.BytesIO(archive_zip_bytes), "r") as zf:
            zf.extractall(output_root)

        modernize = cfg.mode == "enhanced"
        (mdmb_dir / "Info.plist").write_bytes(
            patch_info_plist_bytes(
                info_tpl, device, modernize_versions=modernize
            )
        )
        (mdmb_dir / "Manifest.plist").write_bytes(
            patch_manifest_plist_bytes(
                manifest_tpl, device, modernize_versions=modernize
            )
        )

        if cfg.mode == "enhanced":
            _apply_enhanced_mbdb_upgrades(mdmb_dir, device, cfg)

        return mdmb_dir

    if cfg.mode == "modern":
        mdmb_dir.mkdir(parents=True, exist_ok=True)
        (mdmb_dir / "Info.plist").write_bytes(
            patch_info_plist_bytes(info_tpl, device, modernize_versions=True)
        )
        (mdmb_dir / "Manifest.plist").write_bytes(
            patch_manifest_plist_bytes(
                manifest_tpl, device, modernize_versions=True
            )
        )
        status_data = {
            "BackupState": "new",
            "Date": datetime.datetime.now(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            "IsFullBackup": False,
            "SnapshotState": "finished",
            "UUID": str(uuid.uuid4()).upper(),
            "Version": "2.4",
        }
        (mdmb_dir / "Status.plist").write_bytes(
            plistlib.dumps(status_data, fmt=plistlib.FMT_BINARY)
        )
        _build_surgical_modern_mbdb(mdmb_dir, device, cfg)
        return mdmb_dir

    if cfg.mode == "sqlite":
        mdmb_dir.mkdir(parents=True, exist_ok=True)
        (mdmb_dir / "Info.plist").write_bytes(
            patch_info_plist_bytes(info_tpl, device, modernize_versions=True)
        )
        (mdmb_dir / "Manifest.plist").write_bytes(
            patch_manifest_plist_bytes(
                manifest_tpl,
                device,
                modernize_versions=True,
                use_sqlite_version=True,
            )
        )
        status_data = {
            "BackupState": "new",
            "Date": datetime.datetime.now(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            "IsFullBackup": False,
            "SnapshotState": "finished",
            "UUID": str(uuid.uuid4()).upper(),
            "Version": "3.3",
        }
        (mdmb_dir / "Status.plist").write_bytes(
            plistlib.dumps(status_data, fmt=plistlib.FMT_BINARY)
        )
        _build_sqlite_manifest_backup(mdmb_dir, device, cfg)
        return mdmb_dir

    if cfg.mode == "ios26":
        mdmb_dir.mkdir(parents=True, exist_ok=True)
        (mdmb_dir / "Info.plist").write_bytes(
            patch_info_plist_bytes(info_tpl, device, modernize_versions=True)
        )
        (mdmb_dir / "Manifest.plist").write_bytes(
            patch_manifest_plist_bytes(
                manifest_tpl,
                device,
                modernize_versions=True,
                use_sqlite_version=True,
            )
        )
        status_data = {
            "BackupState": "new",
            "Date": datetime.datetime.now(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            "IsFullBackup": False,
            "SnapshotState": "finished",
            "UUID": str(uuid.uuid4()).upper(),
            "Version": "3.3",
        }
        atomic_write_plist(
            mdmb_dir / "Status.plist", status_data, fmt=plistlib.FMT_BINARY
        )
        _build_ios26_g1_backup(mdmb_dir, device, cfg)
        return mdmb_dir

    raise ValueError(f"Unsupported patch mode: {cfg.mode}")


def _apply_enhanced_mbdb_upgrades(
    mdmb_dir: Path,
    device: DeviceIdentity,
    cfg: PatchConfig,
) -> None:
    """
    Upgrade an extracted `MDMB` (`Manifest.mbdb`) backup directory in-place so
    it works across iOS 9 - 26.x (iOS 27.0+ best-effort):
      1. Inject `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`
         `Library/ConfigurationProfiles/CloudConfigurationDetails.plist` and
         `Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist`.
      2. Inject `ManagedPreferencesDomain` `mobile/com.apple.purplebuddy.plist`.
      3. Upgrade `HomeDomain` `Library/Preferences/com.apple.purplebuddy.plist`
         (fix `zh_CN` locale -> `en_US`, `SetupVersion` -> device major, min 26).
      4. Upgrade `HomeDomain` `Library/ConfigurationProfiles/PublicInfo/MCMeta.plist`
         (`LastMigratedBuild` -> `device.build_version`).
      5. Inject `HomeDomain` `Library/ConfigurationProfiles/CloudConfigurationDetails.plist`.
      6. Clean `SystemPreferencesDomain` `SystemConfiguration/com.apple.mobilegestalt.plist`
         so the device name is not overwritten with `"“iMyFone”的 iPhone"`.
      7. Optionally update `RootDomain` `Library/Preferences/com.apple.MobileAsset.plist`
         to block OTA updates.
    """
    mbdb_path = mdmb_dir / "Manifest.mbdb"
    mbdb = Mbdb.from_bytes(mbdb_path.read_bytes())

    cloud_cfg_bytes = build_cloud_config_details_plist(
        is_supervised=cfg.is_supervised,
        organization_name=cfg.organization_name,
    )
    managed_pb_bytes = build_managed_purplebuddy_plist()

    # 1. SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles
    for rel_path in (
        "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        "Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist",
    ):
        rec = mbdb.upsert_file(
            SYS_CONFIG_PROFILE_DOMAIN,
            rel_path,
            cloud_cfg_bytes,
            mode=MODE_FILE | 0o644,
            uid=501,
            gid=501,
        )
        _write_backup_file(mdmb_dir, rec.file_id, cloud_cfg_bytes)

    # 2. ManagedPreferencesDomain/mobile/com.apple.purplebuddy.plist
    rec = mbdb.upsert_file(
        MANAGED_PREFS_DOMAIN,
        "mobile/com.apple.purplebuddy.plist",
        managed_pb_bytes,
        mode=MODE_FILE | 0o644,
        uid=501,
        gid=501,
    )
    _write_backup_file(mdmb_dir, rec.file_id, managed_pb_bytes)

    # 3. HomeDomain/Library/Preferences/com.apple.purplebuddy.plist
    existing_pb_rec = mbdb.find_record(
        HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"
    )
    existing_pb_bytes: bytes | None = None
    if existing_pb_rec and (mdmb_dir / existing_pb_rec.file_id).exists():
        existing_pb_bytes = (mdmb_dir / existing_pb_rec.file_id).read_bytes()
    home_pb_bytes = build_home_purplebuddy_plist(
        existing_pb_bytes, setup_version=_target_setup_version(device)
    )
    pb_rec = mbdb.upsert_file(
        HOME_DOMAIN,
        "Library/Preferences/com.apple.purplebuddy.plist",
        home_pb_bytes,
        mode=MODE_FILE | 0o600,
        uid=501,
        gid=501,
    )
    _write_backup_file(mdmb_dir, pb_rec.file_id, home_pb_bytes)

    # 4. HomeDomain/Library/ConfigurationProfiles/PublicInfo/MCMeta.plist
    mcmeta_bytes = build_mcmeta_plist(device.build_version)
    mc_rec = mbdb.upsert_file(
        HOME_DOMAIN,
        "Library/ConfigurationProfiles/PublicInfo/MCMeta.plist",
        mcmeta_bytes,
        mode=MODE_FILE | 0o644,
        uid=501,
        gid=501,
    )
    _write_backup_file(mdmb_dir, mc_rec.file_id, mcmeta_bytes)

    # 5. HomeDomain/Library/ConfigurationProfiles/CloudConfigurationDetails.plist
    home_cc_rec = mbdb.upsert_file(
        HOME_DOMAIN,
        "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
        cloud_cfg_bytes,
        mode=MODE_FILE | 0o644,
        uid=501,
        gid=501,
    )
    _write_backup_file(mdmb_dir, home_cc_rec.file_id, cloud_cfg_bytes)

    # 6. Clean "“iMyFone”的 iPhone" from com.apple.mobilegestalt.plist
    mg_rec = mbdb.find_record(
        SYSTEM_PREFS_DOMAIN,
        "SystemConfiguration/com.apple.mobilegestalt.plist",
    )
    if mg_rec and (mdmb_dir / mg_rec.file_id).exists():
        try:
            mg_data = plistlib.loads((mdmb_dir / mg_rec.file_id).read_bytes())
            if isinstance(mg_data, dict):
                mg_data["UserAssignedDeviceName"] = (
                    device.device_name or "iPhone"
                )
                mg_bytes = plistlib.dumps(mg_data, fmt=plistlib.FMT_BINARY)
                mbdb.upsert_file(
                    SYSTEM_PREFS_DOMAIN,
                    "SystemConfiguration/com.apple.mobilegestalt.plist",
                    mg_bytes,
                    mode=mg_rec.mode,
                    uid=mg_rec.uid,
                    gid=mg_rec.gid,
                )
                _write_backup_file(mdmb_dir, mg_rec.file_id, mg_bytes)
        except Exception:
            pass

    # 7. Optional OTA update blocker
    if cfg.block_ota:
        ota_bytes = build_ota_disabler_plist()
        ota_rec = mbdb.upsert_file(
            ROOT_DOMAIN,
            "Library/Preferences/com.apple.MobileAsset.plist",
            ota_bytes,
            mode=MODE_FILE | 0o600,
            uid=0,
            gid=0,
        )
        _write_backup_file(mdmb_dir, ota_rec.file_id, ota_bytes)

    mbdb_path.write_bytes(mbdb.to_bytes())


def _collect_modern_payload_files(
    device: DeviceIdentity,
    cfg: PatchConfig,
) -> list[tuple[str, str, bytes, int, int, int]]:
    """
    Return `(domain, relative_path, data, mode, uid, gid)` tuples for surgical
    modern backups (`modern` and `sqlite` modes).
    """
    cloud_cfg_bytes = build_cloud_config_details_plist(
        is_supervised=cfg.is_supervised,
        organization_name=cfg.organization_name,
    )
    managed_pb_bytes = build_managed_purplebuddy_plist()
    home_pb_bytes = build_home_purplebuddy_plist(
        setup_version=_target_setup_version(device)
    )
    user_settings_bytes = build_user_settings_plist()
    mcmeta_bytes = build_mcmeta_plist(device.build_version)
    mc_migration_bytes = build_mc_data_migration_plist()
    payload_manifest_bytes = build_payload_manifest_plist()
    empty_dict_bytes = build_empty_dict_plist()

    fmode = MODE_FILE | 0o644
    items: list[tuple[str, str, bytes, int, int, int]] = [
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            cloud_cfg_bytes,
            fmode,
            501,
            501,
        ),
        (
            SYS_CONFIG_PROFILE_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist",
            cloud_cfg_bytes,
            fmode,
            501,
            501,
        ),
        (
            MANAGED_PREFS_DOMAIN,
            "mobile/com.apple.purplebuddy.plist",
            managed_pb_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/Preferences/com.apple.purplebuddy.plist",
            home_pb_bytes,
            MODE_FILE | 0o600,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            cloud_cfg_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/UserSettings.plist",
            user_settings_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/PublicInfo/EffectiveUserSettings.plist",
            user_settings_bytes,
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
            mc_migration_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/PayloadManifest.plist",
            payload_manifest_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/ClientTruth.plist",
            empty_dict_bytes,
            fmode,
            501,
            501,
        ),
        (
            HOME_DOMAIN,
            "Library/ConfigurationProfiles/ProfileTruth.plist",
            empty_dict_bytes,
            fmode,
            501,
            501,
        ),
    ]

    if cfg.block_ota:
        items.append(
            (
                ROOT_DOMAIN,
                "Library/Preferences/com.apple.MobileAsset.plist",
                build_ota_disabler_plist(),
                MODE_FILE | 0o600,
                0,
                0,
            )
        )
    return items


def _build_surgical_modern_mbdb(
    mdmb_dir: Path,
    device: DeviceIdentity,
    cfg: PatchConfig,
) -> None:
    mbdb = Mbdb()
    for domain, rel_path, data, mode, uid, gid in _collect_modern_payload_files(
        device, cfg
    ):
        rec = mbdb.upsert_file(
            domain,
            rel_path,
            data,
            mode=mode,
            uid=uid,
            gid=gid,
        )
        _write_backup_file(mdmb_dir, rec.file_id, data, sharded=False)

    (mdmb_dir / "Manifest.mbdb").write_bytes(mbdb.to_bytes())


def _build_sqlite_manifest_backup(
    mdmb_dir: Path,
    device: DeviceIdentity,
    cfg: PatchConfig,
) -> None:
    entries: list[ManifestDbEntry] = []
    for domain, rel_path, data, mode, uid, gid in _collect_modern_payload_files(
        device, cfg
    ):
        entry = ManifestDbEntry(
            domain=domain,
            relative_path=rel_path,
            data=data,
            mode=mode,
            uid=uid,
            gid=gid,
            flags=1,
        )
        entries.append(entry)
        _write_backup_file(mdmb_dir, entry.file_id, data, sharded=True)

    write_manifest_db(mdmb_dir / "Manifest.db", entries)


def _build_ios26_g1_backup(
    mdmb_dir: Path,
    device: DeviceIdentity,
    cfg: PatchConfig,
) -> None:
    """
    Build an iOS 26.x `BackupAgent2`-hardened `Manifest.db` backup and run the
    6-point `verify_ios26_backup` verification gate.
    """
    write_manifest_db(mdmb_dir / "Manifest.db", [])
    payloads = build_ios26_mdm_payloads(
        build_version=device.build_version,
        product_version=device.product_version,
        is_supervised=cfg.is_supervised,
        organization_name=cfg.organization_name,
        block_ota=cfg.block_ota,
    )
    injected, unchanged, _dirs, failed = inject_files_into_ios26_backup(
        mdmb_dir,
        payloads,
        merge_existing_plists=True,
    )
    problems = verify_ios26_backup(mdmb_dir, payloads)
    if failed > 0 or problems:
        raise RuntimeError(
            "iOS 26.x backup verification gate failed:\n  - "
            + "\n  - ".join(problems or [f"{failed} payload(s) failed injection"])
        )
