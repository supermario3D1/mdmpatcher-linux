"""
Unit and integration tests for MDMPatcher-Linux (iOS 9 - iOS 27+):
  1. Reverse-engineered RNCryptor v3 + 6-pair byte swap deobfuscation
  2. Round-trip obfuscation/deobfuscation
  3. Manifest.mbdb binary parsing, modification, and serialization
  4. Manifest.db (SQLite3 + NSKeyedArchiver MBFile + ExtendedAttributes + unique InodeNumber)
  5. iOS 27 G1 Pipeline: MDM.plist/MDMEvents.plist stripping, Donor-Blob Cloning,
     parent directory synthesis, 6-point verification gate, and `.b/<n>/` path normalization
  6. Backup generation across all 5 modes: enhanced, ios27, modern, sqlite, legacy
  7. CLI subcommand execution (`deobfuscate`, `build-backup`, `patch-backup`, `inspect`)
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
from mdmpatcher.ios27 import (
    build_ios27_mdm_payloads,
    is_ios26_or_newer,
    is_ios27_or_newer,
    normalize_ios27_upload_path,
    patch_ios27_existing_backup,
    verify_ios27_backup,
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
            build_version="24A335",
            product_version="27.0",
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

            self.assertEqual(info["Build Version"], "24A335")
            self.assertEqual(info["Product Version"], "27.0")
            self.assertEqual(info["Product Type"], "iPhone17,2")
            self.assertEqual(info["Serial Number"], "G6TZ1234ABCD")
            self.assertEqual(info["Unique Identifier"], "00008130-0011223344556677")
            self.assertEqual(info["IMEI"], "359999111222333")

            self.assertEqual(manifest["SystemDomainsVersion"], "20.0")
            self.assertEqual(manifest["Lockdown"]["ProductVersion"], "27.0")
            self.assertEqual(manifest["Lockdown"]["BuildVersion"], "24A335")

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

            # Verify HomeDomain purplebuddy.plist was cleaned (no zh_CN locale, SetupVersion=27)
            hpb_rec = mbdb.find_record(
                HOME_DOMAIN, "Library/Preferences/com.apple.purplebuddy.plist"
            )
            self.assertIsNotNone(hpb_rec)
            hpb_plist = plistlib.loads((backup_dir / hpb_rec.file_id).read_bytes())
            self.assertEqual(hpb_plist["Locale"], "en_US")
            self.assertTrue(hpb_plist["SetupDone"])
            self.assertEqual(hpb_plist["SetupVersion"], 27)

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
            self.assertEqual(mc_plist["LastMigratedBuild"], "24A335")

    def test_modern_sqlite_and_ios27_modes(self) -> None:
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

            ios27_dir = build_backup(
                Path(tmp) / "ios27",
                self.sample_device,
                PatchConfig(mode="ios27", block_ota=True),
            )
            self.assertTrue((ios27_dir / "Manifest.db").exists())
            expected = build_ios27_mdm_payloads(
                build_version=self.sample_device.build_version,
                block_ota=True,
            )
            problems = verify_ios27_backup(ios27_dir, expected)
            self.assertEqual(problems, [])

    def test_ios27_version_detection_and_path_normalization(self) -> None:
        self.assertTrue(is_ios27_or_newer("27.0", "24A335"))
        self.assertTrue(is_ios27_or_newer(None, "24A5264n"))
        self.assertFalse(is_ios27_or_newer("26.1", "23B82"))
        self.assertTrue(is_ios26_or_newer("26.1", "23B82"))
        self.assertFalse(is_ios26_or_newer("18.2", "22C150"))

        self.assertEqual(
            normalize_ios27_upload_path("/.b/1/Media/DCIM/IMG_0001.PNG"),
            "Media/DCIM/IMG_0001.PNG",
        )
        self.assertEqual(
            normalize_ios27_upload_path("b/42/Library/Preferences/foo.plist"),
            "Library/Preferences/foo.plist",
        )

    def test_ios27_existing_backup_strip_and_donor_injection(self) -> None:
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

            report = patch_ios27_existing_backup(
                bkp_dir,
                udid="00008140-AABBCCDDEEFF0011",
                serial_number="NEW_SERIAL_27",
                build_version="24A335",
                product_version="27.0",
                block_ota=True,
            )
            self.assertTrue(report.verified, f"Problems: {report.verification_problems}")
            self.assertEqual(len(report.stripped_mdm_files), 2)

            mp = plistlib.loads((bkp_dir / "Manifest.plist").read_bytes())
            self.assertEqual(mp["Lockdown"]["UniqueDeviceID"], "00008140-AABBCCDDEEFF0011")
            self.assertEqual(mp["Lockdown"]["SerialNumber"], "NEW_SERIAL_27")

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
                    "ios27",
                    "-u",
                    "00008130-0011223344556677",
                    "-t",
                    "iPhone17,2",
                    "-b",
                    "24A335",
                    "-i",
                    "27.0",
                ]
            )
            self.assertEqual(rc2, 0)

            rc3 = cli_main(["patch-backup", str(bkp_out / "MDMB"), "--block-ota"])
            self.assertEqual(rc3, 0)

            rc4 = cli_main(["inspect", str(bkp_out)])
            self.assertEqual(rc4, 0)


if __name__ == "__main__":
    unittest.main()
