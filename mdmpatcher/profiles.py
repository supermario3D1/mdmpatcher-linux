"""
Modern iOS (iOS 15, 16, 17, 18, and 26.x) MDM & Setup Assistant bypass profile generators.

Verified support window: iOS 9 - iOS 26.x. iOS 27.0+ (builds 24A+) is experimental
and every command warns before touching such a device.

Why the original MDMPatcher-Universal / MDMPatcher-Enhanced fails on modern iOS:
  1. Legacy iOS (< 13) only checked `HomeDomain/Library/ConfigurationProfiles/*`
     and `HomeDomain/Library/Preferences/com.apple.purplebuddy.plist`.
  2. Modern iOS (iOS 15 - 26.x) stores DEP / Automated Device Enrollment (ADE)
     and Cloud Configuration state in the shared system container:
       Domain: `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`
       Path:   `Library/ConfigurationProfiles/CloudConfigurationDetails.plist`
       Path:   `Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist`
     and checks managed Setup Assistant state in:
       Domain: `ManagedPreferencesDomain`
       Path:   `mobile/com.apple.purplebuddy.plist`
  3. On iOS 26.2+ (and the unverified iOS 27 builds 24A+), Apple made two
     further changes (Device Management backup restoration policy):
     a) `backupd` / `BackupAgent2` no longer restores stale MDM configuration from
        synthetic partial backups and triggers "safe state recovery" if a partial
        restore lacks valid `Manifest.db` `MBFile` extended attributes or unique inodes.
     b) Setup Assistant added new iOS 26 panes (`OSShowcase`, `SafetyAndHandling`,
        `AgeBasedSafetySettings`, `EnableLockdownMode`, `AdditionalPrivacySettings`,
        `WebContentFiltering`, `CameraButton`, `Intelligence`, etc.) and supports
        direct Cloud Configuration injection over `com.apple.mobile.MCInstall`
        (`MobileConfigService.set_cloud_configuration`) as well as the G1
        full-backup donor-cloned `Manifest.db` pipeline.
"""

from __future__ import annotations

import plistlib
from typing import Any
import uuid

SYS_CONFIG_PROFILE_DOMAIN = (
    "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"
)
MANAGED_PREFS_DOMAIN = "ManagedPreferencesDomain"
HOME_DOMAIN = "HomeDomain"
ROOT_DOMAIN = "RootDomain"
SYSTEM_PREFS_DOMAIN = "SystemPreferencesDomain"

# Canonical 80-pane SkipSetup list covering iOS 9 through iOS 26.x
# (iOS 27.0+ / builds 24A+ is best-effort and unverified).
# Unrecognized pane identifiers on older iOS versions are safely ignored as no-ops.
SKIP_SETUP_KEYS: list[str] = [
    "Location",
    "Restore",
    "SIMSetup",
    "Android",
    "AppleID",
    "IntendedUser",
    "TOS",
    "Siri",
    "ScreenTime",
    "Diagnostics",
    "SoftwareUpdate",
    "Passcode",
    "Biometric",
    "Payment",
    "Zoom",
    "DisplayTone",
    "MessagingActivationUsingPhoneNumber",
    "HomeButtonSensitivity",
    "CloudStorage",
    "ScreenSaver",
    "TapToSetup",
    "Keyboard",
    "PreferredLanguage",
    "SpokenLanguage",
    "WatchMigration",
    "OnBoarding",
    "TVProviderSignIn",
    "TVHomeScreenSync",
    "Privacy",
    "TVRoom",
    "iMessageAndFaceTime",
    "AppStore",
    "Safety",
    "Multitasking",
    "ActionButton",
    "TermsOfAddress",
    "AccessibilityAppearance",
    "Welcome",
    "Appearance",
    "RestoreCompleted",
    "UpdateCompleted",
    "WiFi",
    "Display",
    "Tone",
    "LanguageAndLocale",
    "TouchID",
    "TrueToneDisplay",
    "FileVault",
    "iCloudStorage",
    "iCloudDiagnostics",
    "Registration",
    "DeviceToDeviceMigration",
    "UnlockWithWatch",
    "Accessibility",
    "All",
    "ExpressLanguage",
    "Language",
    "Region",
    "Avatar",
    "DeviceProtection",
    "Key",
    "LockdownMode",
    "Wallpaper",
    "PrivacySubtitle",
    "SecuritySubtitle",
    "DataSubtitle",
    "AppleIDSubtitle",
    "AppearanceSubtitle",
    "PreferredLang",
    "OnboardingSubtitle",
    "AppleTVSubtitle",
    "Intelligence",
    "WebContentFiltering",
    "CameraButton",
    "AdditionalPrivacySettings",
    "EnableLockdownMode",
    "OSShowcase",
    "SafetyAndHandling",
    "Tips",
    "AgeBasedSafetySettings",
    "Waitai",
    "StolenDeviceProtection",
]


