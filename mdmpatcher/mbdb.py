"""
Parser, editor, and generator for Apple iOS Manifest.mbdb (v5.0 / Backup v2.4)
database files used by MobileBackup2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag
import hashlib
import struct
import time
from typing import Iterable, Optional

MBDB_HEADER = b"mbdb\x05\x00"


class FileMode(IntFlag):
    S_IFMT = 0o0170000
    S_IFIFO = 0o0010000
    S_IFCHR = 0o0020000
    S_IFDIR = 0o0040000
    S_IFBLK = 0o0060000
    S_IFREG = 0o0100000
    S_IFLNK = 0o0120000
    S_IFSOCK = 0o0140000

    S_IRUSR = 0o0000400
    S_IWUSR = 0o0000200
    S_IXUSR = 0o0000100
    S_IRGRP = 0o0000040
    S_IWGRP = 0o0000020
    S_IXGRP = 0o0000010
    S_IROTH = 0o0000004
    S_IWOTH = 0o0000002
    S_IXOTH = 0o0000001

    S_ISUID = 0o0004000
    S_ISGID = 0o0002000
    S_ISVTX = 0o0001000


MODE_DIR = int(FileMode.S_IFDIR)
MODE_FILE = int(FileMode.S_IFREG)
MODE_SYMLINK = int(FileMode.S_IFLNK)


DEFAULT_DIR_MODE = int(
    FileMode.S_IFDIR
    | FileMode.S_IRUSR
    | FileMode.S_IWUSR
    | FileMode.S_IXUSR
    | FileMode.S_IRGRP
    | FileMode.S_IXGRP
    | FileMode.S_IROTH
    | FileMode.S_IXOTH
)  # 0o40755

DEFAULT_FILE_MODE = int(
    FileMode.S_IFREG
    | FileMode.S_IRUSR
    | FileMode.S_IWUSR
    | FileMode.S_IRGRP
    | FileMode.S_IROTH
)  # 0o100644


def compute_file_id(domain: str, relative_path: str) -> str:
    """
    Compute the 40-char SHA-1 hex filename used in iOS backups:
    SHA1("<domain>-<relativePath>").
    """
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def _read_mbdb_bytes(data: bytes, offset: int) -> tuple[Optional[bytes], int]:
    if offset + 2 > len(data):
        raise ValueError("Truncated mbdb string length")
    (length,) = struct.unpack(">H", data[offset : offset + 2])
    offset += 2
    if length == 0xFFFF:
        return None, offset
    if offset + length > len(data):
        raise ValueError("Truncated mbdb string payload")
    return data[offset : offset + length], offset + length


def _write_mbdb_bytes(val: Optional[bytes]) -> bytes:
    if val is None:
        return b"\xFF\xFF"
    return struct.pack(">H", len(val)) + val


@dataclass
class MbdbRecord:
    domain: str
    filename: str
    linktarget: Optional[str] = None
    datahash: Optional[bytes] = None
    enckey: Optional[bytes] = None
    mode: int = DEFAULT_FILE_MODE
    inode: int = 0
    uid: int = 501
    gid: int = 501
    mtime: int = field(default_factory=lambda: int(time.time()))
    atime: int = field(default_factory=lambda: int(time.time()))
    ctime: int = field(default_factory=lambda: int(time.time()))
    filelen: int = 0
    flag: int = 4
    properties: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def file_id(self) -> str:
        return compute_file_id(self.domain, self.filename)

    @property
    def is_dir(self) -> bool:
        return (self.mode & FileMode.S_IFMT) == FileMode.S_IFDIR

    @property
    def is_file(self) -> bool:
        return (self.mode & FileMode.S_IFMT) == FileMode.S_IFREG

    @property
    def is_symlink(self) -> bool:
        return (self.mode & FileMode.S_IFMT) == FileMode.S_IFLNK

    def to_bytes(self) -> bytes:
        parts = [
            _write_mbdb_bytes(self.domain.encode("utf-8")),
            _write_mbdb_bytes(self.filename.encode("utf-8")),
            _write_mbdb_bytes(
                self.linktarget.encode("utf-8") if self.linktarget is not None else None
            ),
            _write_mbdb_bytes(self.datahash),
            _write_mbdb_bytes(self.enckey),
            struct.pack(
                ">HQI3IiqBB",
                self.mode & 0xFFFF,
                self.inode & 0xFFFFFFFFFFFFFFFF,
                self.uid & 0xFFFFFFFF,
                self.gid & 0xFFFFFFFF,
                self.mtime & 0xFFFFFFFF,
                self.atime & 0xFFFFFFFF,
                self.ctime & 0xFFFFFFFF,
                self.filelen,
                self.flag & 0xFF,
                len(self.properties) & 0xFF,
            ),
        ]
        for prop_name, prop_val in self.properties:
            parts.append(_write_mbdb_bytes(prop_name.encode("utf-8")))
            parts.append(_write_mbdb_bytes(prop_val))
        return b"".join(parts)

    @classmethod
    def make_dir(
        cls,
        domain: str,
        filename: str = "",
        mode: int = DEFAULT_DIR_MODE,
        uid: int = 501,
        gid: int = 501,
        inode: int = 0,
        timestamp: Optional[int] = None,
    ) -> "MbdbRecord":
        ts = timestamp if timestamp is not None else int(time.time())
        if (mode & FileMode.S_IFMT) == 0:
            mode |= FileMode.S_IFDIR
        return cls(
            domain=domain,
            filename=filename,
            linktarget=None,
            datahash=None,
            enckey=None,
            mode=mode,
            inode=inode,
            uid=uid,
            gid=gid,
            mtime=ts,
            atime=ts,
            ctime=ts,
            filelen=0,
            flag=4,
            properties=[],
        )

    @classmethod
    def make_file(
        cls,
        domain: str,
        filename: str,
        contents: bytes,
        mode: int = DEFAULT_FILE_MODE,
        uid: int = 501,
        gid: int = 501,
        inode: int = 1000,
        flag: int = 4,
        timestamp: Optional[int] = None,
    ) -> "MbdbRecord":
        ts = timestamp if timestamp is not None else int(time.time())
        if (mode & FileMode.S_IFMT) == 0:
            mode |= FileMode.S_IFREG
        return cls(
            domain=domain,
            filename=filename,
            linktarget=None,
            datahash=hashlib.sha1(contents).digest(),
            enckey=None,
            mode=mode,
            inode=inode,
            uid=uid,
            gid=gid,
            mtime=ts,
            atime=ts,
            ctime=ts,
            filelen=len(contents),
            flag=flag,
            properties=[],
        )


class Mbdb:
    def __init__(self, records: Optional[Iterable[MbdbRecord]] = None):
        self.records: list[MbdbRecord] = list(records) if records else []

    @classmethod
    def from_bytes(cls, data: bytes) -> "Mbdb":
        if not data.startswith(MBDB_HEADER):
            raise ValueError(f"Invalid Manifest.mbdb header: {data[:6]!r}")
        pos = len(MBDB_HEADER)
        records: list[MbdbRecord] = []
        while pos < len(data):
            domain_b, pos = _read_mbdb_bytes(data, pos)
            filename_b, pos = _read_mbdb_bytes(data, pos)
            linktarget_b, pos = _read_mbdb_bytes(data, pos)
            datahash_b, pos = _read_mbdb_bytes(data, pos)
            enckey_b, pos = _read_mbdb_bytes(data, pos)
            if pos + 40 > len(data):
                raise ValueError("Truncated mbdb fixed header")
            (
                mode,
                inode,
                uid,
                gid,
                mtime,
                atime,
                ctime,
                filelen,
                flag,
                numprops,
            ) = struct.unpack(">HQI3IiqBB", data[pos : pos + 40])
            pos += 40
            props: list[tuple[str, bytes]] = []
            for _ in range(numprops):
                pname_b, pos = _read_mbdb_bytes(data, pos)
                pval_b, pos = _read_mbdb_bytes(data, pos)
                props.append(
                    (
                        (pname_b or b"").decode("utf-8", errors="replace"),
                        pval_b or b"",
                    )
                )
            records.append(
                MbdbRecord(
                    domain=(domain_b or b"").decode("utf-8", errors="replace"),
                    filename=(filename_b or b"").decode("utf-8", errors="replace"),
                    linktarget=linktarget_b.decode("utf-8", errors="replace")
                    if linktarget_b is not None
                    else None,
                    datahash=datahash_b,
                    enckey=enckey_b,
                    mode=mode,
                    inode=inode,
                    uid=uid,
                    gid=gid,
                    mtime=mtime,
                    atime=atime,
                    ctime=ctime,
                    filelen=filelen,
                    flag=flag,
                    properties=props,
                )
            )
        return cls(records)

    def to_bytes(self) -> bytes:
        return MBDB_HEADER + b"".join(r.to_bytes() for r in self.records)

    def find_record(self, domain: str, filename: str) -> Optional[MbdbRecord]:
        for rec in self.records:
            if rec.domain == domain and rec.filename == filename:
                return rec
        return None

    def ensure_parent_dirs(
        self,
        domain: str,
        filename: str,
        uid: int = 501,
        gid: int = 501,
        timestamp: Optional[int] = None,
    ) -> None:
        """
        Ensure the domain root record (filename="") and all intermediate parent
        directory records exist before adding a file record.
        """
        if self.find_record(domain, "") is None:
            self.records.append(
                MbdbRecord.make_dir(domain=domain, filename="", uid=uid, gid=gid, timestamp=timestamp)
            )
        parts = [p for p in filename.split("/") if p]
        for i in range(1, len(parts)):
            dir_path = "/".join(parts[:i])
            if self.find_record(domain, dir_path) is None:
                self.records.append(
                    MbdbRecord.make_dir(
                        domain=domain,
                        filename=dir_path,
                        uid=uid,
                        gid=gid,
                        timestamp=timestamp,
                    )
                )

    def upsert_file(
        self,
        domain: str,
        filename: str,
        contents: bytes,
        mode: int = DEFAULT_FILE_MODE,
        uid: int = 501,
        gid: int = 501,
        flag: int = 4,
        timestamp: Optional[int] = None,
    ) -> MbdbRecord:
        """
        Update an existing file record in-place (recomputing SHA-1 datahash and length)
        or insert it along with any missing parent directory records.
        """
        existing = self.find_record(domain, filename)
        if existing is not None:
            existing.datahash = hashlib.sha1(contents).digest()
            existing.filelen = len(contents)
            if timestamp is not None:
                existing.mtime = timestamp
                existing.atime = timestamp
                existing.ctime = timestamp
            return existing

        self.ensure_parent_dirs(domain, filename, uid=uid, gid=gid, timestamp=timestamp)
        next_inode = max((r.inode for r in self.records), default=50000) + 1
        rec = MbdbRecord.make_file(
            domain=domain,
            filename=filename,
            contents=contents,
            mode=mode,
            uid=uid,
            gid=gid,
            inode=next_inode,
            flag=flag,
            timestamp=timestamp,
        )
        self.records.append(rec)
        return rec
