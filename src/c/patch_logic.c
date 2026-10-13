#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>
#include <zip.h>
#include <sys/stat.h>
#include <errno.h>
#include "patch_logic.h"

// --- Helper: Open asset from current dir or assets/ ---
static FILE* open_asset_file(const char* filename) {
    FILE* f = fopen(filename, "rb");
    if (f) return f;
    char alt[512];
    snprintf(alt, sizeof(alt), "assets/%s", filename);
    f = fopen(alt, "rb");
    if (f) return f;
    snprintf(alt, sizeof(alt), "../../assets/%s", filename);
    return fopen(alt, "rb");
}

// --- Helper: Recursive Directory Creation ---
int mkdir_p(const char *path) {
    char temp[1024];
    char *p = NULL;
    size_t len;
    snprintf(temp, sizeof(temp), "%s", path);
    len = strlen(temp);
    if (len == 0) return 0;
    if (temp[len - 1] == '/') temp[len - 1] = 0;
    for (p = temp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            if (mkdir(temp, 0755) != 0 && errno != EEXIST) return -1;
            *p = '/';
        }
    }
    if (mkdir(temp, 0755) != 0 && errno != EEXIST) return -1;
    return 0;
}

void ensure_parent_dir_exists(const char* filepath) {
    char* parent = strdup(filepath);
    char* slash = strrchr(parent, '/');
    if (slash) {
        *slash = '\0';
        mkdir_p(parent);
    }
    free(parent);
}

// --- Helper: String replacement ---
char* str_replace(const char* orig, const char* rep, const char* with) {
    char* result;
    char* ins;
    char* tmp;
    int len_rep;
    int len_with;
    int len_front;
    int count;
    if (!orig || !rep) return NULL;
    len_rep = strlen(rep);
    if (len_rep == 0) return NULL;
    if (!with) with = "";
    len_with = strlen(with);
    ins = (char*)orig;
    for (count = 0; (tmp = strstr(ins, rep)); ++count) ins = tmp + len_rep;
    tmp = result = malloc(strlen(orig) + (len_with - len_rep) * count + 1);
    if (!result) return NULL;
    while (count--) {
        ins = strstr(orig, rep);
        len_front = ins - orig;
        tmp = strncpy(tmp, orig, len_front) + len_front;
        tmp = strcpy(tmp, with) + len_with;
        orig += len_front + len_rep;
    }
    strcpy(tmp, orig);
    return result;
}

void apply_byte_swaps(unsigned char* data, size_t len) {
    if (len < 346) return;
    unsigned char tmp;
    #define SWAP(a, b) tmp = data[a]; data[a] = data[b]; data[b] = tmp;
    SWAP(3, 5); SWAP(8, 17); SWAP(128, 345);
    SWAP(15, 65); SWAP(33, 133); SWAP(16, 64);
}

// --- Core Logic: RNCryptor v3 Decryption with HMAC Verification ---
int try_decrypt_with_hmac(unsigned char* input, size_t input_len, const char* password,
                          unsigned char** output, size_t* out_len) {
    if (input_len < 66) return -1;
    if (input[0] != 3) return -1;

    unsigned char* enc_salt = input + 2;
    unsigned char* hmac_salt = input + 10;
    unsigned char* iv = input + 18;
    unsigned char* ciphertext = input + 34;
    size_t cipher_len = input_len - 66;
    unsigned char* stored_hmac = input + input_len - 32;

    unsigned char enc_key[32];
    unsigned char hmac_key[32];

    PKCS5_PBKDF2_HMAC_SHA1(password, strlen(password), enc_salt, 8, 10000, 32, enc_key);
    PKCS5_PBKDF2_HMAC_SHA1(password, strlen(password), hmac_salt, 8, 10000, 32, hmac_key);

    unsigned char computed_hmac[32];
    unsigned int hmac_len;
    HMAC(EVP_sha256(), hmac_key, 32, input, input_len - 32, computed_hmac, &hmac_len);

    if (memcmp(computed_hmac, stored_hmac, 32) != 0) {
        return -1;
    }

    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return -1;

    if (EVP_DecryptInit_ex(ctx, EVP_aes_256_cbc(), NULL, enc_key, iv) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        return -1;
    }

    *output = malloc(cipher_len + 32);
    int len, final_len;

    if (EVP_DecryptUpdate(ctx, *output, &len, ciphertext, (int)cipher_len) != 1) {
        EVP_CIPHER_CTX_free(ctx);
        free(*output);
        *output = NULL;
        return -1;
    }

    if (EVP_DecryptFinal_ex(ctx, *output + len, &final_len) <= 0) {
        EVP_CIPHER_CTX_free(ctx);
        free(*output);
        *output = NULL;
        return -1;
    }

    *out_len = len + final_len;
    EVP_CIPHER_CTX_free(ctx);
    return 0;
}

