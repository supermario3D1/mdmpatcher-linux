"""
Linux iOS USB Device Discovery, Backup, and Restore Engine (`libimobiledevice` + `pymobiledevice3`).

Replaces the macOS-only `IOKit` USB watcher (`USBDetection.swift`) and embedded
`libimobiledevice` dylibs from `MDMPatcher-Universal` with a multi-backend Linux
driver:
  1. Primary: `idevice_id`, `ideviceinfo`, `idevicebackup2`, `idevicediagnostics`
     from Linux `libimobiledevice-utils` / `libimobiledevice` (Ubuntu, Debian,
     Fedora, Arch, openSUSE).
  2. Fallback / Pure-Python: `pymobiledevice3` (`create_using_usbmux`,
     `Mobilebackup2Service`, `MobileConfigService`, `DiagnosticsService`).

Why the original `MDMPatcher-Universal` C restore call failed on modern iOS:
  - In `MDMPatcher-Universal/MDMPatcher/idevicebackup2.c`, `mainLOL()` set ONLY:
      `cmd_flags |= CMD_FLAG_RESTORE_SETTINGS;`
    which sends `RestoreSystemFiles: false` in the `MobileBackup2` options plist.
  - Without `RestoreSystemFiles: true` (`--system`) and `RestoreDontCopyBackup: true`
    (`--skip-apps`), iOS 15 - 27+ ignores `SysSharedContainerDomain-*` and
    `ManagedPreferencesDomain`.
  - On iOS 27 (builds 24A+), we additionally:
      1. Bump the Lockdown SSL handshake timeout to 60s.
      2. Support `--no-reboot` controlled restores so the device never reboots
         on a failed restore.
      3. Support `_Mobilebackup2NoEscrow` when a freshly wiped iOS 27 device's
         pair record lacks `EscrowBag`.
      4. Push `SetCloudConfiguration` (80 `SkipSetup` panes) directly via
         `com.apple.mobile.MCInstall` (`MobileConfigService`) + `lockdownd`
         completion flags.
"""

from __future__ import annotations

import asyncio
import plistlib
import shutil
import subprocess
from typing import Any, Callable

from .ios27 import (
    G1Watchdog,
    ios27_direct_skip_setup,
    is_ios27_or_newer,
    patch_ios27_existing_backup,
)
from .patcher import DeviceIdentity


class DeviceCommunicationError(RuntimeError):
    """Raised when no iOS device is found or USB communication fails."""


def has_idevice_cli() -> bool:
    return (
        shutil.which("ideviceinfo") is not None
        and shutil.which("idevicebackup2") is not None
    )


def has_pymobiledevice3() -> bool:
    try:
        import pymobiledevice3  # noqa: F401

        return True
    except ImportError:
        return False


def detect_connected_device(udid: str | None = None) -> DeviceIdentity:
    """
    Query a USB-connected iPhone or iPad on Linux via `ideviceinfo` or
    `pymobiledevice3` and return its `DeviceIdentity`.
    """
    if shutil.which("ideviceinfo") is not None:
        cmd = ["ideviceinfo", "-x"]
        if udid:
            cmd.extend(["-u", udid])
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode == 0 and len(proc.stdout) > 32:
            try:
                info: dict[str, Any] = plistlib.loads(proc.stdout)
                return DeviceIdentity(
                    udid=str(info.get("UniqueDeviceID", udid or "")),
                    serial_number=str(info.get("SerialNumber", "")),
                    product_type=str(info.get("ProductType", "")),
                    build_version=str(info.get("BuildVersion", "")),
                    product_version=str(info.get("ProductVersion", "18.0")),
                    imei=str(
                        info.get("InternationalMobileEquipmentIdentity", "")
                        or ""
                    ),
                    device_name=str(info.get("DeviceName", "iPhone")),
                )
            except Exception as exc:
                raise DeviceCommunicationError(
                    f"Failed to parse ideviceinfo XML plist: {exc}"
                ) from exc

    if has_pymobiledevice3():
        try:
            from pymobiledevice3.lockdown import create_using_usbmux

            async def _get_vals() -> dict[str, Any]:
                lockdown = create_using_usbmux(serial=udid)
                if asyncio.iscoroutine(lockdown):
                    lockdown = await lockdown
                vals = getattr(lockdown, "all_values", None)
                if vals is None:
                    gv = lockdown.get_value()
                    vals = await gv if asyncio.iscoroutine(gv) else gv
                return dict(vals or {})

            info = asyncio.run(_get_vals())
            return DeviceIdentity(
                udid=str(info.get("UniqueDeviceID", udid or "")),
                serial_number=str(info.get("SerialNumber", "")),
                product_type=str(info.get("ProductType", "")),
                build_version=str(info.get("BuildVersion", "")),
                product_version=str(info.get("ProductVersion", "18.0")),
                imei=str(
                    info.get("InternationalMobileEquipmentIdentity", "") or ""
                ),
                device_name=str(info.get("DeviceName", "iPhone")),
            )
        except Exception as exc:
            raise DeviceCommunicationError(
                f"Could not connect to iOS device via pymobiledevice3: {exc}"
            ) from exc

    raise DeviceCommunicationError(
        "Could not detect a connected iOS device.\n"
        "Please verify that:\n"
        "  1. Your iPhone/iPad is connected via USB, unlocked, and 'Trust This Computer' was accepted.\n"
        "  2. `usbmuxd` and `libimobiledevice-utils` (or `libimobiledevice` on Fedora) are installed (`./scripts/install.sh`)."
    )