def build_ios26_cloud_config(
    existing: dict[str, Any] | None = None,
    *,
    supervised: bool = False,
    organization_name: str = "",
    supervisor_der_cert: bytes | None = None,
) -> dict[str, Any]:
    """
    Build the canonical CloudConfiguration dictionary used by both:
      - `MobileConfigService.set_cloud_configuration` (direct USB push)
      - iOS 15 - 26.x `CloudConfigurationDetails.plist` in backup-restore flows.
    """
    cloud_config: dict[str, Any] = dict(existing or {})
    cloud_config["SkipSetup"] = list(SKIP_SETUP_KEYS)
    cloud_config["AllowPairing"] = True
    cloud_config["ConfigurationWasApplied"] = True
    cloud_config["CloudConfigurationUIComplete"] = True
    cloud_config["IsSupervised"] = bool(supervised)
    cloud_config["ConfigurationSource"] = 0
    cloud_config["PostSetupProfileWasInstalled"] = True
    cloud_config["IsMandatory"] = False
    cloud_config["IsMultiUser"] = False
    cloud_config["IsMDMUnremovable"] = 0 if not supervised else False

    if supervised and organization_name:
        cloud_config["OrganizationName"] = organization_name
        cloud_config["OrganizationMagic"] = str(uuid.uuid4())
        cloud_config["IsMDMUnremovable"] = False
        if supervisor_der_cert:
            cloud_config["SupervisorHostCertificates"] = [supervisor_der_cert]
    else:
        if organization_name:
            cloud_config["OrganizationName"] = organization_name
        cloud_config.pop("OrganizationMagic", None)
        cloud_config.pop("SupervisorHostCertificates", None)

    return cloud_config