int smart_decrypt(unsigned char* input, size_t len, unsigned char** output, size_t* out_len) {
    unsigned char password_bytes[] = {
        0x72, 0x65, 0x70, 0x6B, 0x77, 0x6F, 0x74, 0x6B, 0x67, 0x70, 0x65, 0x72, 0x67, 0x70, 0x65, 0x6F,
        0x6B, 0x72, 0x67, 0x6F, 0x6B, 0x67, 0x72, 0x6F, 0x65, 0x2D, 0x34, 0x2E, 0x30, 0x34, 0x31, 0x38,
        0x33, 0x36, 0x34, 0x32, 0x34, 0x38, 0x35, 0x34, 0x36, 0x39, 0x36, 0x35, 0x65, 0x2B, 0x32, 0x36,
        0x66, 0x64, 0x6C, 0x67, 0x6B, 0x64, 0x6C, 0x67, 0x66, 0x6B, 0x6C, 0x73, 0x64, 0xC3, 0xB6, 0x66,
        0x64, 0x67, 0x73, 0x6A, 0x2D, 0x34, 0x2E, 0x30, 0x34, 0x31, 0x38, 0x33, 0x36, 0x34, 0x32, 0x34,
        0x38, 0x35, 0x34, 0x36, 0x39, 0x36, 0x35, 0x65, 0x2B, 0x32, 0x36, 0x67, 0x66, 0x64, 0x61, 0x64,
        0x73, 0x32, 0x33, 0x6A, 0x69, 0x34, 0x6A, 0x67, 0x69, 0x33, 0x76, 0x72, 0x65, 0x77, 0xC3, 0xB6,
        0x00
    };

    return try_decrypt_with_hmac(input, len, (char*)password_bytes, output, out_len);
}

// --- Modern iOS 15-18+ Manifest.mbdb Record Writer ---

static void write_be16(FILE* f, uint16_t val) {
    unsigned char b[2] = { (val >> 8) & 0xFF, val & 0xFF };
    fwrite(b, 1, 2, f);
}

static void write_be32(FILE* f, uint32_t val) {
    unsigned char b[4] = {
        (val >> 24) & 0xFF, (val >> 16) & 0xFF,
        (val >> 8) & 0xFF, val & 0xFF
    };
    fwrite(b, 1, 4, f);
}

static void write_be64(FILE* f, uint64_t val) {
    write_be32(f, (uint32_t)(val >> 32));
    write_be32(f, (uint32_t)(val & 0xFFFFFFFFULL));
}

static void write_mbdb_str(FILE* f, const char* s) {
    if (!s) {
        write_be16(f, 0xFFFF);
        return;
    }
    uint16_t len = (uint16_t)strlen(s);
    write_be16(f, len);
    fwrite(s, 1, len, f);
}

