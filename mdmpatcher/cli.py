"""
Command-line interface and optional Linux GUI for MDMPatcher-Linux (iOS 9 - iOS 27+).

Subcommands:
  - `info`:         Detect connected iPhone/iPad over USB and display device info.
  - `patch`:        Detect connected iPhone/iPad, build MDM-patched backup, and restore.
                    Automatically enables the iOS 27 Multi-Vector Engine on iOS 27+ (builds 24A+).
  - `skip-setup`:   Direct USB `MobileConfigService` (`com.apple.mobile.MCInstall`) +
                    `lockdownd` Setup Assistant & CloudConfiguration bypass (no backup restore;
                    zero partial-restore factory-reset risk on iOS 27).
  - `patch-backup`: Apply the iOS 27 G1 MDM-strip + Donor-Cloned `Manifest.db` injection +
                    6-point verification gate to an existing device backup directory.
  - `build-backup`: Generate an MDM-patched backup directory offline (supports `enhanced`,
                    `ios27`, `modern`, `sqlite`, and `legacy` modes).
  - `deobfuscate`:  Decrypt and unswap `extension1.pdf`, `extension2.pdf`, and
                    `libiMobileeDevice.dylib` into `clean_info.plist`,
                    `clean_manifest.plist`, and `clean_archive.zip`.
  - `inspect`:      Inspect a backup directory (`Manifest.mbdb` or `Manifest.db`).
  - `update`:       Pull the latest code from the tracked git branch (default: `main`)
                    and refresh the Python dependencies.
  - `gui`:          Launch the Linux Tkinter GUI (or interactive TUI if headless).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .crypto import (
    SWAP_PAIRS,
    deobfuscate_assets,
    derive_mdmpatcher_password,
)
from .device import (
    DeviceCommunicationError,
    check_find_my_enabled,
    detect_connected_device,
    restore_backup,
    run_ios27_g1_full_backup_patch,
)
from .ios27 import (
    ios27_direct_skip_setup,
    is_ios27_or_newer,
    patch_ios27_existing_backup,
)
from .manifest_db import read_manifest_db
from .mbdb import Mbdb
from .patcher import (
    DeviceIdentity,
    PatchConfig,
    PatchMode,
    build_backup,
    default_assets_dir,
)


def cmd_info(args: argparse.Namespace) -> int:
    try:
        dev = detect_connected_device(args.udid)
    except DeviceCommunicationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    fmi = check_find_my_enabled(dev.udid)
    ios27_flag = is_ios27_or_newer(dev.product_version, dev.build_version)
    print("=== Connected iOS Device ===")
    print(f"  Device Name   : {dev.device_name}")
    print(f"  Product Type  : {dev.product_type}")
    print(f"  iOS Version   : {dev.product_version} ({dev.build_version})")
    print(f"  Serial Number : {dev.serial_number}")
    print(f"  UDID          : {dev.udid}")
    print(f"  IMEI          : {dev.imei or '(none / Wi-Fi only)'}")
    print(
        f"  iOS 27 Engine : {'Active (G1 + MCInstall Hybrid)' if ios27_flag else 'Compatible (Enhanced + MCInstall)'}"
    )
    if fmi is not None:
        print(
            f"  Find My iPhone: {'ENABLED (Must be disabled before restore!)' if fmi else 'Disabled (Ready)'}"
        )
    return 0


def cmd_deobfuscate(args: argparse.Namespace) -> int:
    assets_dir = Path(args.assets_dir) if args.assets_dir else default_assets_dir()
    out_dir = Path(args.output)
    print("[*] Reverse-engineered MDMPatcher-Universal obfuscation details:")
    print(f"    Byte-swap pairs : {list(SWAP_PAIRS)}")
    print(
        f"    RNCryptor v3 PW : {derive_mdmpatcher_password().decode('utf-8')}"
    )
    print(f"    Assets Dir      : {assets_dir}")
    print(f"    Output Dir      : {out_dir}")
    results = deobfuscate_assets(assets_dir, out_dir)
    for name, path in results.items():
        print(f"[+] Wrote {name} ({path.stat().st_size:,} bytes) -> {path}")
    return 0


def cmd_build_backup(args: argparse.Namespace) -> int:
    if args.from_device:
        try:
            device = detect_connected_device(args.udid)
        except DeviceCommunicationError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 1
    else:
        device = DeviceIdentity(
            udid=args.udid or "00008030-001854E42E06402E",
            serial_number=args.serial or "F17F4MLSPLK2",
            product_type=args.product_type or "iPhone16,2",
            build_version=args.build_version or "22B83",
            product_version=args.ios_version or "18.1",
            imei=args.imei or "",
            device_name=args.device_name or "iPhone",
        )

    cfg = PatchConfig(
        mode=args.mode,
        block_ota=args.block_ota,
        is_supervised=args.supervised,
        organization_name=args.org_name or "",
        assets_dir=Path(args.assets_dir) if args.assets_dir else None,
    )

    out_root = Path(args.output)
    mdmb_dir = build_backup(out_root, device, cfg)
    print(f"[+] Built '{cfg.mode}' MDM-patched backup at: {mdmb_dir}")
    print(f"    Target UDID        : {device.udid}")
    print(f"    Product Type       : {device.product_type}")
    print(f"    iOS / Build        : {device.product_version} ({device.build_version})")
    print(f"    Serial Number      : {device.serial_number}")
    print(f"    IMEI               : {device.imei or '(none)'}")
    print("    Restore command    :")
    print(
        f"      idevicebackup2 -u {device.udid} restore --system --settings --skip-apps --no-reboot -s MDMB {out_root}"
    )
    return 0


def cmd_skip_setup(args: argparse.Namespace) -> int:
    print(
        "====================================================================="
    )
    print(
        f"  MDMPatcher-Linux v{__version__} — Direct USB Setup & CloudConfig Bypass"
    )
    print(
        "  (Vector A: com.apple.mobile.MCInstall + lockdownd — No Backup Wipe)"
    )
    print(
        "====================================================================="
    )
    ok = ios27_direct_skip_setup(
        args.udid,
        supervised=args.supervised,
        organization_name=args.org_name or "",
        set_lockdown_flags=True,
    )
    if ok:
        print(
            "\n[+] Successfully applied iOS 27 CloudConfiguration (80 SkipSetup panes) and lockdownd completion flags!"
        )
        return 0
    print(
        "\n[!] Direct MCInstall push could not complete. Ensure the device is connected via USB, unlocked, trusted, and `pymobiledevice3` is installed."
    )
    return 1


def cmd_patch_backup(args: argparse.Namespace) -> int:
    target = Path(args.backup_dir)
    if (target / "MDMB" / "Manifest.db").is_file():
        target = target / "MDMB"
    if not (target / "Manifest.db").is_file():
        print(
            f"[!] No Manifest.db found in {target}. `patch-backup` requires a Backup v3.3+ (Manifest.db) directory.",
            file=sys.stderr,
        )
        return 1

    report = patch_ios27_existing_backup(
        target,
        udid=args.udid,
        serial_number=args.serial,
        build_version=args.build_version or "24A335",
        product_version=args.ios_version or "27.0",
        is_supervised=args.supervised,
        organization_name=args.org_name or "",
        block_ota=args.block_ota,
    )
    print(f"[+] Patched iOS 27 backup directory: {report.backup_dir}")
    print(f"    Stripped MDM files    : {len(report.stripped_mdm_files)}")
    for item in report.stripped_mdm_files:
        print(f"      - {item}")
    print(f"    Injected payloads     : {report.injected_files}")
    print(f"    Unchanged payloads    : {report.unchanged_files}")
    print(f"    Synthesized dir rows  : {report.synthesized_dirs}")
    print(
        f"    6-Point Gate Verified : {'PASS' if report.verified else 'FAIL'}"
    )
    if not report.verified:
        for prob in report.verification_problems:
            print(f"      ! {prob}", file=sys.stderr)
        return 1
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    print("=================================================================")
    print(
        f"  MDMPatcher-Linux v{__version__} (iOS 9 - iOS 27+ Universal Edition)"
    )
    print("=================================================================")
    print("[*] Searching for USB-connected iOS/iPadOS device...")
    try:
        device = detect_connected_device(args.udid)
    except DeviceCommunicationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    is_27 = is_ios27_or_newer(device.product_version, device.build_version)
    selected_mode: PatchMode = args.mode
    if is_27 and selected_mode == "enhanced" and not args.keep_mode:
        print(
            "[*] Detected iOS 27+ firmware — automatically engaging iOS 27 Multi-Vector Engine (`ios27`)."
        )
        selected_mode = "ios27"

    print("[+] Device detected:")
    print(f"    Name   : {device.device_name}")
    print(f"    Model  : {device.product_type}")
    print(f"    iOS    : {device.product_version} ({device.build_version})")
    print(f"    Serial : {device.serial_number}")
    print(f"    UDID   : {device.udid}")
    print(f"    IMEI   : {device.imei or '(none)'}")
    print(f"    Mode   : {selected_mode}")

    fmi = check_find_my_enabled(device.udid)
    if fmi is True and not args.ignore_find_my:
        print(
            "\n[!] ERROR: Find My iPhone is ENABLED on this device.\n"
            "    iOS refuses MobileBackup2 system restores while Find My is active.\n"
            "    Please disable Find My iPhone first (or pass --ignore-find-my to force).",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "direct_only", False):
        return cmd_skip_setup(args)

    cfg = PatchConfig(
        mode=selected_mode,
        block_ota=args.block_ota,
        is_supervised=args.supervised,
        organization_name=args.org_name or "",
        assets_dir=Path(args.assets_dir) if args.assets_dir else None,
    )

    with tempfile.TemporaryDirectory(prefix="mdmpatcher_") as tmp_dir:
        work_root = Path(args.keep_backup) if args.keep_backup else Path(tmp_dir)
        work_root.mkdir(parents=True, exist_ok=True)

        if getattr(args, "full_backup_g1", False):
            rc = run_ios27_g1_full_backup_patch(
                device.udid,
                backup_work_dir=str(work_root),
                build_version=device.build_version,
                product_version=device.product_version,
                serial_number=device.serial_number,
                is_supervised=cfg.is_supervised,
                organization_name=cfg.organization_name,
                block_ota=cfg.block_ota,
                reboot=not args.no_reboot,
            )
        else:
            mdmb_dir = build_backup(work_root, device, cfg)
            print(f"[+] Prepared MDM-patched backup in: {mdmb_dir}")

            if args.dry_run:
                print("[*] Dry-run requested; skipping USB restore.")
                return 0

            if is_27 or selected_mode == "ios27":
                print(
                    "[*] Applying iOS 27 Pre-Restore Direct CloudConfiguration & lockdownd flags..."
                )
                ios27_direct_skip_setup(
                    device.udid,
                    supervised=cfg.is_supervised,
                    organization_name=cfg.organization_name,
                )

            print(
                "[*] Starting MobileBackup2 restore (keep device connected and unlocked)..."
            )
            rc = restore_backup(
                str(work_root),
                device.udid,
                source_udid="MDMB",
                system_files=True,
                settings=True,
                reboot=not args.no_reboot,
                skip_apps=True,
                ios27_mode=(is_27 or selected_mode == "ios27"),
            )

        if rc == 0:
            print("\n[+] SUCCESS: MDM patch applied!")
            print("    The device will now reboot.")
            print(
                "    NOTE for iOS 17.2 - iOS 27+: If the screen displays 'iPhone Partially Set Up',\n"
                "    tap 'Continue with Partial Setup' (do NOT tap 'Finish Setting Up') to enter the Home Screen."
            )
            return 0

        print(
            f"\n[!] Restore exited with code {rc}.\n"
            "    Troubleshooting:\n"
            "      - Ensure Find My iPhone is turned off.\n"
            "      - Ensure the device is on the Setup Assistant ('Hello') screen.\n"
            "      - On iOS 27+, try `./mdmpatcher.py skip-setup` (Vector A) or `./mdmpatcher.py patch --full-backup-g1` (Vector B).",
            file=sys.stderr,
        )
        return rc


def cmd_inspect(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if (target / "MDMB").is_dir():
        target = target / "MDMB"

    print(f"=== Inspecting Backup Directory: {target} ===")
    for plist_name in ("Info.plist", "Manifest.plist", "Status.plist"):
        p = target / plist_name
        if p.exists():
            data = plistlib.loads(p.read_bytes())
            print(f"\n[{plist_name}]")
            for k, v in data.items():
                if isinstance(v, (bytes, dict)) and k not in ("Lockdown",):
                    print(f"  {k}: <{type(v).__name__}>")
                else:
                    print(f"  {k}: {v}")

    mbdb_path = target / "Manifest.mbdb"
    if mbdb_path.exists():
        mbdb = Mbdb.from_bytes(mbdb_path.read_bytes())
        print(f"\n[Manifest.mbdb] Total records: {len(mbdb.records)}")
        for idx, r in enumerate(mbdb.records):
            if (
                "ConfigurationProfiles" in r.relative_path
                or "purplebuddy" in r.relative_path
                or "MobileAsset" in r.relative_path
                or r.domain == "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"
                or r.domain == "ManagedPreferencesDomain"
            ):
                kind = "DIR " if r.is_dir else ("LINK" if r.is_symlink else "FILE")
                print(
                    f"  #{idx:03d} [{kind}] {r.file_id} | {r.domain} :: {r.relative_path} (mode={oct(r.mode)}, len={r.filelen})"
                )
                fpath = target / r.file_id
                if r.is_file and fpath.exists():
                    try:
                        pl = plistlib.loads(fpath.read_bytes())
                        if isinstance(pl, dict):
                            print(f"       -> keys: {sorted(pl.keys())}")
                            for highlight in (
                                "IsSupervised",
                                "CloudConfigurationUIComplete",
                                "SetupDone",
                                "SetupFinishedAllSteps",
                                "LastMigratedBuild",
                            ):
                                if highlight in pl:
                                    print(
                                        f"          {highlight} = {pl[highlight]}"
                                    )
                    except Exception:
                        pass

    mdb_path = target / "Manifest.db"
    if mdb_path.exists():
        rows = read_manifest_db(mdb_path)
        print(f"\n[Manifest.db] Total SQLite rows: {len(rows)}")
        for idx, row in enumerate(rows):
            ino = row["meta"].get("InodeNumber", "?")
            print(
                f"  #{idx:03d} [flags={row['flags']}] {row['fileID']} | {row['domain']} :: {row['relativePath']} (inode={ino})"
            )

    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    """
    Launch a Linux GUI (Tkinter) if a display server is available, or fall back
    to an interactive terminal menu if running in a headless environment.
    """
    import os

    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        try:
            import tkinter as tk
            from tkinter import messagebox, scrolledtext, ttk

            root = tk.Tk()
            root.title(
                f"MDMPatcher-Linux v{__version__} (iOS 9 - 27+ Universal)"
            )
            root.geometry("720x520")

            header = ttk.Label(
                root,
                text="MDMPatcher-Linux (iOS 9 – 27+ Universal)",
                font=("Sans", 14, "bold"),
            )
            header.pack(pady=(12, 4))

            sub = ttk.Label(
                root,
                text="Reverse-Engineered Linux Port with iOS 27 G1 + MCInstall Support",
            )
            sub.pack(pady=(0, 10))

            info_frame = ttk.LabelFrame(root, text="Connected Device")
            info_frame.pack(fill="x", padx=14, pady=6)

            dev_var = tk.StringVar(value="No device queried yet. Click 'Refresh Device'.")
            dev_lbl = ttk.Label(info_frame, textvariable=dev_var, justify="left")
            dev_lbl.pack(anchor="w", padx=10, pady=8)

            opts_frame = ttk.LabelFrame(root, text="Patch Options")
            opts_frame.pack(fill="x", padx=14, pady=6)

            mode_var = tk.StringVar(value="enhanced")
            ttk.Label(opts_frame, text="Backup Mode:").grid(
                row=0, column=0, padx=8, pady=6, sticky="w"
            )
            mode_combo = ttk.Combobox(
                opts_frame,
                textvariable=mode_var,
                values=["enhanced", "ios27", "modern", "sqlite", "legacy"],
                state="readonly",
                width=18,
            )
            mode_combo.grid(row=0, column=1, padx=8, pady=6, sticky="w")

            ota_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                opts_frame,
                text="Block OTA Software Updates",
                variable=ota_var,
            ).grid(row=0, column=2, padx=12, pady=6, sticky="w")

            log_box = scrolledtext.ScrolledText(root, height=12, state="normal")
            log_box.pack(fill="both", expand=True, padx=14, pady=6)

            def append_log(msg: str) -> None:
                log_box.insert("end", msg + "\n")
                log_box.see("end")
                root.update_idletasks()

            def on_refresh() -> None:
                try:
                    d = detect_connected_device()
                    is27 = is_ios27_or_newer(d.product_version, d.build_version)
                    if is27:
                        mode_var.set("ios27")
                    dev_var.set(
                        f"{d.device_name} ({d.product_type}) — iOS {d.product_version} ({d.build_version})\n"
                        f"Serial: {d.serial_number}  |  UDID: {d.udid}"
                    )
                    append_log(f"[+] Detected {d.product_type} on iOS {d.product_version} ({d.build_version})")
                except Exception as exc:
                    dev_var.set("No device detected.")
                    append_log(f"[!] {exc}")

            def on_patch() -> None:
                try:
                    d = detect_connected_device()
                except Exception as exc:
                    messagebox.showerror("Device Error", str(exc))
                    return
                append_log(f"[*] Building '{mode_var.get()}' MDM-patched backup...")
                cfg = PatchConfig(
                    mode=mode_var.get(),  # type: ignore[arg-type]
                    block_ota=ota_var.get(),
                )
                with tempfile.TemporaryDirectory(prefix="mdmpatcher_") as tmp:
                    build_backup(tmp, d, cfg)
                    append_log("[*] Starting MobileBackup2 restore...")
                    rc = restore_backup(
                        tmp,
                        d.udid,
                        ios27_mode=(
                            mode_var.get() == "ios27"
                            or is_ios27_or_newer(
                                d.product_version, d.build_version
                            )
                        ),
                        on_output=append_log,
                    )
                    if rc == 0:
                        append_log("[+] MDM Patch completed! Device is rebooting.")
                        messagebox.showinfo(
                            "Success",
                            "MDM Patch applied!\n\nOn iOS 17.2 - 27+: If prompted with 'iPhone Partially Set Up', tap 'Continue with Partial Setup'.",
                        )
                    else:
                        append_log(f"[!] Restore failed with code {rc}")

            def on_direct_skip() -> None:
                append_log("[*] Running Direct USB MCInstall + lockdownd SkipSetup...")
                ok = ios27_direct_skip_setup(on_output=append_log)
                if ok:
                    messagebox.showinfo(
                        "Success",
                        "Direct iOS 27 CloudConfiguration & Setup Assistant skip applied!",
                    )

            btn_frame = ttk.Frame(root)
            btn_frame.pack(fill="x", padx=14, pady=8)
            ttk.Button(
                btn_frame, text="Refresh Device", command=on_refresh
            ).pack(side="left", padx=6)
            ttk.Button(
                btn_frame,
                text="Direct Skip Setup (iOS 27 Safe)",
                command=on_direct_skip,
            ).pack(side="left", padx=6)
            ttk.Button(
                btn_frame, text="Patch MDM Now", command=on_patch
            ).pack(side="right", padx=6)

            root.mainloop()
            return 0
        except Exception as exc:
            print(f"[*] Tkinter GUI unavailable ({exc}), falling back to interactive TUI...")

    print("=================================================================")
    print(f"  MDMPatcher-Linux v{__version__} — Interactive Terminal Mode")
    print("=================================================================")
    print("  1) Detect connected iOS device (`info`)")
    print("  2) Patch connected iOS device (`enhanced` / auto-`ios27` mode)")
    print("  3) Direct USB Setup & CloudConfig Bypass (`skip-setup`, iOS 27 safe)")
    print("  4) Patch connected iOS 27 device (`full-backup-g1` protective route)")
    print("  5) Deobfuscate MDMPatcher-Universal assets (`deobfuscate`)")
    print("  6) Update MDMPatcher-Linux from git (`update`)")
    print("  0) Exit")
    try:
        choice = input("\nSelect an option [0-6]: ").strip()
    except EOFError:
        return 0

    if choice == "1":
        return cmd_info(argparse.Namespace(udid=None))
    if choice in ("2", "4"):
        return cmd_patch(
            argparse.Namespace(
                udid=None,
                mode="enhanced",
                keep_mode=False,
                block_ota=False,
                supervised=False,
                org_name="",
                assets_dir=None,
                keep_backup=None,
                dry_run=False,
                no_reboot=False,
                ignore_find_my=False,
                direct_only=False,
                full_backup_g1=(choice == "4"),
            )
        )
    if choice == "3":
        return cmd_skip_setup(
            argparse.Namespace(udid=None, supervised=False, org_name="")
        )
    if choice == "5":
        return cmd_deobfuscate(
            argparse.Namespace(
                assets_dir=None,
                output="assets/deobfuscated",
            )
        )
    if choice == "6":
        return cmd_update(argparse.Namespace(branch="", no_deps=False))
    return 0


UPDATE_HELP_URL = (
    "https://raw.githubusercontent.com/supermario3D1/mdmpatcher-linux/main/scripts/install.sh"
)


def _git(repo_root: Path) -> str | None:
    """Return the path to `git` if it is available."""
    return shutil.which("git")


def _git_out(repo_root: Path, *args: str) -> str:
    git = _git(repo_root)
    if git is None:
        return ""
    proc = subprocess.run(
        [git, "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def cmd_update(args: argparse.Namespace) -> int:
    """
    Self-update: fast-forward the installation to the latest code on the
    tracked branch (default: `main`) and refresh Python dependencies.
    """
    repo_root = Path(__file__).resolve().parent.parent
    print(f"[*] MDMPatcher-Linux v{__version__} — self-update")
    print(f"    Install directory: {repo_root}")

    git = _git(repo_root)
    if git is None:
        print("[!] `git` was not found, so an in-place update is not possible.", file=sys.stderr)
        print("    Re-run the one-line installer to re-download the tool:", file=sys.stderr)
        print(f"      curl -fsSL {UPDATE_HELP_URL} | bash", file=sys.stderr)
        return 1

    if not (repo_root / ".git").exists():
        print(
            f"[!] {repo_root} is not a git checkout (it was installed from a tarball).",
            file=sys.stderr,
        )
        print("    Re-run the installer to replace it with a git clone:", file=sys.stderr)
        print(f"      curl -fsSL {UPDATE_HELP_URL} | bash", file=sys.stderr)
        return 1

    branch = args.branch or _git_out(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "main"
    before = _git_out(repo_root, "rev-parse", "--short", "HEAD")

    print(f"[*] Fetching origin/{branch} ...")
    if subprocess.run(
        [git, "-C", str(repo_root), "fetch", "--quiet", "origin", branch], check=False
    ).returncode != 0:
        print(
            "[!] Could not reach the remote repository (offline?). Nothing was changed.",
            file=sys.stderr,
        )
        return 1

    if subprocess.run(
        [git, "-C", str(repo_root), "pull", "--ff-only", "origin", branch], check=False
    ).returncode != 0:
        print(
            f"[!] Fast-forward update failed — {repo_root} has local modifications.",
            file=sys.stderr,
        )
        print(
            "    Review them with:  git -C "
            f"{repo_root} status   (or re-install into a clean directory)",
            file=sys.stderr,
        )
        return 1

    after = _git_out(repo_root, "rev-parse", "--short", "HEAD")
    if after and after == before:
        print(f"[=] Already up to date with origin/{branch} ({after or 'unknown'}).")
    else:
        print(f"[+] Updated {before or '?'} -> {after or '?'} (origin/{branch}).")

    if args.no_deps:
        return 0

    venv_python = repo_root / ".venv" / "bin" / "python3"
    python = str(venv_python) if venv_python.exists() else sys.executable
    print(f"[*] Refreshing Python dependencies with {python} ...")
    install = [
        python,
        "-m",
        "pip",
        "install",
        "-e",
        f"{repo_root}[usb]",
    ]
    if subprocess.run(install, check=False).returncode != 0:
        install.insert(2, "--break-system-packages")
        if subprocess.run(install, check=False).returncode != 0:
            print(
                "[!] Python dependencies could not be refreshed (offline?). The update still applied.",
                file=sys.stderr,
            )
            return 0
    print("[+] Python dependencies up to date.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdmpatcher",
        description=(
            "MDMPatcher-Linux: Port of MDMPatcher-Universal & MDMPatcher-Enhanced "
            "for Linux (Fedora, Ubuntu, Debian, Arch) with iOS 9 - iOS 27+ support."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"mdmpatcher-linux {__version__}"
    )
    sub = parser.add_subparsers(dest="subcommand")

    # info
    p_info = sub.add_parser(
        "info", help="Detect connected iOS device and print device info"
    )
    p_info.add_argument("-u", "--udid", help="Target device UDID")
    p_info.set_defaults(func=cmd_info)

    # patch
    p_patch = sub.add_parser(
        "patch", help="Patch MDM on a connected iOS device over USB"
    )
    p_patch.add_argument("-u", "--udid", help="Target device UDID")
    p_patch.add_argument(
        "-m",
        "--mode",
        choices=["enhanced", "ios27", "modern", "sqlite", "legacy"],
        default="enhanced",
        help="Backup generation mode (default: enhanced; auto-switches to ios27 on iOS 27+)",
    )
    p_patch.add_argument(
        "--keep-mode",
        action="store_true",
        help="Do not auto-switch from enhanced to ios27 mode when iOS 27+ is detected",
    )
    p_patch.add_argument(
        "--direct-only",
        action="store_true",
        help="Run only Vector A (direct USB MobileConfigService + lockdownd push) without backup restore",
    )
    p_patch.add_argument(
        "--full-backup-g1",
        action="store_true",
        help="Run Vector B (iOS 27 G1 5-phase full device backup -> MDM strip & donor-cloned injection -> verified restore)",
    )
    p_patch.add_argument(
        "--block-ota",
        action="store_true",
        help="Also inject OTA update blocker profile into RootDomain",
    )
    p_patch.add_argument(
        "--supervised",
        action="store_true",
        help="Mark CloudConfigurationDetails IsSupervised=True (default: False)",
    )
    p_patch.add_argument(
        "--org-name",
        default="",
        help="Optional OrganizationName string in CloudConfigurationDetails.plist",
    )
    p_patch.add_argument(
        "--assets-dir",
        help="Custom path to assets directory",
    )
    p_patch.add_argument(
        "--keep-backup",
        help="Save generated backup in this directory instead of a temporary folder",
    )
    p_patch.add_argument(
        "--dry-run",
        action="store_true",
        help="Build backup for the connected device without executing restore",
    )
    p_patch.add_argument(
        "--no-reboot",
        action="store_true",
        help="Pass --no-reboot to idevicebackup2",
    )
    p_patch.add_argument(
        "--ignore-find-my",
        action="store_true",
        help="Proceed even if Find My iPhone appears enabled",
    )
    p_patch.set_defaults(func=cmd_patch)

    # skip-setup (Vector A)
    p_skip = sub.add_parser(
        "skip-setup",
        help="Direct USB MobileConfigService + lockdownd Setup Assistant & CloudConfig bypass (iOS 27 safe, no restore)",
    )
    p_skip.add_argument("-u", "--udid", help="Target device UDID")
    p_skip.add_argument(
        "--supervised",
        action="store_true",
        help="Mark device as supervised with removable profile",
    )
    p_skip.add_argument(
        "--org-name",
        default="",
        help="Optional OrganizationName string",
    )
    p_skip.set_defaults(func=cmd_skip_setup)

    # patch-backup (Vector B offline/existing backup modifier)
    p_pb = sub.add_parser(
        "patch-backup",
        help="Strip MDM.plist/MDMEvents.plist and inject iOS 27 donor-cloned payloads into an existing Manifest.db backup",
    )
    p_pb.add_argument(
        "backup_dir",
        help="Path to existing iOS backup directory containing Manifest.db",
    )
    p_pb.add_argument("-u", "--udid", help="Override UniqueDeviceID in Manifest.plist")
    p_pb.add_argument("-s", "--serial", help="Override SerialNumber in Manifest.plist")
    p_pb.add_argument(
        "-b",
        "--build-version",
        default="24A335",
        help="Target BuildVersion (default: 24A335)",
    )
    p_pb.add_argument(
        "-i",
        "--ios-version",
        default="27.0",
        help="Target ProductVersion (default: 27.0)",
    )
    p_pb.add_argument(
        "--block-ota",
        action="store_true",
        help="Also inject OTA update disabler profile",
    )
    p_pb.add_argument(
        "--supervised",
        action="store_true",
        help="Set IsSupervised=True in CloudConfigurationDetails.plist",
    )
    p_pb.add_argument("--org-name", default="", help="Optional OrganizationName")
    p_pb.set_defaults(func=cmd_patch_backup)

    # build-backup
    p_build = sub.add_parser(
        "build-backup",
        help="Generate a customized MDM-patched backup directory (offline or from connected device)",
    )
    p_build.add_argument(
        "-o",
        "--output",
        default="./output_backup",
        help="Output root directory (will create <output>/MDMB)",
    )
    p_build.add_argument(
        "-m",
        "--mode",
        choices=["enhanced", "ios27", "modern", "sqlite", "legacy"],
        default="enhanced",
        help="Backup generation mode (default: enhanced)",
    )
    p_build.add_argument(
        "--from-device",
        action="store_true",
        help="Read device identity from a connected USB device",
    )
    p_build.add_argument("-u", "--udid", help="Device UDID")
    p_build.add_argument("-s", "--serial", help="Device SerialNumber")
    p_build.add_argument(
        "-t", "--product-type", help="Device ProductType (e.g. iPhone16,2)"
    )
    p_build.add_argument(
        "-b", "--build-version", help="Device BuildVersion (e.g. 24A335)"
    )
    p_build.add_argument(
        "-i", "--ios-version", help="Device ProductVersion (e.g. 27.0)"
    )
    p_build.add_argument("--imei", help="Device IMEI (omit for Wi-Fi iPads)")
    p_build.add_argument(
        "-n", "--device-name", default="iPhone", help="Device display name"
    )
    p_build.add_argument(
        "--block-ota",
        action="store_true",
        help="Also inject OTA update blocker profile into RootDomain",
    )
    p_build.add_argument(
        "--supervised",
        action="store_true",
        help="Mark CloudConfigurationDetails IsSupervised=True",
    )
    p_build.add_argument(
        "--org-name",
        default="",
        help="Optional OrganizationName string",
    )
    p_build.add_argument(
        "--assets-dir",
        help="Custom path to assets directory",
    )
    p_build.set_defaults(func=cmd_build_backup)

    # deobfuscate
    p_deob = sub.add_parser(
        "deobfuscate",
        help="Decrypt and deobfuscate extension1.pdf, extension2.pdf, and libiMobileeDevice.dylib",
    )
    p_deob.add_argument(
        "-a",
        "--assets-dir",
        help="Directory containing obfuscated files (default: ./assets)",
    )
    p_deob.add_argument(
        "-o",
        "--output",
        default="./assets/deobfuscated",
        help="Output directory for clean files",
    )
    p_deob.set_defaults(func=cmd_deobfuscate)

    # inspect
    p_insp = sub.add_parser(
        "inspect",
        help="Inspect an iOS backup directory (Manifest.mbdb, Manifest.db, plists)",
    )
    p_insp.add_argument("path", help="Path to backup directory")
    p_insp.set_defaults(func=cmd_inspect)

    # update (self-update from the tracked git branch, default: main)
    p_upd = sub.add_parser(
        "update",
        help="Update this installation from the git remote (default branch: main)",
    )
    p_upd.add_argument(
        "-b",
        "--branch",
        default="",
        help="Branch to pull from (default: the branch currently checked out)",
    )
    p_upd.add_argument(
        "--no-deps",
        action="store_true",
        help="Only pull the source code; do not refresh Python dependencies",
    )
    p_upd.set_defaults(func=cmd_update)

    # gui
    p_gui = sub.add_parser(
        "gui",
        help="Launch Linux GUI or interactive TUI",
    )
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        return cmd_gui(args)
    return int(args.func(args))
