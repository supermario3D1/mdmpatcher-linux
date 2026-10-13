#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <ctype.h>
#include <sys/stat.h>
#include <errno.h>
#include <plist/plist.h>
#include "utils.h"

char *string_build_path(const char *elem, ...) {
    if (!elem) return NULL;
    va_list args;
    int len = strlen(elem) + 1;
    va_start(args, elem);
    char *arg = va_arg(args, char*);
    while (arg) {
        len += strlen(arg) + 1;
        arg = va_arg(args, char*);
    }
    va_end(args);

    char *path = malloc(len);
    strcpy(path, elem);

    va_start(args, elem);
    arg = va_arg(args, char*);
    while (arg) {
        strcat(path, "/");
        strcat(path, arg);
        arg = va_arg(args, char*);
    }
    va_end(args);
    return path;
}

char *string_format_size(uint64_t size) {
    char *str = malloc(24);
    if (size > 1073741824)
        sprintf(str, "%.1f GB", (float)size / 1073741824.0);
    else if (size > 1048576)
        sprintf(str, "%.1f MB", (float)size / 1048576.0);
    else if (size > 1024)
        sprintf(str, "%.1f KB", (float)size / 1024.0);
    else
        sprintf(str, "%d B", (int)size);
    return str;
}

char *string_toupper(char *str) {
    char *p = str;
    while (*p) {
        *p = toupper(*p);
        p++;
    }
    return str;
}

int plist_read_from_filename(plist_t *plist, const char *filename) {
    struct stat st;
    if (stat(filename, &st) < 0) return 0;
    
    FILE *f = fopen(filename, "rb");
    if (!f) return 0;
    
    char *buf = malloc(st.st_size);
    if (fread(buf, 1, st.st_size, f) != st.st_size) {
        fclose(f);
        free(buf);
        return 0;
    }
    fclose(f);
    
    if (memcmp(buf, "bplist00", 8) == 0) {
        plist_from_bin(buf, st.st_size, plist);
    } else {
        plist_from_xml(buf, st.st_size, plist);
    }
    free(buf);
    return (*plist != NULL);
}

int plist_write_to_filename(plist_t plist, const char *filename, plist_format_t format) {
    char *out = NULL;
    uint32_t len = 0;
    
    if (format == 1) /* PLIST_FORMAT_BINARY */
        plist_to_bin(plist, &out, &len);
    else
        plist_to_xml(plist, &out, &len);
        
    if (!out) return 0;
    
    FILE *f = fopen(filename, "wb");
    if (!f) { free(out); return 0; }
    fwrite(out, 1, len, f);
    fclose(f);
    free(out);
    return 1;
}