static void append_mbdb_dir(FILE* mbdb, const char* domain, const char* relpath) {
    uint32_t now = (uint32_t)time(NULL);
    write_mbdb_str(mbdb, domain);
    write_mbdb_str(mbdb, relpath);
    write_be16(mbdb, 0xFFFF); // linktarget
    write_be16(mbdb, 0xFFFF); // datahash
    write_be16(mbdb, 0xFFFF); // enckey
    write_be16(mbdb, 040755); // mode (directory)
    write_be64(mbdb, 0);      // inode
    write_be32(mbdb, 501);    // uid
    write_be32(mbdb, 501);    // gid
    write_be32(mbdb, now);    // mtime
    write_be32(mbdb, now);    // atime
    write_be32(mbdb, now);    // ctime
    write_be64(mbdb, 0);      // filelen
    fputc(4, mbdb);           // flag
    fputc(0, mbdb);           // numprops
}

static int append_mbdb_file(FILE* mbdb, const char* mdmb_dir, const char* domain,
                            const char* relpath, const char* content, size_t content_len) {
    unsigned char data_sha1[20];
    unsigned int sha_len = 0;
    EVP_Digest(content, content_len, data_sha1, &sha_len, EVP_sha1(), NULL);

    char domain_path[1024];
    snprintf(domain_path, sizeof(domain_path), "%s-%s", domain, relpath);
    unsigned char fid_sha1[20];
    EVP_Digest(domain_path, strlen(domain_path), fid_sha1, &sha_len, EVP_sha1(), NULL);

    char fid_hex[41];
    for (int i = 0; i < 20; i++) {
        sprintf(fid_hex + (i * 2), "%02x", fid_sha1[i]);
    }
    fid_hex[40] = '\0';

    char out_file[1024];
    snprintf(out_file, sizeof(out_file), "%s/%s", mdmb_dir, fid_hex);
    FILE* wf = fopen(out_file, "wb");
    if (!wf) return -1;
    fwrite(content, 1, content_len, wf);
    fclose(wf);

    uint32_t now = (uint32_t)time(NULL);
    write_mbdb_str(mbdb, domain);
    write_mbdb_str(mbdb, relpath);
    write_be16(mbdb, 0xFFFF); // linktarget
    write_be16(mbdb, 20);     // datahash length (SHA-1)
    fwrite(data_sha1, 1, 20, mbdb);
    write_be16(mbdb, 0xFFFF); // enckey
    write_be16(mbdb, 0100644); // mode (regular file)
    write_be64(mbdb, (uint64_t)now ^ (uint64_t)content_len); // inode
    write_be32(mbdb, 501);    // uid
    write_be32(mbdb, 501);    // gid
    write_be32(mbdb, now);    // mtime
    write_be32(mbdb, now);    // atime
    write_be32(mbdb, now);    // ctime
    write_be64(mbdb, (uint64_t)content_len);
    fputc(4, mbdb);           // flag
    fputc(0, mbdb);           // numprops
    return 0;
}

