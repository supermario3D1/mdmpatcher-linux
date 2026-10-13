"""
Modern iOS (iOS 10.2 - iOS 27+) `Manifest.db` SQLite3 generator, injector, and parser.

In Backup v3.2 / v3.3+, Apple replaced `Manifest.mbdb` with a SQLite3 database
named `Manifest.db` containing two tables:
  - `Files(fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)`
  - `Properties(key TEXT PRIMARY KEY, value BLOB)`

Where:
  - `fileID` = `SHA1(domain + "-" + relativePath).hexdigest()`
  - `flags` = 1 (regular file), 2 (directory), 4 (symbolic link)
  - `file` = an `NSKeyedArchiver` binary plist encoding an `MBFile` object.

iOS 27 `BackupAgent2` Hardening Requirements:
  1. Regular file (`flags=1`) and directory (`flags=2`) rows MUST carry a strictly
     unique, non-zero `InodeNumber` (`BackupAgent2` deduplicates by inode; rows
     sharing an inode or carrying `InodeNumber = 0` are overwritten or skipped).
  2. Regular file `MBFile` blobs MUST include `ExtendedAttributes` carrying
     `{"com.apple.dataprotection.policy.exception-applied-by": b"com.apple.springboard"}`
     (binary plist bytes) and `ProtectionClass = 4`.
  3. Every parent directory in `relativePath` MUST have an explicit `flags=2`
     directory row in `Manifest.db` or `BackupAgent2` silently skips the child file.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import plistlib
from plistlib import UID
import sqlite3
import time
from typing import Any

from .mbdb import MODE_DIR, MODE_FILE, MODE_SYMLINK, compute_file_id

FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS Files (
    fileID TEXT PRIMARY KEY,
    domain TEXT,
    relativePath TEXT,
    flags INTEGER,
    file BLOB
);
CREATE TABLE IF NOT EXISTS Properties (
    key TEXT PRIMARY KEY,
    value BLOB
);
CREATE INDEX IF NOT EXISTS FilesDomainIdx ON Files(domain);
CREATE INDEX IF NOT EXISTS FilesRelativePathIdx ON Files(relativePath);
CREATE INDEX IF NOT EXISTS FilesFlagsIdx ON Files(flags);
"""

SPRINGBOARD_EXT_ATTR_BYTES: bytes = plistlib.dumps(
    {
        "com.apple.dataprotection.policy.exception-applied-by": (
            b"com.apple.springboard"
        )
    },
    fmt=plistlib.FMT_BINARY,
)


@dataclass
class ManifestDbEntry:
    domain: str
    relative_path: str
    data: bytes | None = None
    mode: int = MODE_FILE | 0o644
    uid: int = 501
    gid: int = 501
    mtime: int | None = None
    atime: int | None = None
    ctime: int | None = None
    flags: int = 1  # 1 = file, 2 = directory, 4 = symlink
    protection_class: int = 4
    link_target: str | None = None
    inode: int = 0
    include_extended_attributes: bool = True

    @property
    def file_id(self) -> str:
        return compute_file_id(self.domain, self.relative_path)


