#ifndef PATCH_LOGIC_H
#define PATCH_LOGIC_H

#include <stddef.h>

/**
 * @brief Replicates Swift's Data.swapAt logic for obfuscation.
 */
void apply_byte_swaps(unsigned char* data, size_t len);

/**
 * @brief Decrypts RNCryptor v3 formatted data with the reverse-engineered password.
 * @return 0 on success, -1 on failure.
 */
int smart_decrypt(unsigned char* input, size_t len, unsigned char** output, size_t* out_len);

/**
 * @brief Patches Info.plist with device-specific metadata (including ProductVersion).
 */
void patchFile1(const char* build, const char* imei, const char* type,
                const char* sn, const char* udid, const char* path);

/**
 * @brief Patches Manifest.plist with device-specific metadata.
 */
void patchFile2(const char* build, const char* imei, const char* type,
                const char* sn, const char* udid, const char* path);

/**
 * @brief Decrypts and extracts the libiMobileeDevice.dylib ZIP archive to disk.
 */
void patchFile3(const char* zip_buffer, size_t len, const char* target_path);

/**
 * @brief Injects modern iOS 15-18+ SysSharedContainerDomain CloudConfigurationDetails.plist
 *        and ManagedPreferencesDomain com.apple.purplebuddy.plist into MDMB/ and Manifest.mbdb.
 */
int patchModernMDM(const char* path);

/**
 * @brief Utility for string substitution within XML/Plist content.
 */
char* str_replace(const char* orig, const char* rep, const char* with);

#endif // PATCH_LOGIC_H