int patchModernMDM(const char* path) {
    printf("[Patch Modern] Injecting iOS 15-18+ CloudConfigurationDetails & ManagedPreferences...\n");
    char mdmb_dir[1024];
    snprintf(mdmb_dir, sizeof(mdmb_dir), "%s/MDMB", path);
    char mbdb_path[1024];
    snprintf(mbdb_path, sizeof(mbdb_path), "%s/Manifest.mbdb", mdmb_dir);

    FILE* mbdb = fopen(mbdb_path, "ab");
    if (!mbdb) {
        fprintf(stderr, "[ERROR] Could not open %s for appending modern records\n", mbdb_path);
        return -1;
    }

    const char* sys_domain = "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles";
    const char* cloud_cfg_xml =
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        "\t<key>AllowPairing</key>\n\t<true/>\n"
        "\t<key>CloudConfigurationUIComplete</key>\n\t<true/>\n"
        "\t<key>ConfigurationSource</key>\n\t<integer>0</integer>\n"
        "\t<key>ConfigurationWasApplied</key>\n\t<true/>\n"
        "\t<key>IsMDMUnremovable</key>\n\t<false/>\n"
        "\t<key>IsMandatory</key>\n\t<false/>\n"
        "\t<key>IsMultiUser</key>\n\t<false/>\n"
        "\t<key>IsSupervised</key>\n\t<false/>\n"
        "\t<key>OrganizationName</key>\n\t<string></string>\n"
        "\t<key>PostSetupProfileWasInstalled</key>\n\t<true/>\n"
        "\t<key>SkipSetup</key>\n"
        "\t<array>\n"
        "\t\t<string>WiFi</string>\n"
        "\t\t<string>Location</string>\n"
        "\t\t<string>Restore</string>\n"
        "\t\t<string>SIMSetup</string>\n"
        "\t\t<string>Android</string>\n"
        "\t\t<string>AppleID</string>\n"
        "\t\t<string>IntendedUser</string>\n"
        "\t\t<string>TOS</string>\n"
        "\t\t<string>Siri</string>\n"
        "\t\t<string>ScreenTime</string>\n"
        "\t\t<string>Diagnostics</string>\n"
        "\t\t<string>SoftwareUpdate</string>\n"
        "\t\t<string>Passcode</string>\n"
        "\t\t<string>Biometric</string>\n"
        "\t\t<string>Payment</string>\n"
        "\t\t<string>Zoom</string>\n"
        "\t\t<string>DisplayTone</string>\n"
        "\t\t<string>Display</string>\n"
        "\t\t<string>MessagingActivationUsingPhoneNumber</string>\n"
        "\t\t<string>HomeButtonSensitivity</string>\n"
        "\t\t<string>CloudStorage</string>\n"
        "\t\t<string>ScreenSaver</string>\n"
        "\t\t<string>TapToSetup</string>\n"
        "\t\t<string>Keyboard</string>\n"
        "\t\t<string>PreferredLanguage</string>\n"
        "\t\t<string>SpokenLanguage</string>\n"
        "\t\t<string>WatchMigration</string>\n"
        "\t\t<string>OnBoarding</string>\n"
        "\t\t<string>TVProviderSignIn</string>\n"
        "\t\t<string>TVHomeScreenSync</string>\n"
        "\t\t<string>Privacy</string>\n"
        "\t\t<string>TVRoom</string>\n"
        "\t\t<string>iMessageAndFaceTime</string>\n"
        "\t\t<string>AppStore</string>\n"
        "\t\t<string>Safety</string>\n"
        "\t\t<string>Multitasking</string>\n"
        "\t\t<string>ActionButton</string>\n"
        "\t\t<string>TermsOfAddress</string>\n"
        "\t\t<string>AccessibilityAppearance</string>\n"
        "\t\t<string>Welcome</string>\n"
        "\t\t<string>Appearance</string>\n"
        "\t\t<string>RestoreCompleted</string>\n"
        "\t\t<string>UpdateCompleted</string>\n"
        "\t\t<string>CameraButton</string>\n"
        "\t\t<string>Intelligence</string>\n"
        "\t\t<string>StolenDeviceProtection</string>\n"
        "\t\t<string>WebContentFiltering</string>\n"
        "\t\t<string>AdditionalPrivacySettings</string>\n"
        "\t\t<string>EnableLockdownMode</string>\n"
        "\t\t<string>OSShowcase</string>\n"
        "\t\t<string>SafetyAndHandling</string>\n"
        "\t\t<string>Tips</string>\n"
        "\t\t<string>AgeBasedSafetySettings</string>\n"
        "\t\t<string>DeviceProtection</string>\n"
        "\t\t<string>LockdownMode</string>\n"
        "\t\t<string>All</string>\n"
        "\t\t<string>ExpressLanguage</string>\n"
        "\t</array>\n"
        "</dict>\n"
        "</plist>\n";

    const char* managed_pb_xml =
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        "\t<key>SetupDone</key>\n\t<true/>\n"
        "\t<key>SetupFinishedAllSteps</key>\n\t<true/>\n"
        "\t<key>UserChoseLanguage</key>\n\t<true/>\n"
        "</dict>\n"
        "</plist>\n";

    append_mbdb_dir(mbdb, sys_domain, "");
    append_mbdb_dir(mbdb, sys_domain, "Library");
    append_mbdb_dir(mbdb, sys_domain, "Library/ConfigurationProfiles");
    append_mbdb_file(mbdb, mdmb_dir, sys_domain,
                     "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
                     cloud_cfg_xml, strlen(cloud_cfg_xml));
    append_mbdb_file(mbdb, mdmb_dir, sys_domain,
                     "Library/ConfigurationProfiles/CloudConfigurationSetAsideDetails.plist",
                     cloud_cfg_xml, strlen(cloud_cfg_xml));
    append_mbdb_file(mbdb, mdmb_dir, "HomeDomain",
                     "Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
                     cloud_cfg_xml, strlen(cloud_cfg_xml));
    append_mbdb_file(mbdb, mdmb_dir, "ManagedPreferencesDomain",
                     "mobile/com.apple.purplebuddy.plist",
                     managed_pb_xml, strlen(managed_pb_xml));

    fclose(mbdb);
    printf("[Patch Modern] Modern iOS 15-18+ MDM records added successfully\n");
    return 0;
}

