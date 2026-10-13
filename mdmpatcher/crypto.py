"""
Reverse-engineered obfuscation and RNCryptor v3 implementation for
MDMPatcher-Universal / MDMPatcher-Enhanced.

In MDMPatcher-Universal (ViewController.swift), three bundled files are obfuscated:
  - extension1.pdf          -> Info.plist template
  - extension2.pdf          -> Manifest.plist template
  - libiMobileeDevice.dylib -> ZIP archive containing MDMB/ backup directory

Obfuscation pipeline applied by the original tool:
  1. Plaintext bytes have 6 fixed index pairs swapped:
     (3, 5), (8, 17), (128, 345), (15, 65), (33, 133), (16, 64)
  2. Encrypted with RNCryptor v3 (AES-256-CBC, PBKDF2-HMAC-SHA1 10,000 iterations,
     HMAC-SHA256) using a password derived from IEEE-754 Double arithmetic in Swift:
       var i: Double = 4*2*4*6
       i = i * 7/5 + 23
       i = i - 546*5464564*64635645*4536454*462
       // i == -4.0418364248546965e+26
       "qepkwotkgpeqgpeokqgokgqoe\\(i)fdlgkdlgfklsdöfdgsj\\(i)gfdads23ji4jgi3vqewö"
         .replacingOccurrences(of: "q", with: "r")
  3. Ciphertext bytes have the same 6 fixed index pairs swapped again.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import hmac
import os
from pathlib import Path
from typing import Union

SWAP_PAIRS: tuple[tuple[int, int], ...] = (
    (3, 5),
    (8, 17),
    (128, 345),
    (15, 65),
    (33, 133),
    (16, 64),
)

MIN_SWAP_LENGTH = 346
RNCRYPTOR_V3_VERSION = 3
RNCRYPTOR_V3_OPTIONS_PASSWORD = 1
PBKDF2_ITERATIONS = 10000
PBKDF2_KEY_LEN = 32
RNCRYPTOR_HEADER_LEN = 34  # 1 (ver) + 1 (opt) + 8 (enc_salt) + 8 (hmac_salt) + 16 (iv)
RNCRYPTOR_HMAC_LEN = 32
RNCRYPTOR_MIN_LEN = RNCRYPTOR_HEADER_LEN + RNCRYPTOR_HMAC_LEN


def derive_mdmpatcher_password() -> bytes:
    """
    Reproduce the exact UTF-8 password bytes derived in MDMPatcher-Universal's
    ViewController.swift via IEEE-754 Double string interpolation.
    """
    i_swift_repr = "-4.0418364248546965e+26"
    raw = (
        f"qepkwotkgpeqgpeokqgokgqoe{i_swift_repr}"
        f"fdlgkdlgfklsdöfdgsj{i_swift_repr}"
        f"gfdads23ji4jgi3vqewö"
    ).replace("q", "r")
    return raw.encode("utf-8")


def apply_byte_swaps(data: Union[bytes, bytearray]) -> bytes:
    """
    Apply the 6 byte-pair swaps used before and after RNCryptor decryption
    in MDMPatcher-Universal. Because all 12 indices are distinct, this operation
    is an involution (applying it twice restores the original bytes).
    """
    if len(data) < MIN_SWAP_LENGTH:
        raise ValueError(
            f"Data length ({len(data)}) is smaller than minimum required for byte swaps ({MIN_SWAP_LENGTH})."
        )
    buf = bytearray(data)
    for idx_a, idx_b in SWAP_PAIRS:
        buf[idx_a], buf[idx_b] = buf[idx_b], buf[idx_a]
    return bytes(buf)


def _load_libcrypto() -> ctypes.CDLL:
    candidates = [
        "libcrypto.so.3",
        "/lib64/libcrypto.so.3",
        "/usr/lib64/libcrypto.so.3",
        "/lib/x86_64-linux-gnu/libcrypto.so.3",
        "/lib/aarch64-linux-gnu/libcrypto.so.3",
        "/usr/lib/libcrypto.so.3",
        "libcrypto.so.1.1",
        "/lib64/libcrypto.so.1.1",
        "/usr/lib64/libcrypto.so.1.1",
        "libcrypto.so",
        ctypes.util.find_library("crypto"),
    ]
    for name in candidates:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            return lib
        except OSError:
            continue
    raise RuntimeError("Could not load OpenSSL libcrypto shared library.")


_LIBCRYPTO: ctypes.CDLL | None = None


def _get_libcrypto() -> ctypes.CDLL:
    global _LIBCRYPTO
    if _LIBCRYPTO is None:
        lib = _load_libcrypto()
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_new.argtypes = []
        lib.EVP_CIPHER_CTX_free.restype = None
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_aes_256_cbc.restype = ctypes.c_void_p
        lib.EVP_aes_256_cbc.argtypes = []

        lib.EVP_DecryptInit_ex.restype = ctypes.c_int
        lib.EVP_DecryptInit_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.EVP_DecryptUpdate.restype = ctypes.c_int
        lib.EVP_DecryptUpdate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        lib.EVP_DecryptFinal_ex.restype = ctypes.c_int
        lib.EVP_DecryptFinal_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]

        lib.EVP_EncryptInit_ex.restype = ctypes.c_int
        lib.EVP_EncryptInit_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.EVP_EncryptUpdate.restype = ctypes.c_int
        lib.EVP_EncryptUpdate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        lib.EVP_EncryptFinal_ex.restype = ctypes.c_int
        lib.EVP_EncryptFinal_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        _LIBCRYPTO = lib
    return _LIBCRYPTO


def _aes_256_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    lib = _get_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_cbc(), None, key, iv) != 1:
            raise ValueError("EVP_DecryptInit_ex failed")
        out_buf = ctypes.create_string_buffer(len(ciphertext) + 32)
        out_len = ctypes.c_int(0)
        final_len = ctypes.c_int(0)
        if (
            lib.EVP_DecryptUpdate(
                ctx, out_buf, ctypes.byref(out_len), ciphertext, len(ciphertext)
            )
            != 1
        ):
            raise ValueError("EVP_DecryptUpdate failed")
        if (
            lib.EVP_DecryptFinal_ex(
                ctx, ctypes.byref(out_buf, out_len.value), ctypes.byref(final_len)
            )
            != 1
        ):
            raise ValueError("EVP_DecryptFinal_ex failed (bad key, IV, or padding)")
        return out_buf.raw[: out_len.value + final_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aes_256_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    lib = _get_libcrypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_256_cbc(), None, key, iv) != 1:
            raise ValueError("EVP_EncryptInit_ex failed")
        out_buf = ctypes.create_string_buffer(len(plaintext) + 32)
        out_len = ctypes.c_int(0)
        final_len = ctypes.c_int(0)
        if (
            lib.EVP_EncryptUpdate(
                ctx, out_buf, ctypes.byref(out_len), plaintext, len(plaintext)
            )
            != 1
        ):
            raise ValueError("EVP_EncryptUpdate failed")
        if (
            lib.EVP_EncryptFinal_ex(
                ctx, ctypes.byref(out_buf, out_len.value), ctypes.byref(final_len)
            )
            != 1
        ):
            raise ValueError("EVP_EncryptFinal_ex failed")
        return out_buf.raw[: out_len.value + final_len.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def decrypt_rncryptor_v3(data: bytes, password: Union[bytes, str, None] = None) -> bytes:
    """
    Decrypt an RNCryptor v3 payload with HMAC-SHA256 verification.
    """
    if len(data) < RNCRYPTOR_MIN_LEN:
        raise ValueError(f"RNCryptor payload too short ({len(data)} bytes)")
    version = data[0]
    options = data[1]
    if version != RNCRYPTOR_V3_VERSION:
        raise ValueError(f"Unsupported RNCryptor format version: {version} (expected 3)")
    if options != RNCRYPTOR_V3_OPTIONS_PASSWORD:
        raise ValueError(f"Unsupported RNCryptor options byte: {options} (expected 1)")

    if password is None:
        pw_bytes = derive_mdmpatcher_password()
    elif isinstance(password, str):
        pw_bytes = password.encode("utf-8")
    else:
        pw_bytes = password

    enc_salt = data[2:10]
    hmac_salt = data[10:18]
    iv = data[18:34]
    ciphertext = data[34:-32]
    stored_hmac = data[-32:]

    enc_key = hashlib.pbkdf2_hmac("sha1", pw_bytes, enc_salt, PBKDF2_ITERATIONS, PBKDF2_KEY_LEN)
    hmac_key = hashlib.pbkdf2_hmac("sha1", pw_bytes, hmac_salt, PBKDF2_ITERATIONS, PBKDF2_KEY_LEN)

    computed_hmac = hmac.new(hmac_key, data[:-32], hashlib.sha256).digest()
    if not hmac.compare_digest(computed_hmac, stored_hmac):
        raise ValueError("RNCryptor HMAC-SHA256 verification failed (invalid password or corrupted file)")

    return _aes_256_cbc_decrypt(ciphertext, enc_key, iv)


def encrypt_rncryptor_v3(plaintext: bytes, password: Union[bytes, str, None] = None) -> bytes:
    """
    Encrypt plaintext into RNCryptor v3 format.
    """
    if password is None:
        pw_bytes = derive_mdmpatcher_password()
    elif isinstance(password, str):
        pw_bytes = password.encode("utf-8")
    else:
        pw_bytes = password

    enc_salt = os.urandom(8)
    hmac_salt = os.urandom(8)
    iv = os.urandom(16)

    enc_key = hashlib.pbkdf2_hmac("sha1", pw_bytes, enc_salt, PBKDF2_ITERATIONS, PBKDF2_KEY_LEN)
    hmac_key = hashlib.pbkdf2_hmac("sha1", pw_bytes, hmac_salt, PBKDF2_ITERATIONS, PBKDF2_KEY_LEN)

    ciphertext = _aes_256_cbc_encrypt(plaintext, enc_key, iv)
    header_and_body = bytes([RNCRYPTOR_V3_VERSION, RNCRYPTOR_V3_OPTIONS_PASSWORD]) + enc_salt + hmac_salt + iv + ciphertext
    tag = hmac.new(hmac_key, header_and_body, hashlib.sha256).digest()
    return header_and_body + tag


def deobfuscate_payload(data: bytes, password: Union[bytes, str, None] = None) -> bytes:
    """
    Full deobfuscation pipeline matching MDMPatcher-Universal:
      1. Reverse outer byte swaps
      2. RNCryptor v3 decrypt
      3. Reverse inner byte swaps
    """
    swapped_outer = apply_byte_swaps(data)
    decrypted = decrypt_rncryptor_v3(swapped_outer, password=password)
    return apply_byte_swaps(decrypted)


def obfuscate_payload(plaintext: bytes, password: Union[bytes, str, None] = None) -> bytes:
    """
    Full obfuscation pipeline (exact inverse of deobfuscate_payload).
    """
    swapped_inner = apply_byte_swaps(plaintext)
    encrypted = encrypt_rncryptor_v3(swapped_inner, password=password)
    return apply_byte_swaps(encrypted)


def deobfuscate_assets(
    assets_dir: Union[str, Path],
    output_dir: Union[str, Path],
) -> dict[str, Path]:
    """
    Deobfuscate extension1.pdf, extension2.pdf, and libiMobileeDevice.dylib
    from assets_dir into clean_info.plist, clean_manifest.plist, and clean_archive.zip
    in output_dir.
    """
    assets_path = Path(assets_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    mapping = {
        "extension1.pdf": "clean_info.plist",
        "extension2.pdf": "clean_manifest.plist",
        "libiMobileeDevice.dylib": "clean_archive.zip",
    }
    results: dict[str, Path] = {}
    for src_name, dst_name in mapping.items():
        src_file = assets_path / src_name
        if not src_file.exists():
            raise FileNotFoundError(f"Missing obfuscated asset: {src_file}")
        plain = deobfuscate_payload(src_file.read_bytes())
        dst_file = out_path / dst_name
        dst_file.write_bytes(plain)
        results[dst_name] = dst_file
    return results