def build_mbfile_blob(entry: ManifestDbEntry, *, inode: int | None = None) -> bytes:
    """
    Construct the exact `NSKeyedArchiver` binary plist for an `MBFile` object
    stored in `Manifest.db.Files.file`, compliant with iOS 15 - 27+ `BackupAgent2`.
    """
    now = int(time.time())
    mtime = entry.mtime if entry.mtime is not None else now
    ctime = entry.ctime if entry.ctime is not None else now
    birth = entry.atime if entry.atime is not None else now
    size = len(entry.data) if (entry.data is not None and entry.flags == 1) else 0
    resolved_inode = inode if inode is not None else (entry.inode or 1)

    objects: list[Any] = [
        "$null",
        {},  # placeholder for root MBFile dict at index 1
        entry.relative_path,
    ]

    mbfile_dict: dict[str, Any] = {
        "Birth": birth,
        "Flags": 0,
        "GroupID": entry.gid,
        "InodeNumber": resolved_inode,
        "LastModified": mtime,
        "LastStatusChange": ctime,
        "Mode": entry.mode,
        "ProtectionClass": entry.protection_class,
        "RelativePath": UID(2),
        "Size": size,
        "UserID": entry.uid,
    }

    if entry.flags == 1 and entry.data is not None:
        digest = hashlib.sha1(entry.data).digest()
        digest_uid = len(objects)
        objects.append(digest)
        mbfile_dict["Digest"] = UID(digest_uid)

        if entry.include_extended_attributes:
            ext_uid = len(objects)
            objects.append(SPRINGBOARD_EXT_ATTR_BYTES)
            mbfile_dict["ExtendedAttributes"] = UID(ext_uid)

    if entry.flags == 4 and entry.link_target is not None:
        target_uid = len(objects)
        objects.append(entry.link_target)
        mbfile_dict["Target"] = UID(target_uid)

    class_uid = len(objects)
    objects.append(
        {
            "$classes": ["MBFile", "NSObject"],
            "$classname": "MBFile",
        }
    )
    mbfile_dict["$class"] = UID(class_uid)
    objects[1] = mbfile_dict

    keyed_archive = {
        "$archiver": "NSKeyedArchiver",
        "$objects": objects,
        "$top": {"root": UID(1)},
        "$version": 100000,
    }
    return plistlib.dumps(keyed_archive, fmt=plistlib.FMT_BINARY)


def write_manifest_db(
    db_path: str | Path,
    entries: list[ManifestDbEntry],
) -> None:
    """
    Create a fresh `Manifest.db` SQLite3 database from a list of `ManifestDbEntry`
    objects, automatically synthesizing parent directory rows and assigning strictly
    unique monotonic `InodeNumber` values for iOS 27 `BackupAgent2` compatibility.
    """
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()

    # Synthesize any missing parent directories so BackupAgent2 never skips child files
    existing_keys = {(e.domain, e.relative_path) for e in entries}
    synthesized: list[ManifestDbEntry] = []

    for entry in entries:
        rel = entry.relative_path.strip("/")
        if not rel:
            continue
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            parent_rel = "/".join(parts[:depth])
            key = (entry.domain, parent_rel)
            if key not in existing_keys:
                existing_keys.add(key)
                synthesized.append(
                    ManifestDbEntry(
                        domain=entry.domain,
                        relative_path=parent_rel,
                        data=None,
                        mode=MODE_DIR | 0o755,
                        uid=entry.uid,
                        gid=entry.gid,
                        flags=2,
                    )
                )

    all_entries = synthesized + list(entries)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(FILES_SCHEMA)
        next_inode = 1000
        rows = []
        for entry in all_entries:
            next_inode += 1
            entry_inode = entry.inode if entry.inode > 0 else next_inode
            if entry_inode >= next_inode:
                next_inode = entry_inode + 1
            rows.append(
                (
                    entry.file_id,
                    entry.domain,
                    entry.relative_path,
                    entry.flags,
                    build_mbfile_blob(entry, inode=entry_inode),
                )
            )
        conn.executemany(
            "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def read_manifest_db(db_path: str | Path) -> list[dict[str, Any]]:
    """
    Read all rows from a `Manifest.db` SQLite3 database and decode their `MBFile`
    keyed-archive metadata for inspection/testing.
    """
    conn = sqlite3.connect(str(db_path))
    results: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT fileID, domain, relativePath, flags, file FROM Files ORDER BY domain, relativePath"
        )
        for file_id, domain, rel_path, flags, file_blob in cur.fetchall():
            decoded_meta: dict[str, Any] = {}
            if file_blob:
                try:
                    archive = plistlib.loads(file_blob)
                    objs = archive.get("$objects", [])
                    if len(objs) > 1 and isinstance(objs[1], dict):
                        for k, v in objs[1].items():
                            if isinstance(v, UID):
                                decoded_meta[k] = objs[v.data]
                            else:
                                decoded_meta[k] = v
                except Exception:
                    pass
            results.append(
                {
                    "fileID": file_id,
                    "domain": domain,
                    "relativePath": rel_path,
                    "flags": flags,
                    "meta": decoded_meta,
                }
            )
    finally:
        conn.close()
    return results