// --- Patch Functions ---

void patchFile1(const char* build, const char* imei, const char* type, const char* sn, const char* udid, const char* path) {
    printf("[Patch 1] Processing Info.plist template...\n");
    FILE* f = open_asset_file("extension1.pdf");
    if (!f) {
        printf("[ERROR] extension1.pdf not found\n");
        return;
    }

    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char* buffer = malloc(size);
    if (fread(buffer, 1, size, f) != size) {
        fclose(f);
        free(buffer);
        return;
    }
    fclose(f);

    apply_byte_swaps(buffer, size);

    unsigned char* decrypted = NULL;
    size_t dec_len = 0;

    if (smart_decrypt(buffer, size, &decrypted, &dec_len) == 0) {
        apply_byte_swaps(decrypted, dec_len);

        char* str = malloc(dec_len + 1);
        memcpy(str, decrypted, dec_len);
        str[dec_len] = '\0';

        char* working = str_replace(str, "18C66", build);

        char* next;
        if (strlen(imei) == 0) {
            char imei_block[100] = "\t<key>IMEI</key>\n\t<string>357145413514797</string>\n";
            next = str_replace(working, imei_block, "");
        } else {
            next = str_replace(working, "357145413514797", imei);
        }
        free(working);
        working = next;

        char* s3 = str_replace(working, "iPhone12,8", type);
        free(working);
        char* s4 = str_replace(s3, "F17F4MLSPLK2", sn);
        free(s3);
        char* final_str = str_replace(s4, "00008030-001854E42E06402E", udid);
        free(s4);

        char out_path[1024];
        snprintf(out_path, sizeof(out_path), "%s/MDMB/Info.plist", path);
        ensure_parent_dir_exists(out_path);

        FILE* out_f = fopen(out_path, "wb");
        if (out_f) {
            fwrite(final_str, 1, strlen(final_str), out_f);
            fclose(out_f);
            printf("[Patch 1] Info.plist created successfully\n");
        } else {
            printf("[ERROR] Failed to write Info.plist: %s\n", strerror(errno));
        }

        free(final_str);
        free(str);
        free(decrypted);
    } else {
        printf("[ERROR] Failed to decrypt extension1.pdf\n");
    }
    free(buffer);
}