def build_cloud_config_details_plist(
    *,
    is_supervised: bool = False,
    organization_name: str = "",
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """
    Generate `CloudConfigurationDetails.plist` (and `CloudConfigurationSetAsideDetails.plist`)
    for `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`.

    When restored to the device, `profiled` and `Setup.app` read this plist and see:
      - `CloudConfigurationUIComplete = True`
      - `ConfigurationWasApplied = True`
      - `IsSupervised = False`
      - `IsMDMUnremovable = 0`
      - `IsMandatory = False`
      - `PostSetupProfileWasInstalled = True`
      - `SkipSetup = [...]` (skips remote management and all iOS 15-26.x setup screens)
    """
    payload = build_ios26_cloud_config(
        supervised=is_supervised,
        organization_name=organization_name,
    )
    return plistlib.dumps(payload, fmt=fmt)


def build_managed_purplebuddy_plist(
    *,
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """
    Generate `mobile/com.apple.purplebuddy.plist` for `ManagedPreferencesDomain`.
    Modern iOS (15 - 26.x) Setup.app checks `ManagedPreferencesDomain` in addition to
    `HomeDomain` to determine if Setup Assistant has completed all steps.
    """
    payload: dict[str, Any] = {
        "SetupDone": True,
        "SetupFinishedAllSteps": True,
        "UserChoseLanguage": True,
    }
    return plistlib.dumps(payload, fmt=fmt)


def build_home_purplebuddy_plist(
    existing_plist_bytes: bytes | None = None,
    *,
    setup_version: int = 26,
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """
    Generate or upgrade `Library/Preferences/com.apple.purplebuddy.plist` for `HomeDomain`.
    Fixes the legacy Chinese locale (`zh_CN`) and outdated `SetupVersion = 7` present in
    MDMPatcher-Universal's 2018 iMyFone backup while preserving compatibility keys.
    Defaults `SetupVersion` to `26` (the verified maximum); callers such as
    `build_ios26_mdm_payloads` raise it to the target device's own iOS major.
    """
    base: dict[str, Any] = {}
    if existing_plist_bytes:
        try:
            loaded = plistlib.loads(existing_plist_bytes)
            if isinstance(loaded, dict):
                base.update(loaded)
        except Exception:
            pass

    base.update(
        {
            "AppleIDPB10Presented": True,
            "AppleIDPB3Presented": True,
            "ApplePayOnBoardingPresented": True,
            "ApplicationSwitcherOnBoardingPresented": True,
            "AssistantPresented": True,
            "AutoUpdatePresented": True,
            "ControlCenterOnBoardingPresented": True,
            "GoHomeOnBoardingPresented": True,
            "HSA2UpgradeMiniBuddy3Ran": True,
            "KeychainSync3Presented": True,
            "Language": "en",
            "Locale": "en_US",
            "PBDiagnostics4Presented": True,
            "PBDiagnosticsPresented": True,
            "PBTCPresented": True,
            "Passcode4Presented": True,
            "Payment2Presented": True,
            "PaymentMiniBuddy4Ran": True,
            "PrivacyContentVersion": 2,
            "PrivacyPresented": True,
            "RestoreChoice": "Update",
            "ScreenTimePresented": True,
            "SetupDone": True,
            "SetupFinishedAllSteps": True,
            "SetupLastExit": "normal",
            "SetupState": "SetupUsingiTunes",
            "SetupVersion": setup_version,
            "SiriOnBoardingPresented": True,
            "TrueTonePresented": True,
            "UserChoseLanguage": True,
            "UserInterfaceStyleModePresented": True,
            "WiFiPresented": True,
        }
    )
    return plistlib.dumps(base, fmt=fmt)


def build_mcmeta_plist(
    build_version: str,
    *,
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """
    Generate `Library/ConfigurationProfiles/PublicInfo/MCMeta.plist`.
    In MDMPatcher-Universal, `LastMigratedBuild` was hardcoded to `10B329` (iOS 6.1.3!),
    which causes `profiled` on newer iOS versions to trigger a full profile migration wipe.
    Setting `LastMigratedBuild` to the target device's actual `BuildVersion` prevents this.
    """
    return plistlib.dumps({"LastMigratedBuild": build_version}, fmt=fmt)


def build_mc_data_migration_plist(
    *,
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """Generate `Library/ConfigurationProfiles/MCDataMigration.plist`."""
    return plistlib.dumps(
        {
            "didMigrate": True,
            "didRestore": True,
            "passcodeSet": 0,
        },
        fmt=fmt,
    )


def build_payload_manifest_plist(
    *,
    fmt: int = plistlib.FMT_BINARY,
) -> bytes:
    """Generate `Library/ConfigurationProfiles/PayloadManifest.plist` with empty profile lists."""
    return plistlib.dumps(
        {
            "HiddenProfiles": [],
            "OrderedProfiles": [],
        },
        fmt=fmt,
    )


def build_empty_dict_plist(*, fmt: int = plistlib.FMT_BINARY) -> bytes:
    """Generate an empty dictionary plist (`ClientTruth.plist`, `ProfileTruth.plist`)."""
    return plistlib.dumps({}, fmt=fmt)


def build_user_settings_plist(*, fmt: int = plistlib.FMT_BINARY) -> bytes:
    """
    Generate `Library/ConfigurationProfiles/UserSettings.plist` and
    `Library/ConfigurationProfiles/PublicInfo/EffectiveUserSettings.plist`
    with permissive restrictions and no MDM enforcement.
    """
    restricted_bool: dict[str, Any] = {
        key: {"preference": value, "value": value}
        for key, value in {
            "allowAccountModification": True,
            "allowActivityContinuation": True,
            "allowAddingGameCenterFriends": True,
            "allowAppCellularDataModification": True,
            "allowAppInstallation": True,
            "allowAppRemoval": True,
            "allowAssistant": True,
            "allowAssistantUserGeneratedContent": True,
            "allowAssistantWhileLocked": True,
            "allowAutoCorrection": True,
            "allowAutomaticAppDownloads": True,
            "allowBookstore": True,
            "allowBookstoreErotica": True,
            "allowCamera": True,
            "allowChat": True,
            "allowCloudBackup": True,
            "allowCloudDocumentSync": True,
            "allowCloudPhotoLibrary": True,
            "allowContinuousPathKeyboard": True,
            "allowDefinitionLookup": True,
            "allowDeviceNameModification": True,
            "allowDiagnosticSubmission": True,
            "allowDictation": True,
            "allowEnablingRestrictions": True,
            "allowEnterpriseAppTrust": True,
            "allowEnterpriseBookBackup": True,
            "allowEnterpriseBookMetadataSync": True,
            "allowEraseContentAndSettings": True,
            "allowExplicitContent": True,
            "allowFindMyDevice": True,
            "allowFindMyFriends": True,
            "allowFingerprintForUnlock": True,
            "allowFingerprintModification": True,
            "allowGameCenter": True,
            "allowGlobalBackgroundFetchWhenRoaming": True,
            "allowHostPairing": True,
            "allowInAppPurchases": True,
            "allowKeyboardShortcuts": True,
            "allowManagedAppsCloudSync": True,
            "allowMultiplayerGaming": True,
            "allowMusicService": True,
            "allowNews": True,
            "allowNotificationsModification": True,
            "allowOpenFromManagedToUnmanaged": True,
            "allowOpenFromUnmanagedToManaged": True,
            "allowPairedWatch": True,
            "allowPassbookWhileLocked": True,
            "allowPasscodeModification": True,
            "allowPhotoStream": True,
            "allowPredictiveKeyboard": True,
            "allowRadioService": True,
            "allowRemoteAppPairing": True,
            "allowSafari": True,
            "allowScreenShot": True,
            "allowSharedStream": True,
            "allowSpellCheck": True,
            "allowSpotlightInternetResults": True,
            "allowUIAppInstallation": True,
            "allowUIConfigurationProfileInstallation": True,
            "allowUNstrustedTLSPrompt": True,
            "allowUntrustedTLSPrompt": True,
            "allowUSBFileDriveAccess": True,
            "allowUSBRestrictedMode": True,
            "allowVideoConferencing": True,
            "allowVoiceDialing": True,
            "allowWallpaperModification": True,
            "forceAssistantProfanityFilter": False,
            "forceEncryptedBackup": False,
            "forceITunesStorePasswordEntry": False,
            "forceLimitAdTracking": False,
            "forceWatchWristDetection": False,
            " safariAllowAutoFill": True,
            "safariAllowAutoFill": True,
            "safariAllowJavaScript": True,
            "safariAllowPopups": False,
            "safariForceFraudWarning": False,
        }.items()
    }
    restricted_value: dict[str, Any] = {
        "lar": {"value": 1000},
        " linkedIn": {"value": 1000},
        "maxGracePeriod": {" preference": 28800, "value": 28800},
        "maxInactivity": {"preference": 60, "value": 120},
        "minLength": {"preference": 0, "value": 0},
        "minComplexChars": {"preference": 0, "value": 0},
        "ratingApps": {"preference": 1000, "value": 1000},
        "ratingMovies": {"preference": 1000, "value": 1000},
        "ratingRegion": {"value": "us"},
        "ratingTVShows": {"preference": 1000, "value": 1000},
        "safariAcceptCookies": {"preference": 2, "value": 2},
    }
    payload: dict[str, Any] = {
        "assignedObject": {},
        "restrictedBool": restricted_bool,
        "restrictedValue": restricted_value,
    }
    return plistlib.dumps(payload, fmt=fmt)


def build_ota_disabler_plist(*, fmt: int = plistlib.FMT_BINARY) -> bytes:
    """
    Generate `Library/Preferences/com.apple.MobileAsset.plist` for `RootDomain`
    pointing `MobileAssetServerURL-com.apple.MobileAsset.SoftwareUpdate` to an invalid
    loopback URL so OTA updates do not inadvertently re-trigger Setup Assistant.
    """
    return plistlib.dumps(
        {
            "LastDanglingReferenceCheck": plistlib.loads(
                plistlib.dumps({"d": 0})
            ).get("d", 0),
            "MobileAssetServerURL-com.apple.MobileAsset.SoftwareUpdate": "http://127.0.0.1:1/ota-blocked",
            "MobileAssetAssetAudience": "00000000-0000-0000-0000-000000000000",
        },
        fmt=fmt,
    )
