#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <dirent.h>
#include <errno.h>
#include <plist/plist.h>

#include "libidevicefunctions.h"
#include "idevicebackup2.h"
#include "patch_logic.h"

#define TOOL_NAME "mdm_patcher"

unsigned char* read_file_to_buffer(const char* filename, size_t* size) {
    FILE* f = fopen(filename, "rb");
    if (!f) {
        char alt[512];
        snprintf(alt, sizeof(alt), "assets/%s", filename);
        f = fopen(alt, "rb");
        if (!f) {
            snprintf(alt, sizeof(alt), "../../assets/%s", filename);
            f = fopen(alt, "rb");
        }
    }
    if (!f) {
        fprintf(stderr, "Error: Could not open file %s\n", filename);
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    *size = ftell(f);
    fseek(f, 0, SEEK_SET);

    unsigned char* buffer = malloc(*size);
    if (!buffer) {
        fclose(f);
        return NULL;
    }

    if (fread(buffer, 1, *size, f) != *size) {
        fprintf(stderr, "Error: Could not read entire file %s\n", filename);
        free(buffer);
        fclose(f);
        return NULL;
    }

    fclose(f);
    return buffer;
}

char* get_string_from_xml(const char* xml, const char* key) {
    if (!xml) return NULL;
    plist_t plist = NULL;
    char* result = NULL;
    plist_from_xml(xml, strlen(xml), &plist);
    if (!plist) return NULL;
    plist_t node = plist_dict_get_item(plist, key);
    if (node && plist_get_node_type(node) == PLIST_STRING) {
        plist_get_string_val(node, &result);
    }
    plist_free(plist);
    return result;
}

void remove_directory_recursive(const char *path) {
    char command[1024];
    snprintf(command, sizeof(command), "rm -rf \"%s\"", path);
    int rc = system(command);
    (void)rc;
}

int main(int argc, char *argv[]) {
    int legacy_mode = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--legacy") == 0) {
            legacy_mode = 1;
        }
    }

    printf("MDM Patcher Linux v2.0 (iOS 15 - 18+ Universal Edition)\n");
    printf("=======================================================\n\n");
    printf("Waiting for device...\n");

    char* xml_info = getdeviceInformation();
    if (!xml_info || strlen(xml_info) < 10 || strcmp(xml_info, "-1") == 0) {
        fprintf(stderr, "Error: Could not connect to device.\n");
        fprintf(stderr, "Please ensure device is:\n");
        fprintf(stderr, "  - Connected via USB\n");
        fprintf(stderr, "  - Unlocked and trusted this computer\n");
        fprintf(stderr, "  - Not in recovery or DFU mode\n");
        return 1;
    }

    char* productType = get_string_from_xml(xml_info, "ProductType");
    char* productVersion = get_string_from_xml(xml_info, "ProductVersion");
    char* serialNumber = get_string_from_xml(xml_info, "SerialNumber");
    char* buildVersion = get_string_from_xml(xml_info, "BuildVersion");
    char* uniqueDeviceID = get_string_from_xml(xml_info, "UniqueDeviceID");
    char* imei = get_string_from_xml(xml_info, "InternationalMobileEquipmentIdentity");

    if (!imei) imei = strdup("");

    if (!productType || !serialNumber || !buildVersion || !uniqueDeviceID) {
        fprintf(stderr, "Error: Failed to retrieve device information.\n");
        return 1;
    }

    printf("Device detected:\n");
    printf("  Model:    %s\n", productType);
    printf("  Serial:   %s\n", serialNumber);
    printf("  iOS:      %s (%s)\n", productVersion ? productVersion : "Unknown", buildVersion);
    printf("  UDID:     %s\n", uniqueDeviceID);
    printf("  Mode:     %s\n\n", legacy_mode ? "Legacy (MDMPatcher-Universal)" : "Enhanced (iOS 15-18+ Universal)");

    char temp_dir_template[] = "/tmp/mdmpatch_XXXXXX";
    char* temp_path = mkdtemp(temp_dir_template);
    if (!temp_path) {
        perror("Error creating temporary directory");
        return 1;
    }

    size_t zip_len = 0;
    unsigned char* zip_buffer = read_file_to_buffer("libiMobileeDevice.dylib", &zip_len);
    if (!zip_buffer) {
        fprintf(stderr, "Error: 'libiMobileeDevice.dylib' not found in current or assets/ directory.\n");
        remove_directory_recursive(temp_path);
        return 1;
    }

    printf("Preparing backup files...\n");

    patchFile3((const char*)zip_buffer, zip_len, temp_path);
    free(zip_buffer);

    patchFile1(buildVersion, imei, productType, serialNumber, uniqueDeviceID, temp_path);
    patchFile2(buildVersion, imei, productType, serialNumber, uniqueDeviceID, temp_path);

    if (!legacy_mode) {
        patchModernMDM(temp_path);
    }

    char path_buffer[1024];
    snprintf(path_buffer, sizeof(path_buffer), "%s/MDMB/Info.plist", temp_path);
    struct stat st;
    if (stat(path_buffer, &st) != 0) {
        fprintf(stderr, "Error: Failed to generate Info.plist\n");
        remove_directory_recursive(temp_path);
        return 1;
    }

    snprintf(path_buffer, sizeof(path_buffer), "%s/MDMB/Manifest.plist", temp_path);
    if (stat(path_buffer, &st) != 0) {
        fprintf(stderr, "Error: Failed to generate Manifest.plist\n");
        remove_directory_recursive(temp_path);
        return 1;
    }

    printf("\nStarting restore process...\n");
    printf("Please keep device connected and unlocked.\n\n");

    int result = mainLOL(temp_path, uniqueDeviceID);

    if (result == 0) {
        printf("\n✓ MDM patch applied successfully!\n");
        printf("  Your device should now reboot.\n");
        printf("  On iOS 17.2 / 18+: If you see 'iPhone Partially Set Up', tap 'Continue with Partial Setup'.\n");
        remove_directory_recursive(temp_path);
    } else {
        fprintf(stderr, "\n✗ Restore failed (Error code: %d)\n", result);
        fprintf(stderr, "\nTroubleshooting:\n");
        fprintf(stderr, "  - Ensure 'Find My' is disabled\n");
        fprintf(stderr, "  - Device should be on the Setup Assistant screen\n");
        fprintf(stderr, "  - Try rebooting the device and running again\n");
        fprintf(stderr, "\nWorkspace preserved for debugging: %s\n", temp_path);
    }

    free(productType);
    if (productVersion) free(productVersion);
    free(serialNumber);
    free(buildVersion);
    free(uniqueDeviceID);
    free(imei);
    free(xml_info);

    return result;
}