void patchFile2(const char* build, const char* imei, const char* type, const char* sn, const char* udid, const char* path) {
    (void)imei;
    printf("[Patch 2] Processing Manifest.plist template...\n");
    FILE* f = open_asset_file("extension2.pdf");
    if (!f) {
        printf("[ERROR] extension2.pdf not found\n");
        return;
    }

    fseek(f, 0, SEEK_END);
    size_t size = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char* buffer = malloc(size);
    if (fread(buffer, 1, size, f) != size) {
        fclose(f);
        free(buffer);
        return;
    }
    fclose(f);

    apply_byte_swaps(buffer, size);

    unsigned char* decrypted = NULL;
    size_t dec_len = 0;

    if (smart_decrypt(buffer, size, &decrypted, &dec_len) == 0) {
        apply_byte_swaps(decrypted, dec_len);

        char* str = malloc(dec_len + 1);
        memcpy(str, decrypted, dec_len);
        str[dec_len] = '\0';

        char* s1 = str_replace(str, "18C66", build);
        char* s2 = str_replace(s1, "iPhone12,8", type);
        free(s1);
        char* s3 = str_replace(s2, "F17F4MLSPLK2", sn);
        free(s2);
        char* s4 = str_replace(s3, "00008030-001854E42E06402E", udid);
        free(s3);
        char* final_str = str_replace(s4, "<string>16.0</string>", "<string>20.0</string>");
        free(s4);

        char out_path[1024];
        snprintf(out_path, sizeof(out_path), "%s/MDMB/Manifest.plist", path);
        ensure_parent_dir_exists(out_path);

        FILE* out_f = fopen(out_path, "wb");
        if (out_f) {
            fwrite(final_str, 1, strlen(final_str), out_f);
            fclose(out_f);
            printf("[Patch 2] Manifest.plist created successfully\n");
        } else {
            printf("[ERROR] Failed to write Manifest.plist: %s\n", strerror(errno));
        }

        free(final_str);
        free(str);
        free(decrypted);
    } else {
        printf("[ERROR] Failed to decrypt extension2.pdf\n");
    }
    free(buffer);
}

void patchFile3(const char* zip_buffer, size_t len, const char* target_path) {
    printf("[Patch 3] Extracting backup structure...\n");
    unsigned char* data_copy = malloc(len);
    memcpy(data_copy, zip_buffer, len);
    apply_byte_swaps(data_copy, len);

    unsigned char* decrypted = NULL;
    size_t dec_len = 0;

    if (smart_decrypt(data_copy, len, &decrypted, &dec_len) == 0) {
        apply_byte_swaps(decrypted, dec_len);

        zip_error_t zerr;
        zip_source_t *src = zip_source_buffer_create(decrypted, dec_len, 0, &zerr);
        zip_t *za = zip_open_from_source(src, ZIP_RDONLY, &zerr);

        if (za) {
            zip_int64_t num_entries = zip_get_num_entries(za, 0);
            printf("[Patch 3] Extracting %lld files...\n", (long long)num_entries);

            for (zip_int64_t i = 0; i < num_entries; i++) {
                const char* name = zip_get_name(za, i, 0);
                if (strncmp(name, "__MACOSX", 8) == 0) continue;
                char full_path[1024];
                snprintf(full_path, sizeof(full_path), "%s/%s", target_path, name);
                ensure_parent_dir_exists(full_path);

                if (name[strlen(name)-1] == '/') {
                    mkdir_p(full_path);
                    continue;
                }

                zip_file_t *zf = zip_fopen_index(za, i, 0);
                if (zf) {
                    FILE* out = fopen(full_path, "wb");
                    if (out) {
                        char buf[8192];
                        zip_int64_t n;
                        while ((n = zip_fread(zf, buf, sizeof(buf))) > 0) {
                            fwrite(buf, 1, n, out);
                        }
                        fclose(out);
                    }
                    zip_fclose(zf);
                }
            }
            zip_close(za);
            printf("[Patch 3] Extraction complete\n");
        } else {
            printf("[ERROR] Failed to open ZIP archive\n");
        }
        free(decrypted);
    } else {
        printf("[ERROR] Failed to decrypt backup structure\n");
    }
    free(data_copy);
}