def check_find_my_enabled(udid: str | None = None) -> bool | None:
    """
    Check if Find My iPhone (`fmip`) is enabled on the connected device.
    MobileBackup2 restores require Find My to be disabled.
    """
    if shutil.which("ideviceinfo") is not None:
        cmd = ["ideviceinfo", "-q", "com.apple.fmip", "-x"]
        if udid:
            cmd.extend(["-u", udid])
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            try:
                data = plistlib.loads(proc.stdout)
                if isinstance(data, dict) and "IsAssociated" in data:
                    return bool(data["IsAssociated"])
            except Exception:
                pass
    return None


def restart_device(
    udid: str | None = None,
    *,
    on_output: Callable[[str], None] | None = None,
) -> bool:
    """Perform a controlled restart of the connected iOS device."""
    def _emit(msg: str) -> None:
        if on_output:
            on_output(msg)
        else:
            print(msg)

    for tool_cmd in (
        ["idevicediagnostics", "restart"],
        ["idevicerestart"],
    ):
        if shutil.which(tool_cmd[0]) is not None:
            cmd = list(tool_cmd)
            if udid:
                cmd.extend(["-u", udid])
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode == 0:
                _emit("[+] Sent controlled device restart command.")
                return True

    if has_pymobiledevice3():
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.diagnostics import DiagnosticsService

            async def _async_restart() -> None:
                ld = create_using_usbmux(serial=udid)
                if asyncio.iscoroutine(ld):
                    ld = await ld
                diag_ctx = DiagnosticsService(ld)
                if hasattr(diag_ctx, "__aenter__"):
                    async with diag_ctx as diag:
                        await diag.restart()
                else:
                    with diag_ctx as diag:
                        res = diag.restart()
                        if asyncio.iscoroutine(res):
                            await res

            asyncio.run(_async_restart())
            _emit("[+] Sent controlled device restart command via DiagnosticsService.")
            return True
        except Exception:
            pass
    return False


