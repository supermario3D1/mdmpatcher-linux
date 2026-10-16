"""
Unit and integration tests for MDMPatcher-Linux (verified on iOS 9 - iOS 26.x):
  1. Reverse-engineered RNCryptor v3 + 6-pair byte swap deobfuscation
  2. Round-trip obfuscation/deobfuscation
  3. Manifest.mbdb binary parsing, modification, and serialization
  4. Manifest.db (SQLite3 + NSKeyedArchiver MBFile + ExtendedAttributes + unique InodeNumber)
  5. G1 Pipeline: MDM.plist/MDMEvents.plist stripping, Donor-Blob Cloning,
     parent directory synthesis, 6-point verification gate, and `.b/<n>/` path normalization
  6. Backup generation across all 5 modes: enhanced, ios26, modern, sqlite, legacy
  7. Verified support window policy (iOS 9 - 26.x; iOS 27+ warned, not blocked)
  8. CLI subcommand execution (`deobfuscate`, `build-backup`, `patch-backup`, `inspect`)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

from mdmpatcher.cli import main as cli_main
from mdmpatcher.crypto import (
    apply_byte_swaps,
    deobfuscate_assets,
    deobfuscate_payload,
    derive_mdmpatcher_password,
    obfuscate_payload,
)
from mdmpatcher.ios26 import (
    MAX_SUPPORTED_IOS_MAJOR,
    MIN_SUPPORTED_IOS_MAJOR,
    build_ios26_mdm_payloads,
    ios_support_state,
    is_above_supported_ios,
    is_ios26_or_newer,
    normalize_ios26_upload_path,
    patch_ios26_existing_backup,
    unsupported_ios_notice,
    verify_ios26_backup,
    warn_if_unsupported_ios,
)
from mdmpatcher.manifest_db import read_manifest_db
from mdmpatcher.mbdb import MODE_FILE, Mbdb, compute_file_id
from mdmpatcher.patcher import (
    DeviceIdentity,
    PatchConfig,
    build_backup,
    default_assets_dir,
)
from mdmpatcher.profiles import (
    HOME_DOMAIN,
    MANAGED_PREFS_DOMAIN,
    SYS_CONFIG_PROFILE_DOMAIN,
    SYSTEM_PREFS_DOMAIN,
)


class TestMDMPatcherLinux(unittest.TestCase):
    def setUp(self) -> None:
        self.assets_dir = default_assets_dir()
        self.sample_device = DeviceIdentity(
            udid="00008130-0011223344556677",
            serial_number="G6TZ1234ABCD",
            product_type="iPhone17,2",
            build_version="23A345",
            product_version="26.0",
            imei="359999111222333",
            device_name="Test iPhone 17 Pro Max",
        )

    def test_password_derivation_and_byte_swaps(self) -> None:
        pw = derive_mdmpatcher_password()
        self.assertIn(b"-4.0418364248546965e+26", pw)
        self.assertNotIn(b"q", pw)

        sample = bytes(range(256)) * 2  # 512 bytes >= 346
        swapped = apply_byte_swaps(sample)
        self.assertNotEqual(sample, swapped)
        self.assertEqual(apply_byte_swaps(swapped), sample)

    def test_deobfuscate_matches_clean_reference_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = deobfuscate_assets(self.assets_dir, tmp)
            for clean_name in (
                "clean_info.plist",
                "clean_manifest.plist",
                "clean_archive.zip",
            ):
                expected = (self.assets_dir / clean_name).read_bytes()
                actual = results[clean_name].read_bytes()
                self.assertEqual(
                    actual,
                    expected,
                    f"Deobfuscated {clean_name} did not match reference file byte-for-byte",
                )

    def test_obfuscate_round_trip(self) -> None:
        plaintext = b"A" * 500 + b"MDMPatcher-Linux-Test-Payload"
        obf = obfuscate_payload(plaintext)
        self.assertNotEqual(obf, plaintext)
        rec = deobfuscate_payload(obf)
        self.assertEqual(rec, plaintext)

    def test_mbdb_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = build_backup(
                tmp, self.sample_device, PatchConfig(mode="legacy")
            )
            mbdb_bytes = (legacy_dir / "Manifest.mbdb").read_bytes()
            mbdb = Mbdb.from_bytes(mbdb_bytes)
            self.assertEqual(len(mbdb.records), 117)
            self.assertEqual(mbdb.to_bytes(), mbdb_bytes)

    def test_enhanced_mode_backup_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = build_backup(
                tmp,
                self.sample_device,
                PatchConfig(mode="enhanced", block_ota=True),
            )
            info = plistlib.loads((backup_dir / "Info.plist").read_bytes())
            manifest = plistlib.loads((backup_dir / "Manifest.plist").read_bytes())

            self.assertEqual(info["Build Version"], "23A345")
            self.assertEqual(info["Product Version"], "26.0")
            self.assertEqual(info["Product Type"], "iPhone17,2")
            self.assertEqual(info["Serial Number"], "G6TZ1234ABCD")
            self.assertEqual(info["Unique Identifier"], "00008130-0011223344556677")
            self.assertEqual(info["IMEI"], "359999111222333")

            self.assertEqual(manifest["SystemDomainsVersion"], "20.0")
            self.assertEqual(manifest["Lockdown"]["ProductVersion"], "26.0")
            self.assertEqual(manifest["Lockdown"]["BuildVersion"], "23A345")

            mbdb = Mbdb.from_bytes((backup_dir / "Manifest.mbdb").read_bytes())

            # Verify SysSharedContainerDomain CloudConfigurationDetails.plist
            cc_rec = mbdb.find_record(
                SYS_CONFIG_PROFILE_DOMAIN,
                "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            )
            self.assertIsNotNone(cc_rec)
            cc_path = backup_dir / cc_rec.file_id
            self.assertTrue(cc_path.exists())
            cc_bytes = cc_path.read_bytes()
            self.assertEqual(cc_rec.filelen, len(cc_bytes))
            self.assertEqual(cc_rec.datahash, hashlib.sha1(cc_bytes).digest())
            cc_plist = plistlib.loads(cc_bytes)
            self.assertTrue(cc_plist["CloudConfigurationUIComplete"])
            self.assertFalse(cc_plist["IsSupervised"])
            self.assertIn("WiFi", cc_plist["SkipSetup"])
            self.assertIn("Intelligence", cc_plist["SkipSetup"])
            self.assertIn("OSShowcase", cc_plist["SkipSetup"])
            self.assertIn("SafetyAndHandling", cc_plist["SkipSetup"])
            self.assertIn("AgeBasedSafetySettings", cc_plist["SkipSetup"])

            # Verify ManagedPreferencesDomain purplebuddy.plist
            mpb_rec = mbdb.find_record(
                MANAGED_PREFS_DOMAIN, "mobile/com.apple.purplebuddy.plist"
            )
            self.assertIsNotNone(mpb_rec)
            mpb_plist = plistlib.loads((backup_dir / mpb_rec.file_id).read_bytes())
            self.assertTrue(mpb_plist["SetupDone"])
            self.assertTrue(mpb_plist["SetupFinishedAllSteps"])

            # Verify HomeDomain purplebuddy.plist was cleaned (no zh_CN locale,
            # SetupVersion == the device's own iOS major, floored at 26)
            hpb_rec = mbdb.find_record(
                HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"
            )
            self.assertIsNotNone(hpb_rec)
            hpb_plist = plistlib.loads((backup_dir / hpb_rec.file_id).read_bytes())
            self.assertEqual(hpb_plist["Locale"], "en_US")
            self.assertTrue(hpb_plist["SetupDone"])
            self.assertEqual(hpb_plist["SetupVersion"], 26)

            # Verify legacy iMyFone device name was cleaned
            mg_rec = mbdb.find_record(
                SYSTEM_PREFS_DOMAIN,
                "SystemConfiguration/com.apple.mobilegestalt.plist",
            )
            self.assertIsNotNone(mg_rec)
            mg_plist = plistlib.loads((backup_dir / mg_rec.file_id).read_bytes())
            self.assertEqual(
                mg_plist["UserAssignedDeviceName"], "Test iPhone 17 Pro Max"
            )

            # Verify MCMeta.plist LastMigratedBuild matches target build_version
            mc_rec = mbdb.find_record(
                HOME_DOMAIN,
                "Library/ConfigurationProfiles/PublicInfo/MCMeta.plist",
            )
            self.assertIsNotNone(mc_rec)
            mc_plist = plistlib.loads((backup_dir / mc_rec.file_id).read_bytes())
            self.assertEqual(mc_plist["LastMigratedBuild"], "23A345")

    def test_modern_sqlite_and_ios26_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mod_dir = build_backup(
                Path(tmp) / "modern",
                self.sample_device,
                PatchConfig(mode="modern"),
            )
            self.assertTrue((mod_dir / "Manifest.mbdb").exists())

            sql_dir = build_backup(
                Path(tmp) / "sqlite",
                self.sample_device,
                PatchConfig(mode="sqlite"),
            )
            self.assertTrue((sql_dir / "Manifest.db").exists())
            entries = read_manifest_db(sql_dir / "Manifest.db")
            self.assertGreater(len(entries), 15)

            ios26_dir = build_backup(
                Path(tmp) / "ios26",
                self.sample_device,
                PatchConfig(mode="ios26", block_ota=True),
            )
            self.assertTrue((ios26_dir / "Manifest.db").exists())
            expected = build_ios26_mdm_payloads(
                build_version=self.sample_device.build_version,
                product_version=self.sample_device.product_version,
                block_ota=True,
            )
            problems = verify_ios26_backup(ios26_dir, expected)
            self.assertEqual(problems, [])

    def test_ios26_version_detection_and_path_normalization(self) -> None:
        self.assertTrue(is_above_supported_ios("27.0", "24A335"))
        self.assertTrue(is_above_supported_ios(None, "24A5264n"))
        self.assertFalse(is_above_supported_ios("26.1", "23B82"))
        self.assertTrue(is_ios26_or_newer("26.1", "23B82"))
        self.assertFalse(is_ios26_or_newer("18.2", "22C150"))

        self.assertEqual(
            normalize_ios26_upload_path("/.b/1/Media/DCIM/IMG_0001.PNG"),
            "Media/DCIM/IMG_0001.PNG",
        )
        self.assertEqual(
            normalize_ios26_upload_path("b/42/Library/Preferences/foo.plist"),
            "Library/Preferences/foo.plist",
        )

    def test_ios26_existing_backup_strip_and_donor_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bkp_dir = build_backup(
                Path(tmp) / "donor",
                self.sample_device,
                PatchConfig(mode="sqlite"),
            )
            # Simulate an active MDM.plist and MDMEvents.plist in the backup
            conn = sqlite3.connect(str(bkp_dir / "Manifest.db"))
            for rel in (
                "Library/ConfigurationProfiles/MDM.plist",
                "Library/ConfigurationProfiles/MDMEvents.plist",
            ):
                fid = compute_file_id(SYS_CONFIG_PROFILE_DOMAIN, rel)
                p = bkp_dir / fid[:2] / fid
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"active-mdm-lock")
                conn.execute(
                    "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, 1, ?)",
                    (fid, SYS_CONFIG_PROFILE_DOMAIN, rel, b"dummy"),
                )
            conn.commit()
            conn.close()

            report = patch_ios26_existing_backup(
                bkp_dir,
                udid="00008140-AABBCCDDEEFF0011",
                serial_number="NEW_SERIAL_26",
                build_version="23A345",
                product_version="26.0",
                block_ota=True,
            )
            self.assertTrue(report.verified, f"Problems: {report.verification_problems}")
            self.assertEqual(len(report.stripped_mdm_files), 2)

            mp = plistlib.loads((bkp_dir / "Manifest.plist").read_bytes())
            self.assertEqual(mp["Lockdown"]["UniqueDeviceID"], "00008140-AABBCCDDEEFF0011")
            self.assertEqual(mp["Lockdown"]["SerialNumber"], "NEW_SERIAL_26")

    def test_verified_support_window_policy(self) -> None:
        """iOS 9 - iOS 26.x is the verified window; iOS 27+ warns but still runs."""
        self.assertEqual(MIN_SUPPORTED_IOS_MAJOR, 9)
        self.assertEqual(MAX_SUPPORTED_IOS_MAJOR, 26)

        verified = (
            ("26.0", "23A345"),
            ("26.7", "23G99"),
            ("18.2", "22C150"),
            ("9.0", "13A344"),
        )
        for product_version, build_version in verified:
            self.assertEqual(
                ios_support_state(product_version, build_version), "verified"
            )
            self.assertFalse(is_above_supported_ios(product_version, build_version))
            self.assertEqual(
                unsupported_ios_notice(product_version, build_version), []
            )

        unverified = (("27.0", "24A335"), ("27.1", "24B92"), (None, "24A5264n"))
        for product_version, build_version in unverified:
            self.assertEqual(
                ios_support_state(product_version, build_version), "unverified"
            )
            self.assertTrue(is_above_supported_ios(product_version, build_version))
            notice = unsupported_ios_notice(product_version, build_version)
            self.assertTrue(notice, "iOS 27+ must produce a warning")
            self.assertTrue(any("UNVERIFIED" in line for line in notice))
            self.assertTrue(any("iOS 26.x" in line for line in notice))

        # Warnings are routed through the caller's callback...
        emitted: list[str] = []
        self.assertTrue(
            warn_if_unsupported_ios("27.0", "24A335", on_output=emitted.append)
        )
        self.assertTrue(emitted)
        # ...and nothing is emitted inside the verified window.
        emitted.clear()
        self.assertFalse(
            warn_if_unsupported_ios("26.1", "23B82", on_output=emitted.append)
        )
        self.assertEqual(emitted, [])

        # Anything below iOS 9 is reported as out-of-window (best-effort).
        self.assertEqual(ios_support_state("8.0", None), "below")

    def test_setup_version_tracks_target_device(self) -> None:
        """`SetupVersion` follows the target's own iOS major, floored at 26."""
        cases = (
            ("26.0", "23A345", 26),
            ("26.7", "23G99", 26),
            ("27.0", "24A335", 27),
            ("18.2", "22C150", 26),
        )
        for product_version, build_version, expected in cases:
            payloads = build_ios26_mdm_payloads(
                build_version=build_version,
                product_version=product_version,
            )
            home_pb = next(
                p
                for p in payloads
                if p[1] == "Library/Preferences/com.apple.purplebuddy.plist"
            )
            plist = plistlib.loads(home_pb[2])
            self.assertEqual(
                plist["SetupVersion"],
                expected,
                f"unexpected SetupVersion for {product_version} ({build_version})",
            )

    def test_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deob_out = Path(tmp) / "deob"
            rc1 = cli_main(["deobfuscate", "-o", str(deob_out)])
            self.assertEqual(rc1, 0)
            self.assertTrue((deob_out / "clean_archive.zip").exists())

            bkp_out = Path(tmp) / "bkp"
            rc2 = cli_main(
                [
                    "build-backup",
                    "-o",
                    str(bkp_out),
                    "-m",
                    "ios26",
                    "-u",
                    "00008130-0011223344556677",
                    "-t",
                    "iPhone17,2",
                    "-b",
                    "23A345",
                    "-i",
                    "26.0",
                ]
            )
            self.assertEqual(rc2, 0)

            # Firmware above the verified maximum is warned about, not blocked.
            above_out = Path(tmp) / "bkp_above_max"
            rc2b = cli_main(
                [
                    "build-backup",
                    "-o",
                    str(above_out),
                    "-m",
                    "ios26",
                    "-u",
                    "00008130-0011223344556677",
                    "-t",
                    "iPhone18,1",
                    "-b",
                    "24A335",
                    "-i",
                    "27.0",
                ]
            )
            self.assertEqual(rc2b, 0)

            rc3 = cli_main(["patch-backup", str(bkp_out / "MDMB"), "--block-ota"])
            self.assertEqual(rc3, 0)

            rc4 = cli_main(["inspect", str(bkp_out)])
            self.assertEqual(rc4, 0)


if __name__ == "__main__":
    unittest.main()
