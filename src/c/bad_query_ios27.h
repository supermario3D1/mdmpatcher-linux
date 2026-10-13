#ifndef BAD_QUERY_IOS27_H
#define BAD_QUERY_IOS27_H

#include <stdint.h>
#include <stdbool.h>

/*
 * Companion on-device sandbox extension & inode enumeration helper for
 * iOS 26.0 - 26.6.1 / iOS 27.0b1 - 27.0b4 (`bad_query` / CVE-2026 containermanagerd
 * Class-13 MCMSharedSystemDataContainer path traversal).
 *
 * Targets `/var/containers/Shared/SystemGroup/<UUID>/Library/ConfigurationProfiles/`
 * (`SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles`) on-device.
 */

int64_t ios27_acquire_systemgroup_sandbox_token(const char *absolute_path, bool create_if_missing);
void ios27_release_sandbox_token(int64_t handle);
char *ios27_find_configurationprofiles_container(int64_t max_inode);

#endif /* BAD_QUERY_IOS27_H */