def create_device_backup(
    backup_root: str,
    udid: str,
    *,
    full: bool = True,
    on_output: Callable[[str], None] | None = None,
) -> str:
    """
    Create a real unencrypted device backup into `backup_root/<udid>` over USB
    (used by the iOS 27 G1 Full-Backup -> Donor-Cloned Injection -> Restore route).
    """
    def _emit(msg: str) -> None:
        if on_output:
            on_output(msg)
        else:
            print(msg)

    if shutil.which("idevicebackup2") is not None:
        cmd = ["idevicebackup2", "-u", udid, "backup"]
        if full:
            cmd.append("--full")
        cmd.append(str(backup_root))
        _emit(f"[*] Taking device backup via: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _emit(line.rstrip())
        rc = proc.wait()
        if rc != 0:
            raise DeviceCommunicationError(
                f"idevicebackup2 backup failed with exit code {rc}."
            )
        return str(backup_root)

    if has_pymobiledevice3():
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
        import pymobiledevice3.service_connection as _sc

        _sc.DEFAULT_SSL_HANDSHAKE_TIMEOUT = 60

        async def _async_backup() -> None:
            ld = create_using_usbmux(serial=udid)
            if asyncio.iscoroutine(ld):
                ld = await ld
            mb_ctx = Mobilebackup2Service(ld)
            if hasattr(mb_ctx, "__aenter__"):
                async with mb_ctx as mb:
                    await mb.backup(
                        full=full,
                        backup_directory=str(backup_root),
                    )
            else:
                with mb_ctx as mb:
                    res = mb.backup(
                        full=full,
                        backup_directory=str(backup_root),
                    )
                    if asyncio.iscoroutine(res):
                        await res

        asyncio.run(_async_backup())
        return str(backup_root)

    raise DeviceCommunicationError(
        "Neither `idevicebackup2` nor `pymobiledevice3` is installed."
    )


def restore_backup(
    backup_root: str,
    udid: str,
    *,
    source_udid: str = "MDMB",
    system_files: bool = True,
    settings: bool = True,
    reboot: bool = True,
    remove_items: bool = False,
    skip_apps: bool = True,
    ios27_mode: bool = False,
    on_output: Callable[[str], None] | None = None,
) -> int:
    """
    Restore the generated backup at `backup_root/<source_udid>` onto the
    connected iOS device with `udid`.

    When `ios27_mode=True`:
      1. Runs the restore with `--no-reboot` first (`reboot=False`) under a
         stall watchdog so the device never reboots on an incomplete restore.
      2. Pushes `SetCloudConfiguration` (80 `SkipSetup` panes) + `lockdownd`
         completion flags via `ios27_direct_skip_setup`.
      3. Triggers a controlled restart if `reboot=True`.
    """
    def _emit(msg: str) -> None:
        if on_output:
            on_output(msg)
        else:
            print(msg)

    effective_reboot = False if ios27_mode else reboot
    watchdog = G1Watchdog()

    if shutil.which("idevicebackup2") is not None:
        help_out = subprocess.run(
            ["idevicebackup2", "--help"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout

        cmd = ["idevicebackup2", "-u", udid, "restore"]
        if system_files:
            cmd.append("--system")
        if settings:
            cmd.append("--settings")
        if not effective_reboot:
            cmd.append("--no-reboot")
        if remove_items:
            cmd.append("--remove")
        if skip_apps and "--skip-apps" in help_out:
            cmd.append("--skip-apps")
        cmd.extend(["-s", source_udid, str(backup_root)])

        _emit(f"[*] Running: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            watchdog.note_progress()
            _emit(line.rstrip())
        rc = proc.wait()
        if rc == 0 and ios27_mode:
            _emit(
                "[*] Running iOS 27 Phase-5 MobileConfigService + lockdownd sweep..."
            )
            ios27_direct_skip_setup(udid, on_output=_emit)
            if reboot:
                restart_device(udid, on_output=_emit)
        return rc

    if has_pymobiledevice3():
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.lockdown_service import (
                LockdownService,
            )
            from pymobiledevice3.services.mobilebackup2 import (
                Mobilebackup2Service,
            )
            import pymobiledevice3.service_connection as _sc

            _sc.DEFAULT_SSL_HANDSHAKE_TIMEOUT = 60

            class _Mobilebackup2NoEscrow(Mobilebackup2Service):
                def __init__(self, lockdown: Any) -> None:
                    LockdownService.__init__(
                        self,
                        lockdown,
                        self.SERVICE_NAME,
                        include_escrow_bag=False,
                    )

            def _pick_mb_service(lc: Any) -> Mobilebackup2Service:
                pair_record = getattr(lc, "pair_record", None)
                if (
                    isinstance(pair_record, dict)
                    and "EscrowBag" not in pair_record
                ):
                    _emit(
                        "[*] Pair record has no EscrowBag (fresh post-wipe state); starting mobilebackup2 without EscrowBag."
                    )
                    return _Mobilebackup2NoEscrow(lc)
                return Mobilebackup2Service(lc)

            _emit(
                f"[*] Restoring backup via pymobiledevice3 (source={source_udid}, system={system_files}, skip_apps={skip_apps}, ios27_mode={ios27_mode})..."
            )

            async def _async_restore() -> None:
                lockdown = create_using_usbmux(serial=udid)
                if asyncio.iscoroutine(lockdown):
                    lockdown = await lockdown
                mb_ctx = _pick_mb_service(lockdown)
                if hasattr(mb_ctx, "__aenter__"):
                    async with mb_ctx as mb:
                        await mb.restore(
                            backup_directory=str(backup_root),
                            system=system_files,
                            reboot=effective_reboot,
                            copy=False,
                            settings=settings,
                            remove=remove_items,
                            source=source_udid,
                            skip_apps=skip_apps,
                        )
                else:
                    with mb_ctx as mb:
                        res = mb.restore(
                            backup_directory=str(backup_root),
                            system=system_files,
                            reboot=effective_reboot,
                            copy=False,
                            settings=settings,
                            remove=remove_items,
                            source=source_udid,
                            skip_apps=skip_apps,
                        )
                        if asyncio.iscoroutine(res):
                            await res

            asyncio.run(_async_restore())
            if ios27_mode:
                _emit(
                    "[*] Running iOS 27 Phase-5 MobileConfigService + lockdownd sweep..."
                )
                ios27_direct_skip_setup(udid, on_output=_emit)
                if reboot:
                    restart_device(udid, on_output=_emit)
            return 0
        except Exception as exc:
            _emit(f"[!] pymobiledevice3 restore failed: {exc}")
            return 1

    raise DeviceCommunicationError(
        "Neither `idevicebackup2` nor `pymobiledevice3` is installed to perform the restore."
    )


def run_ios27_g1_full_backup_patch(
    udid: str,
    *,
    backup_work_dir: str,
    build_version: str = "24A335",
    product_version: str = "27.0",
    serial_number: str = "",
    is_supervised: bool = False,
    organization_name: str = "",
    block_ota: bool = False,
    reboot: bool = True,
    on_output: Callable[[str], None] | None = None,
) -> int:
    """
    Execute the full iOS 27 G1 Pipeline on a connected device:
      Phase 1: Take a real unencrypted backup of the connected device (`create_device_backup`).
      Phase 2: Strip `MDM.plist` & `MDMEvents.plist` and inject donor-cloned iOS 27
               MDM-bypass & Setup Assistant payloads with unique monotonic inodes.
      Phase 3: Run the 6-point pre-restore verification gate (`verify_ios27_backup`).
      Phase 4: Restore the verified backup with `--no-reboot` (`ios27_mode=True`).
      Phase 5: Push `SetCloudConfiguration` via `MobileConfigService` and restart.
    """
    def _emit(msg: str) -> None:
        if on_output:
            on_output(msg)
        else:
            print(msg)

    _emit(
        "[*] iOS 27 G1 Pipeline Phase 1/5: Taking protective device backup..."
    )
    create_device_backup(backup_work_dir, udid, full=True, on_output=_emit)

    _emit(
        "[*] iOS 27 G1 Pipeline Phase 2/5: Stripping MDM profiles & injecting donor-cloned payloads..."
    )
    report = patch_ios27_existing_backup(
        backup_work_dir,
        udid=udid,
        serial_number=serial_number or None,
        build_version=build_version,
        product_version=product_version,
        is_supervised=is_supervised,
        organization_name=organization_name,
        block_ota=block_ota,
    )
    _emit(
        f"[+] Stripped {len(report.stripped_mdm_files)} MDM file(s), "
        f"injected {report.injected_files} payload(s), "
        f"synthesized {report.synthesized_dirs} directory row(s)."
    )

    _emit(
        "[*] iOS 27 G1 Pipeline Phase 3/5: Running 6-point pre-restore verification gate..."
    )
    if not report.verified:
        _emit(
            "[!] ABORTING BEFORE RESTORE — Pre-restore verification gate failed:"
        )
        for prob in report.verification_problems:
            _emit(f"    - {prob}")
        return 1
    _emit("[+] Pre-restore verification gate passed (0 problems).")

    _emit(
        "[*] iOS 27 G1 Pipeline Phase 4/5: Restoring verified backup with controlled reboot guard..."
    )
    return restore_backup(
        backup_work_dir,
        udid,
        source_udid=udid,
        system_files=True,
        settings=True,
        reboot=reboot,
        skip_apps=True,
        ios27_mode=True,
        on_output=_emit,
    )
