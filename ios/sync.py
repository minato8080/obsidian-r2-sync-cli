#!/usr/bin/env python3
"""Dependency-free R2 sync client for iOS/a-Shell.

The command accepts a JSON configuration and may keep its checkpoint next to
that configuration inside the vault.  The checkpoint is always protected from
Vault scanning and remote synchronization.  It supports the small explicit-
file Probe as well as a full scan for development use.  Full sync applies
PUSH/PULL/merge with ``--apply`` and applies deletes only when
``--allow-delete`` is also supplied.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as _datetime
import difflib
import functools
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


DEFAULT_SALT = bytes.fromhex("a80df43a8fbd0308a7cab83e581f86b1")
MAGIC = b"RCLONE\x00\x00"
BLOCK_DATA_SIZE = 64 * 1024
BLOCK_TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + 24


def _emit_progress(progress, message: str) -> None:
    if progress is not None:
        progress(message)


def _report_action_plan(progress, actions: list[dict]) -> None:
    grouped = {}
    for action in actions:
        grouped.setdefault(action["type"], []).append(action)
    for action_type, items in grouped.items():
        _emit_progress(progress, f"\n[{action_type}] {len(items)}件")
        for action in items[:50]:
            reason = f" ({action['reason']})" if action.get("reason") else ""
            _emit_progress(progress, f"  {action['path']}{reason}")
        if len(items) > 50:
            _emit_progress(progress, f"  ...ほか{len(items) - 50}件")


def _report_result(progress, result: dict) -> None:
    if result.get("mode") == "dry-run" and result.get("ok"):
        _emit_progress(progress, "\ndry-runのため実際の変更は行っていません。--apply を付けて実行してください。")
    _emit_progress(progress, "\n=== 実行結果 ===")
    _emit_progress(progress, f"ok: {'true' if result.get('ok') else 'false'}")
    for name in (
        "planned", "unchanged", "validated", "applied", "seeded", "ignoredRemoteDeletes",
        "mergeBasesPruned", "mergeBasesPendingPrune",
    ):
        if name in result:
            _emit_progress(progress, f"{name}: {result[name]}")
    for action_type, count in result.get("appliedByType", {}).items():
        _emit_progress(progress, f"{action_type}: {count}")
    for action_type, count in result.get("skippedByType", {}).items():
        _emit_progress(progress, f"SKIPPED_{action_type}: {count}")
    conflicts = result.get("conflicts", [])
    if conflicts:
        _emit_progress(progress, f"\n競合: {len(conflicts)}件")
        for conflict in conflicts[:50]:
            _emit_progress(progress, f"  {conflict.get('path', '<unknown>')}: {conflict.get('reason', 'conflict')}")
    errors = result.get("errors", [])
    if errors:
        _emit_progress(progress, f"\nエラー: {len(errors)}件")
        for error in errors[:50]:
            _emit_progress(progress, f"  {error.get('path', '<unknown>')}: {error.get('error', 'error')}")


class PullError(Exception):
    """An expected, user-facing pull failure."""


class ParallelInterrupted(KeyboardInterrupt):
    """Ctrl+C received while worker threads may still be blocked."""


def _gf_mul(a: int, b: int) -> int:
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        a = ((a << 1) ^ 0x11B) if a & 0x80 else a << 1
        b >>= 1
    return out & 0xFF


def _rotl8(value: int, count: int) -> int:
    return ((value << count) | (value >> (8 - count))) & 0xFF


def _make_sbox() -> tuple[int, ...]:
    values = []
    for x in range(256):
        inverse = 0
        if x:
            inverse = 1
            exponent = 254
            base = x
            while exponent:
                if exponent & 1:
                    inverse = _gf_mul(inverse, base)
                base = _gf_mul(base, base)
                exponent >>= 1
        values.append(
            inverse
            ^ _rotl8(inverse, 1)
            ^ _rotl8(inverse, 2)
            ^ _rotl8(inverse, 3)
            ^ _rotl8(inverse, 4)
            ^ 0x63
        )
    return tuple(values)


SBOX = _make_sbox()
INV_SBOX = tuple(SBOX.index(value) for value in range(256))
GF_MUL_2 = tuple(_gf_mul(value, 2) for value in range(256))
GF_MUL_3 = tuple(_gf_mul(value, 3) for value in range(256))
GF_MUL_9 = tuple(_gf_mul(value, 9) for value in range(256))
GF_MUL_11 = tuple(_gf_mul(value, 11) for value in range(256))
GF_MUL_13 = tuple(_gf_mul(value, 13) for value in range(256))
GF_MUL_14 = tuple(_gf_mul(value, 14) for value in range(256))


@functools.lru_cache(maxsize=8)
def _aes_round_keys(key: bytes) -> tuple[bytes, ...]:
    if len(key) not in (16, 24, 32):
        raise PullError("AES key length is invalid")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[i : i + 4]) for i in range(0, len(key), 4)]
    rcon = 1
    for i in range(nk, 4 * (rounds + 1)):
        word = words[i - 1][:]
        if i % nk == 0:
            word = word[1:] + word[:1]
            word = [SBOX[x] for x in word]
            word[0] ^= rcon
            rcon = _gf_mul(rcon, 2)
        elif nk > 6 and i % nk == 4:
            word = [SBOX[x] for x in word]
        words.append([a ^ b for a, b in zip(words[i - nk], word)])
    return tuple(bytes(sum(words[i : i + 4], [])) for i in range(0, len(words), 4))


def _add_key(state: list[int], key: bytes) -> None:
    for i, value in enumerate(key):
        state[i] ^= value


def _sub_bytes(state: list[int], inverse: bool = False) -> None:
    table = INV_SBOX if inverse else SBOX
    for i, value in enumerate(state):
        state[i] = table[value]


def _shift_rows(state: list[int], inverse: bool = False) -> None:
    old = state[:]
    for row in range(4):
        for col in range(4):
            source_col = (col + row if not inverse else col - row) % 4
            state[4 * col + row] = old[4 * source_col + row]


def _mix_columns(state: list[int], inverse: bool = False) -> None:
    for col in range(4):
        i = 4 * col
        a0, a1, a2, a3 = state[i : i + 4]
        if inverse:
            state[i : i + 4] = [
                GF_MUL_14[a0] ^ GF_MUL_11[a1] ^ GF_MUL_13[a2] ^ GF_MUL_9[a3],
                GF_MUL_9[a0] ^ GF_MUL_14[a1] ^ GF_MUL_11[a2] ^ GF_MUL_13[a3],
                GF_MUL_13[a0] ^ GF_MUL_9[a1] ^ GF_MUL_14[a2] ^ GF_MUL_11[a3],
                GF_MUL_11[a0] ^ GF_MUL_13[a1] ^ GF_MUL_9[a2] ^ GF_MUL_14[a3],
            ]
        else:
            state[i : i + 4] = [
                GF_MUL_2[a0] ^ GF_MUL_3[a1] ^ a2 ^ a3,
                a0 ^ GF_MUL_2[a1] ^ GF_MUL_3[a2] ^ a3,
                a0 ^ a1 ^ GF_MUL_2[a2] ^ GF_MUL_3[a3],
                GF_MUL_3[a0] ^ a1 ^ a2 ^ GF_MUL_2[a3],
            ]


def _aes_block(block: bytes, key: bytes, decrypt: bool = False) -> bytes:
    if len(block) != 16:
        raise PullError("AES input must be one block")
    keys = _aes_round_keys(key)
    state = list(block)
    if not decrypt:
        _add_key(state, keys[0])
        for round_key in keys[1:-1]:
            _sub_bytes(state)
            _shift_rows(state)
            _mix_columns(state)
            _add_key(state, round_key)
        _sub_bytes(state)
        _shift_rows(state)
        _add_key(state, keys[-1])
    else:
        _add_key(state, keys[-1])
        for round_key in reversed(keys[1:-1]):
            _shift_rows(state, inverse=True)
            _sub_bytes(state, inverse=True)
            _add_key(state, round_key)
            _mix_columns(state, inverse=True)
        _shift_rows(state, inverse=True)
        _sub_bytes(state, inverse=True)
        _add_key(state, keys[0])
    return bytes(state)


def _double_block(value: bytes) -> bytes:
    out = bytearray(16)
    carry = 0
    for i, current in enumerate(value):
        out[i] = ((current << 1) & 0xFF) | carry
        carry = 1 if current & 0x80 else 0
    out[0] ^= 0x87 if carry else 0
    return bytes(out)


def _eme_transform(data: bytes, tweak: bytes, key: bytes, decrypt: bool) -> bytes:
    if len(tweak) != 16 or not data or len(data) % 16:
        raise PullError("invalid EME input")
    blocks = [data[i : i + 16] for i in range(0, len(data), 16)]
    l_table = []
    current = _double_block(_aes_block(b"\x00" * 16, key))
    for _ in blocks:
        l_table.append(current)
        current = _double_block(current)
    transformed = []
    for block, l_value in zip(blocks, l_table):
        mixed = bytes(a ^ b for a, b in zip(block, l_value))
        transformed.append(_aes_block(mixed, key, decrypt))
    mp = bytearray(tweak)
    for block in transformed:
        for i, value in enumerate(block):
            mp[i] ^= value
    mc = _aes_block(bytes(mp), key, decrypt)
    mask = bytes(a ^ b for a, b in zip(mp, mc))
    for i in range(1, len(transformed)):
        mask = _double_block(mask)
        transformed[i] = bytes(a ^ b for a, b in zip(transformed[i], mask))
    first = bytearray(mc)
    for i, value in enumerate(tweak):
        first[i] ^= value
    for block in transformed[1:]:
        for i, value in enumerate(block):
            first[i] ^= value
    transformed[0] = bytes(first)
    return b"".join(
        bytes(a ^ b for a, b in zip(_aes_block(block, key, decrypt), l_value))
        for block, l_value in zip(transformed, l_table)
    )


def _salsa_round(state: list[int], add_original: bool = True) -> list[int]:
    x = state[:]

    def rotate(value: int, count: int) -> int:
        value &= 0xFFFFFFFF
        return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF

    def mix(index: int, left: int, right: int, count: int) -> None:
        x[index] = (x[index] ^ rotate((x[left] + x[right]) & 0xFFFFFFFF, count)) & 0xFFFFFFFF

    for _ in range(10):
        mix(4, 0, 12, 7)
        mix(8, 4, 0, 9)
        mix(12, 8, 4, 13)
        mix(0, 12, 8, 18)
        mix(9, 5, 1, 7)
        mix(13, 9, 5, 9)
        mix(1, 13, 9, 13)
        mix(5, 1, 13, 18)
        mix(14, 10, 6, 7)
        mix(2, 14, 10, 9)
        mix(6, 2, 14, 13)
        mix(10, 6, 2, 18)
        mix(3, 15, 11, 7)
        mix(7, 3, 15, 9)
        mix(11, 7, 3, 13)
        mix(15, 11, 7, 18)
        mix(1, 0, 3, 7)
        mix(2, 1, 0, 9)
        mix(3, 2, 1, 13)
        mix(0, 3, 2, 18)
        mix(6, 5, 4, 7)
        mix(7, 6, 5, 9)
        mix(4, 7, 6, 13)
        mix(5, 4, 7, 18)
        mix(11, 10, 9, 7)
        mix(8, 11, 10, 9)
        mix(9, 8, 11, 13)
        mix(10, 9, 8, 18)
        mix(12, 15, 14, 7)
        mix(13, 12, 15, 9)
        mix(14, 13, 12, 13)
        mix(15, 14, 13, 18)
    if add_original:
        return [(a + b) & 0xFFFFFFFF for a, b in zip(x, state)]
    return x


def _words(data: bytes) -> list[int]:
    return [int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4)]


def _salsa_block(key: bytes, nonce8: bytes, counter: int) -> bytes:
    constants = _words(b"expand 32-byte k")
    k = _words(key)
    n = _words(nonce8)
    state = [constants[0], k[0], k[1], k[2], k[3], constants[1], n[0], n[1], counter & 0xFFFFFFFF, counter >> 32, constants[2], k[4], k[5], k[6], k[7], constants[3]]
    return b"".join(value.to_bytes(4, "little") for value in _salsa_round(state))


def _hsalsa(key: bytes, nonce16: bytes) -> bytes:
    constants = _words(b"expand 32-byte k")
    k = _words(key)
    n = _words(nonce16)
    state = [constants[0], k[0], k[1], k[2], k[3], constants[1], n[0], n[1], n[2], n[3], constants[2], k[4], k[5], k[6], k[7], constants[3]]
    x = _salsa_round(state, add_original=False)
    return b"".join(x[i].to_bytes(4, "little") for i in (0, 5, 10, 15, 6, 7, 8, 9))


def _xsalsa_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    if len(key) != 32 or len(nonce) != 24:
        raise PullError("invalid XSalsa20 key or nonce")
    subkey = _hsalsa(key, nonce[:16])
    result = bytearray()
    counter = 0
    while len(result) < length:
        result.extend(_salsa_block(subkey, nonce[16:], counter))
        counter += 1
    return bytes(result[:length])


def _poly1305(message: bytes, key: bytes) -> bytes:
    if len(key) != 32:
        raise PullError("invalid Poly1305 key")
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    acc = 0
    modulus = (1 << 130) - 5
    for offset in range(0, len(message), 16):
        block = message[offset : offset + 16]
        acc = ((acc + int.from_bytes(block + b"\x01", "little")) * r) % modulus
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


def _secretbox_decrypt(data: bytes, key: bytes, nonce: bytes) -> bytes:
    if len(data) < 16:
        raise PullError("encrypted block is too short")
    # noble's xsalsa20poly1305 (and NaCl secretbox) serializes tag first.
    tag, ciphertext = data[:16], data[16:]
    stream = _xsalsa_stream(key, nonce, len(ciphertext) + 32)
    expected = _poly1305(ciphertext, stream[:32])
    if not hmac.compare_digest(tag, expected):
        raise PullError("encrypted block authentication failed")
    return bytes(a ^ b for a, b in zip(ciphertext, stream[32:]))


def _secretbox_encrypt(plain: bytes, key: bytes, nonce: bytes) -> bytes:
    if len(key) != 32 or len(nonce) != 24:
        raise PullError("invalid XSalsa20 key or nonce")
    stream = _xsalsa_stream(key, nonce, len(plain) + 32)
    ciphertext = bytes(a ^ b for a, b in zip(plain, stream[32:]))
    return _poly1305(ciphertext, stream[:32]) + ciphertext


class RcloneBase64:
    """rclone-base64 filename and content decoder used by Remotely Save."""

    def __init__(self, password: str):
        if not isinstance(password, str):
            raise PullError("password must be text")
        key = b"\x00" * 80 if password == "" else hashlib.scrypt(
            password.encode("utf-8"), salt=DEFAULT_SALT, n=16384, r=8, p=1, dklen=80
        )
        self.data_key = key[:32]
        self.name_key = key[32:64]
        self.name_tweak = key[64:80]
        self._decrypted_name_cache = {}
        self._encrypted_name_cache = {}

    @staticmethod
    def _cache_name(cache: dict[str, str], key: str, value: str) -> str:
        # A full Vault comfortably fits while still bounding memory for an
        # unexpectedly large or hostile remote listing.
        if len(cache) >= 16_384:
            cache.clear()
        cache[key] = value
        return value

    def decrypt_path(self, encrypted: str) -> str:
        parts = encrypted.split("/")
        result = []
        for part in parts:
            cached = self._decrypted_name_cache.get(part)
            if cached is not None:
                result.append(cached)
                continue
            raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            if not raw or len(raw) % 16:
                raise PullError("invalid encrypted filename")
            padded = _eme_transform(raw, self.name_tweak, self.name_key, True)
            pad_size = padded[-1]
            if not 1 <= pad_size <= 16 or padded[-pad_size:] != bytes([pad_size]) * pad_size:
                raise PullError("invalid encrypted filename padding")
            plain_part = padded[:-pad_size].decode("utf-8")
            result.append(self._cache_name(self._decrypted_name_cache, part, plain_part))
        return "/".join(result)

    def encrypt_path(self, plain: str) -> str:
        result = []
        for part in plain.replace("\\", "/").split("/"):
            cached = self._encrypted_name_cache.get(part)
            if cached is not None:
                result.append(cached)
                continue
            padded_size = 16 - (len(part.encode("utf-8")) % 16)
            padded = part.encode("utf-8") + bytes([padded_size]) * padded_size
            encrypted = _eme_transform(padded, self.name_tweak, self.name_key, False)
            encrypted_part = base64.urlsafe_b64encode(encrypted).decode("ascii").rstrip("=")
            result.append(self._cache_name(self._encrypted_name_cache, part, encrypted_part))
        return "/".join(result)

    def decrypt_content(self, data: bytes) -> bytes:
        if len(data) < HEADER_SIZE or data[: len(MAGIC)] != MAGIC:
            raise PullError("not an rclone encrypted file")
        nonce = bytearray(data[len(MAGIC) : HEADER_SIZE])
        payload = data[HEADER_SIZE:]
        output = bytearray()
        offset = 0
        while offset < len(payload):
            block_size = min(BLOCK_DATA_SIZE + BLOCK_TAG_SIZE, len(payload) - offset)
            if block_size <= BLOCK_TAG_SIZE:
                raise PullError("truncated encrypted block")
            encrypted_block = payload[offset : offset + block_size]
            output.extend(_secretbox_decrypt(bytes(encrypted_block), self.data_key, bytes(nonce)))
            offset += block_size
            for i in range(24):
                nonce[i] = (nonce[i] + 1) & 0xFF
                if nonce[i]:
                    break
        return bytes(output)

    def encrypt_content(self, data: bytes) -> bytes:
        if not isinstance(data, bytes):
            data = bytes(data)
        nonce = bytearray(os.urandom(24))
        output = bytearray(MAGIC + nonce)
        for offset in range(0, len(data), BLOCK_DATA_SIZE):
            block = data[offset : offset + BLOCK_DATA_SIZE]
            output.extend(_secretbox_encrypt(block, self.data_key, bytes(nonce)))
            for index in range(24):
                nonce[index] = (nonce[index] + 1) & 0xFF
                if nonce[index]:
                    break
        return bytes(output)


class PlainContent:
    def decrypt_content(self, data: bytes) -> bytes:
        return data

    def encrypt_content(self, data: bytes) -> bytes:
        return data


class RemoteObject:
    def __init__(self, data: bytes, etag: str | None = None, metadata: dict[str, str] | None = None):
        self.data = data
        self.etag = etag
        self.metadata = {str(k).lower(): str(v) for k, v in (metadata or {}).items()}


class R2Client:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, timeout: float = 30):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PullError("endpoint must be an http(s) URL")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"", extra_headers: dict[str, str] | None = None) -> tuple[bytes, object]:
        now = _datetime.datetime.now(_datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        parsed = urllib.parse.urlsplit(self.endpoint)
        path_parts = (self.bucket, *key.split("/")) if key else (self.bucket,)
        encoded_path = "/" + "/".join(urllib.parse.quote(part, safe="~") for part in path_parts)
        query = query or {}
        canonical_query = "&".join(
            f"{urllib.parse.quote(str(name), safe='~')}={urllib.parse.quote(str(value), safe='~')}"
            for name, value in sorted(query.items())
        )
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + encoded_path, canonical_query, ""))
        host = parsed.netloc
        payload_hash = "UNSIGNED-PAYLOAD"
        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date, **(extra_headers or {})}
        canonical_headers = "".join(f"{name}:{' '.join(value.strip().split())}\n" for name, value in sorted(headers.items()))
        signed_headers = ";".join(sorted(headers))
        canonical_request = f"{method}\n{encoded_path}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{date}/auto/s3/aws4_request"
        hashed_request = hashlib.sha256(canonical_request.encode()).hexdigest()
        to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashed_request}"
        date_key = hmac.new(("AWS4" + self.secret_key).encode(), date.encode(), hashlib.sha256).digest()
        region_key = hmac.new(date_key, b"auto", hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        try:
            request = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response
        except urllib.error.HTTPError as error:
            raise PullError(f"R2 {method} failed ({error.code})") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise PullError(f"R2 {method} failed (network error)") from error

    def get_object(self, key: str) -> RemoteObject:
        body, response = self._request("GET", key)
        metadata = {k.lower(): v for k, v in response.headers.items() if k.lower().startswith("x-amz-meta-")}
        metadata = {k.removeprefix("x-amz-meta-"): v for k, v in metadata.items()}
        return RemoteObject(body, response.headers.get("ETag"), metadata)

    def put_object(self, key: str, data: bytes, mtime_ms: float) -> str | None:
        _, response = self._request(
            "PUT", key, body=data,
            extra_headers={"x-amz-meta-mtime": str(mtime_ms / 1000)},
        )
        return response.headers.get("ETag")

    def delete_object(self, key: str) -> None:
        self._request("DELETE", key)

    @staticmethod
    def _xml_text(parent: ET.Element, name: str) -> str | None:
        for child in parent.iter():
            if child.tag.rsplit("}", 1)[-1] == name:
                return child.text
        return None

    def list_all(self, prefix: str = "") -> list[dict]:
        """List all objects below ``prefix`` using S3 ListObjectsV2."""
        items = []
        token = None
        while True:
            query = {"list-type": "2"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            body, _ = self._request("GET", "", query)
            try:
                root = ET.fromstring(body)
            except ET.ParseError as error:
                raise PullError("R2 LIST returned invalid XML") from error
            for contents in root.iter():
                if contents.tag.rsplit("}", 1)[-1] != "Contents":
                    continue
                item = {
                    "key": self._xml_text(contents, "Key"),
                    "etag": self._xml_text(contents, "ETag"),
                    "size": self._xml_text(contents, "Size"),
                    "lastModified": self._xml_text(contents, "LastModified"),
                }
                if not isinstance(item["key"], str) or not item["key"]:
                    raise PullError("R2 LIST returned an object without a key")
                try:
                    item["size"] = int(item["size"] or 0)
                except ValueError as error:
                    raise PullError("R2 LIST returned an invalid object size") from error
                items.append(item)
            truncated = self._xml_text(root, "IsTruncated") == "true"
            token = self._xml_text(root, "NextContinuationToken") if truncated else None
            if truncated and not token:
                raise PullError("R2 LIST is truncated but has no continuation token")
            if not token:
                return items


def _run_parallel(items: list, worker, concurrency: int, on_progress=None) -> list[tuple[object, concurrent.futures.Future]]:
    """Run read-only work and return futures in input order.

    ThreadPoolExecutor's context manager waits for every worker while unwinding.
    That makes Ctrl+C appear ineffective when a network worker is blocked.  On
    interruption, cancel work that has not started and deliberately skip that
    wait; the CLI entry point then exits without running Python's thread join.
    """
    if not items:
        return []
    worker_count = min(concurrency, len(items))
    if worker_count == 1:
        completed = []
        for index, item in enumerate(items, start=1):
            future = concurrent.futures.Future()
            try:
                future.set_result(worker(item))
            except Exception as error:
                future.set_exception(error)
            completed.append((item, future))
            if on_progress:
                on_progress(index, len(items))
        return completed

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    future_items = {pool.submit(worker, item): (index, item) for index, item in enumerate(items)}
    completed = [None] * len(items)
    try:
        for done, future in enumerate(concurrent.futures.as_completed(future_items), start=1):
            index, item = future_items[future]
            completed[index] = (item, future)
            if on_progress:
                on_progress(done, len(items))
    except KeyboardInterrupt as error:
        for future in future_items:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise ParallelInterrupted() from error
    except BaseException:
        for future in future_items:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return completed


def _report_fetch_progress(progress, done: int, total: int) -> None:
    if total <= 10 or done % 50 == 0 or done == total:
        _emit_progress(progress, f"  取得・検証中: {done}/{total}")


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PullError(f"unsafe relative path: {value!r}")
    return "/".join(path.parts)


def _vault_path(vault: Path, rel_path: str) -> Path:
    target = (vault / Path(*rel_path.split("/"))).resolve()
    try:
        target.relative_to(vault.resolve())
    except ValueError as error:
        raise PullError(f"path escapes vault: {rel_path!r}") from error
    return target


def _state_rel_path(vault: Path, state_path: Path) -> str | None:
    try:
        return state_path.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        return None


def load_state(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("entries", {}), dict):
        raise PullError("state file has an invalid format")
    return value["entries"]


def save_state(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"version": 1, "updatedAt": _datetime.datetime.now(_datetime.timezone.utc).isoformat(), "entries": entries}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_replace(target: Path, data: bytes, mtime_ms: float) -> os.stat_result:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".r2-sync-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            timestamp_ns = int(mtime_ms * 1_000_000)
            os.utime(temp_name, ns=(timestamp_ns, timestamp_ns))
        os.replace(temp_name, target)
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass
        return target.stat()
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _mtime_ms(metadata: dict[str, str]) -> float:
    raw = metadata.get("mtime") or metadata.get("mmtime") or metadata.get("mtime-ms")
    if raw is None:
        return time.time() * 1000
    try:
        value = float(raw)
    except ValueError as error:
        raise PullError("remote mtime metadata is invalid") from error
    if not math.isfinite(value):
        raise PullError("remote mtime metadata is invalid")
    return value if abs(value) > 100_000_000_000 else value * 1000


def _local_conflict(vault: Path, rel_path: str, previous: dict | None) -> str | None:
    target = _vault_path(vault, rel_path)
    if not target.exists():
        return None
    if not previous:
        return "target exists without checkpoint"
    stat = target.stat()
    if previous.get("localSize") is not None and stat.st_size != previous["localSize"]:
        return "target size changed since checkpoint"
    if previous.get("localMtimeMs") is not None and abs(stat.st_mtime_ns / 1_000_000 - float(previous["localMtimeMs"])) > 1.0:
        return "target mtime changed since checkpoint"
    expected_hash = previous.get("localContentHash")
    if expected_hash:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected_hash:
            return "target content changed since checkpoint"
    return None


def execute_probe(
    vault_path: str | Path,
    state_path: str | Path,
    files: list[dict],
    fetch,
    decoder,
    apply: bool = False,
    extra_protected_paths: list[str] | None = None,
    fetch_concurrency: int = 2,
    progress=None,
) -> dict:
    """Fetch and optionally apply explicit PULL files.

    ``fetch(key)`` returns RemoteObject and is injectable so all safety tests
    can run without touching a real R2 bucket.
    """
    started = time.perf_counter()
    vault = Path(vault_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    state_rel_path = _state_rel_path(vault, state_file)
    protected_paths = [state_rel_path] if state_rel_path else []
    protected_paths.extend(extra_protected_paths or [])
    _, is_protected_file = _ignore_matcher([], protected_paths=protected_paths)
    if len(files) < 1 or len(files) > 2:
        raise PullError("Probe accepts one or two files")
    previous = load_state(state_file)
    specs = []
    conflicts = []
    for raw in files:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise PullError("each files entry needs a path")
        rel_path = _safe_relative(raw["path"])
        if is_protected_file(rel_path):
            raise PullError("protected file cannot be a Probe sync target")
        conflict = _local_conflict(vault, rel_path, previous.get(rel_path))
        if conflict:
            conflicts.append({"path": rel_path, "reason": conflict})
        specs.append((rel_path, raw.get("key")))
    conflict_check_ms = round((time.perf_counter() - started) * 1000, 2)
    if conflicts:
        return {"ok": False, "mode": "apply" if apply else "dry-run", "planned": 0, "validated": 0, "applied": 0, "errors": [], "conflicts": conflicts, "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": 0, "apply": 0, "total": conflict_check_ms}}

    _emit_progress(progress, f"probe files: {len(specs)}件 / 前回状態: {len(previous)}件")
    _report_action_plan(progress, [{"type": "PULL", "path": rel_path} for rel_path, _ in specs])

    prepared = []
    errors = []
    _emit_progress(progress, f"R2から取得・検証しています(並列{min(fetch_concurrency, len(specs))}件)...")

    def prepare(spec):
        rel_path, key = spec
        remote = fetch(key or rel_path)
        clear = decoder.decrypt_content(remote.data)
        try:
            clear.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PullError("decrypted content is not UTF-8") from error
        return {"path": rel_path, "data": clear, "etag": remote.etag, "mtimeMs": _mtime_ms(remote.metadata)}

    completed = _run_parallel(
        specs, prepare, fetch_concurrency,
        lambda done, total: _report_fetch_progress(progress, done, total),
    )
    for spec, future in completed:
        try:
            prepared.append(future.result())
        except Exception as error:  # keep result JSON safe; no file has been written yet
            errors.append({"path": spec[0], "error": str(error)})
    fetch_validate_ms = round((time.perf_counter() - started) * 1000, 2) - conflict_check_ms
    if errors:
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"ok": False, "mode": "apply" if apply else "dry-run", "planned": len(specs), "validated": len(prepared), "applied": 0, "errors": errors, "conflicts": [], "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0, "total": total_ms}}

    result = {"ok": True, "mode": "apply" if apply else "dry-run", "planned": len(prepared), "validated": len(prepared), "applied": 0, "errors": [], "conflicts": [], "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0}}
    if not apply:
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    entries = dict(previous)
    apply_started = time.perf_counter()
    if prepared:
        _emit_progress(progress, "変更を適用しています(並列1件)...")
    for index, item in enumerate(prepared, start=1):
        try:
            stat = atomic_replace(_vault_path(vault, item["path"]), item["data"], item["mtimeMs"])
            entries[item["path"]] = _with_base({"localMtimeMs": stat.st_mtime_ns / 1_000_000, "localSize": stat.st_size, "remoteETag": item["etag"], "localContentHash": hashlib.sha256(item["data"]).hexdigest()}, item["data"])
            save_state(state_file, entries)
            result["applied"] += 1
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": item["path"], "error": str(error)})
            _emit_progress(progress, f"  適用中: {index}/{len(prepared)}")
            break
        if index % 50 == 0 or index == len(prepared):
            _emit_progress(progress, f"  適用中: {index}/{len(prepared)}")
    result["timingsMs"]["apply"] = round((time.perf_counter() - apply_started) * 1000, 2)
    result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and index + 1 < len(pattern) and pattern[index + 1] == "*":
            if index + 2 < len(pattern) and pattern[index + 2] == "/":
                parts.append("(?:.*/)?")
                index += 3
            else:
                parts.append(".*")
                index += 2
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1 or end == index + 1:
                parts.append(r"\[")
                index += 1
            else:
                content = pattern[index + 1:end].replace("\\", r"\\")
                if content.startswith("!"):
                    content = "^" + content[1:]
                parts.append(f"[{content}]")
                index = end + 1
        else:
            parts.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def _join_ignore_path(base_rel_path: str, pattern: str) -> str:
    parts = [part.strip("/") for part in (base_rel_path, pattern) if part.strip("/")]
    return "/".join(parts)


def _ignore_matcher(
    extra_patterns: list[str] | None = None, base_rel_path: str = "", protected_paths: list[str] | None = None
):
    """Return the gitignore-style glob matcher used by the desktop client."""
    base_rel_path = base_rel_path.replace("\\", "/").strip("/")
    protected = {path.replace("\\", "/").strip("/") for path in (protected_paths or []) if path}
    parsed = []
    for raw in extra_patterns or []:
        if not isinstance(raw, str):
            continue
        value = raw.replace("\\", "/").strip()
        if not value:
            continue
        legacy_anchored = value.startswith("./")
        anchored = legacy_anchored or value.startswith("/")
        if legacy_anchored:
            value = value[2:]
        value = value.lstrip("/")
        dir_only = value.endswith("/")
        if dir_only:
            value = value[:-1]
        if not value:
            continue
        has_slash = "/" in value
        parsed.append({
            "pattern": _join_ignore_path(base_rel_path, value) if anchored or has_slash else value,
            "any_depth": not anchored and not has_slash,
            "dir_only": dir_only,
        })

    def glob_matches(pattern: str, value: str) -> bool:
        return bool(_glob_regex(pattern).match(value))

    def path_prefixes(rel_path: str) -> list[str]:
        parts = rel_path.split("/")
        return ["/".join(parts[:index + 1]) for index in range(len(parts))]

    def under_base(rel_path: str) -> bool:
        return not base_rel_path or rel_path == base_rel_path or rel_path.startswith(base_rel_path + "/")

    def matches_extra(rel_path: str, is_file: bool) -> bool:
        if not under_base(rel_path):
            return False
        for item in parsed:
            pattern = item["pattern"]
            if item["any_depth"]:
                parts = rel_path.split("/")
                candidates = parts[:-1] if is_file and item["dir_only"] else parts
                if any(glob_matches(pattern, part) for part in candidates):
                    return True
                continue
            for prefix in path_prefixes(rel_path):
                if item["dir_only"] and is_file and prefix == rel_path:
                    continue
                if glob_matches(pattern, prefix) or (pattern.endswith("/**") and prefix == pattern[:-3]):
                    return True
        return False

    def ignored_dir(rel_path: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1]
        return name in {".git", "node_modules"} or matches_extra(rel_path, False)

    def ignored_file(rel_path: str) -> bool:
        parts = rel_path.split("/")
        basename = parts[-1]
        parent = "/".join(parts[:-1])
        protected_temp = False
        for protected_path in protected:
            protected_parts = protected_path.rsplit("/", 1)
            protected_parent = protected_parts[0] if len(protected_parts) == 2 else ""
            protected_name = protected_parts[-1]
            if parent == protected_parent and basename.startswith(f".{protected_name}.") and basename.endswith(".tmp"):
                protected_temp = True
                break
        return (
            rel_path in protected
            or protected_temp
            or basename in {".DS_Store", "Thumbs.db"}
            or any(part in {".git", "node_modules"} for part in parts[:-1])
            or matches_extra(rel_path, True)
        )

    return ignored_dir, ignored_file


def _scan_vault(
    vault: Path, extra_patterns: list[str] | None = None, base_rel_path: str = "", protected_paths: list[str] | None = None
) -> dict[str, dict]:
    """Recursively list regular files without following directory symlinks."""
    ignored_dir, ignored_file = _ignore_matcher(extra_patterns, base_rel_path, protected_paths)
    result = {}

    def walk(directory: Path, rel_dir: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise PullError(f"cannot scan vault: {directory}") from error
        for entry in entries:
            rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir(follow_symlinks=False):
                if not ignored_dir(rel_path):
                    walk(Path(entry.path), rel_path)
            elif entry.is_file(follow_symlinks=False) and not ignored_file(rel_path):
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise PullError(f"cannot stat vault file: {rel_path}") from error
                result[rel_path] = {"mtimeMs": stat.st_mtime_ns / 1_000_000, "size": stat.st_size}

    if vault.exists():
        if not vault.is_dir():
            raise PullError("vaultPath must be a directory")
        walk(vault, "")
    else:
        vault.mkdir(parents=True, exist_ok=True)
    return result


def _remote_mtime_ms(remote: RemoteObject, listed: dict) -> float:
    if remote.metadata.get("mtime") or remote.metadata.get("mmtime") or remote.metadata.get("mtime-ms"):
        return _mtime_ms(remote.metadata)
    raw = listed.get("lastModified")
    if isinstance(raw, str) and raw:
        try:
            parsed = _datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.timestamp() * 1000
        except ValueError:
            pass
    return time.time() * 1000


def _entry_from_stat(stat: os.stat_result, etag: str | None, data: bytes | None = None) -> dict:
    return {
        "localMtimeMs": stat.st_mtime_ns / 1_000_000,
        "localSize": stat.st_size,
        "remoteETag": etag,
        "localContentHash": hashlib.sha256(data).hexdigest() if data is not None else None,
    }


def _collect_remote_objects(
    listed: list[dict], decoder, remote_prefix: str, extra_patterns: list[str] | None = None, base_rel_path: str = "", protected_paths: list[str] | None = None
) -> tuple[dict[str, dict], list[dict], list[dict]]:
    prefix = remote_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""
    remotes = {}
    ignored = []
    conflicts = []
    _, ignored_file = _ignore_matcher(extra_patterns, base_rel_path, protected_paths)
    for raw in listed:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            conflicts.append({"path": "<remote-list>", "reason": "R2 LIST returned an invalid object"})
            continue
        key = raw["key"]
        if prefix and not key.startswith(prefix):
            continue
        encrypted_path = key[len(prefix):] if prefix else key
        try:
            rel_path = _safe_relative(decoder.decrypt_path(encrypted_path) if hasattr(decoder, "decrypt_path") else encrypted_path)
        except Exception as error:
            ignored.append({"key": key, "reason": f"cannot decode object path: {error}"})
            continue
        if ignored_file(rel_path):
            ignored.append({"key": key, "path": rel_path, "reason": "ignored"})
            continue
        if rel_path in remotes:
            conflicts.append({"path": rel_path, "reason": "multiple remote objects decode to the same path"})
            continue
        item = dict(raw)
        item["key"] = key
        remotes[rel_path] = item
    return remotes, ignored, conflicts


def _state_base_bytes(previous: dict | None) -> bytes | None:
    if not previous or not isinstance(previous.get("baseContentBase64"), str):
        return None
    try:
        return base64.b64decode(previous["baseContentBase64"], validate=True)
    except (ValueError, base64.binascii.Error):
        return None


def _eligible_text_merge_base(data: bytes, max_bytes: int) -> bool:
    if len(data) > max_bytes:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _with_base(entry: dict, data: bytes, text_merge_base_max_bytes: int | None = None) -> dict:
    result = dict(entry)
    if text_merge_base_max_bytes is None or _eligible_text_merge_base(data, text_merge_base_max_bytes):
        result["baseContentBase64"] = base64.b64encode(data).decode("ascii")
    else:
        result.pop("baseContentBase64", None)
    return result


def _prune_merge_bases(entries: dict, max_bytes: int | None) -> tuple[dict, int]:
    if max_bytes is None:
        return entries, 0
    result = dict(entries)
    pruned = 0
    for path_name, raw_entry in entries.items():
        if not isinstance(raw_entry, dict) or "baseContentBase64" not in raw_entry:
            continue
        base = _state_base_bytes(raw_entry)
        if base is not None and _eligible_text_merge_base(base, max_bytes):
            continue
        entry = dict(raw_entry)
        entry.pop("baseContentBase64", None)
        result[path_name] = entry
        pruned += 1
    return result, pruned


def _diff_hunks(base_lines: list[str], variant_lines: list[str]) -> list[dict]:
    return [
        {"start": start, "end": end, "replacement": variant_lines[new_start:new_end]}
        for tag, start, end, new_start, new_end in difflib.SequenceMatcher(None, base_lines, variant_lines, autojunk=False).get_opcodes()
        if tag != "equal"
    ]


def _hunks_overlap(left: dict, right: dict) -> bool:
    if left["start"] == left["end"] == right["start"] == right["end"]:
        return True
    if left["start"] == left["end"]:
        return right["start"] <= left["start"] <= right["end"]
    if right["start"] == right["end"]:
        return left["start"] <= right["start"] <= left["end"]
    return max(left["start"], right["start"]) < min(left["end"], right["end"])


def _render_hunks(base_lines: list[str], start: int, end: int, hunks: list[dict]) -> list[str]:
    output = []
    cursor = start
    for hunk in sorted(hunks, key=lambda value: (value["start"], value["end"])):
        output.extend(base_lines[cursor:hunk["start"]])
        output.extend(hunk["replacement"])
        cursor = hunk["end"]
    output.extend(base_lines[cursor:end])
    return output


def three_way_merge(base: bytes, local: bytes, remote: bytes) -> tuple[bytes | None, bool]:
    """Merge UTF-8 line changes; return ``(None, True)`` on a real conflict."""
    try:
        base_lines = base.decode("utf-8").splitlines(keepends=True)
        local_lines = local.decode("utf-8").splitlines(keepends=True)
        remote_lines = remote.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None, True
    local_hunks = _diff_hunks(base_lines, local_lines)
    remote_hunks = _diff_hunks(base_lines, remote_lines)
    all_hunks = [("local", hunk) for hunk in local_hunks] + [("remote", hunk) for hunk in remote_hunks]
    groups = []
    ungrouped = list(all_hunks)
    while ungrouped:
        group = [ungrouped.pop(0)]
        expanded = True
        while expanded:
            expanded = False
            remaining = []
            for candidate in ungrouped:
                if any(_hunks_overlap(candidate[1], existing[1]) for existing in group):
                    group.append(candidate)
                    expanded = True
                else:
                    remaining.append(candidate)
            ungrouped = remaining
        groups.append(group)

    output = []
    cursor = 0
    for group in sorted(groups, key=lambda value: (min(item[1]["start"] for item in value), min(item[1]["end"] for item in value))):
        start = min(item[1]["start"] for item in group)
        end = max(item[1]["end"] for item in group)
        output.extend(base_lines[cursor:start])
        local_text = _render_hunks(base_lines, start, end, [hunk for side, hunk in group if side == "local"])
        remote_text = _render_hunks(base_lines, start, end, [hunk for side, hunk in group if side == "remote"])
        if local_text == remote_text:
            output.extend(local_text)
        elif not any(side == "local" for side, _ in group):
            output.extend(remote_text)
        elif not any(side == "remote" for side, _ in group):
            output.extend(local_text)
        else:
            def marked(label: str, lines: list[str]) -> list[str]:
                text = "".join(lines)
                if text and not text.endswith("\n"):
                    text += "\n"
                return [f"{label}\n", text, "=======\n"] if label == "<<<<<<< LOCAL" else [text, f"{label}\n"]

            output.extend(marked("<<<<<<< LOCAL", local_text))
            output.extend(marked(">>>>>>> REMOTE", remote_text))
            return "".join(output + base_lines[end:]).encode("utf-8"), True
        cursor = end
    output.extend(base_lines[cursor:])
    return "".join(output).encode("utf-8"), False


def execute_full(
    vault_path: str | Path,
    state_path: str | Path,
    list_remote,
    fetch,
    decoder,
    remote_prefix: str = "",
    extra_patterns: list[str] | None = None,
    apply: bool = False,
    ignore_base_rel_path: str = "",
    extra_protected_paths: list[str] | None = None,
    fetch_concurrency: int = 2,
) -> dict:
    """Run a full, PULL-only scan with all writes deferred until validation."""
    started = time.perf_counter()
    vault = Path(vault_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    state_rel_path = _state_rel_path(vault, state_file)
    protected_paths = [state_rel_path] if state_rel_path else []
    protected_paths.extend(extra_protected_paths or [])
    previous = load_state(state_file)
    local = _scan_vault(vault, extra_patterns, ignore_base_rel_path, protected_paths)
    prefix = remote_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""

    try:
        listed = list_remote(prefix)
    except Exception as error:
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "ok": False, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
            "scannedRemote": 0, "planned": 0, "validated": 0, "applied": 0, "seeded": 0,
            "ignoredRemoteDeletes": 0, "errors": [{"error": str(error)}], "conflicts": [],
            "timingsMs": {"scan": 0, "conflictCheck": 0, "fetchValidate": 0, "apply": 0, "total": total_ms},
        }

    remotes = {}
    ignored_remote = []
    _, ignored_file = _ignore_matcher(extra_patterns, ignore_base_rel_path, protected_paths)
    for raw in listed:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            return {
                "ok": False, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
                "scannedRemote": len(listed), "planned": 0, "validated": 0, "applied": 0, "seeded": 0,
                "ignoredRemoteDeletes": 0, "errors": [{"error": "R2 LIST returned an invalid object"}],
                "conflicts": [], "timingsMs": {},
            }
        key = raw["key"]
        if prefix and not key.startswith(prefix):
            continue
        encrypted_path = key[len(prefix):] if prefix else key
        try:
            rel_path = _safe_relative(decoder.decrypt_path(encrypted_path) if hasattr(decoder, "decrypt_path") else encrypted_path)
        except Exception as error:
            ignored_remote.append({"key": key, "reason": f"cannot decode object path: {error}"})
            continue
        if ignored_file(rel_path):
            ignored_remote.append({"key": key, "path": rel_path, "reason": "ignored"})
            continue
        if rel_path in remotes:
            return {
                "ok": False, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
                "scannedRemote": len(listed), "planned": 0, "validated": 0, "applied": 0, "seeded": 0,
                "ignoredRemoteDeletes": 0, "errors": [], "conflicts":[{"path": rel_path, "reason": "multiple remote objects decode to the same path"}],
                "timingsMs": {},
            }
        raw = dict(raw)
        raw["key"] = key
        remotes[rel_path] = raw

    scan_ms = round((time.perf_counter() - started) * 1000, 2)
    conflicts = []
    actions = []
    bootstrap = []
    ignored_remote_deletes = 0
    for rel_path, listed_object in remotes.items():
        target = _vault_path(vault, rel_path)
        exists = target.is_file()
        prev = previous.get(rel_path)
        remote_etag = listed_object.get("etag")
        remote_changed = not prev or (remote_etag is not None and remote_etag != prev.get("remoteETag"))
        if prev and exists:
            local_conflict = _local_conflict(vault, rel_path, prev)
            if local_conflict and remote_changed:
                conflicts.append({"path": rel_path, "reason": local_conflict})
                continue
            if local_conflict:
                continue
            if remote_changed:
                actions.append({"type": "PULL", "path": rel_path, "remote": listed_object, "hadLocal": True})
        elif prev and not exists:
            actions.append({"type": "PULL", "path": rel_path, "remote": listed_object, "hadLocal": False})
        elif exists:
            bootstrap.append({"type": "SEED_OR_CONFLICT", "path": rel_path, "remote": listed_object, "hadLocal": True})
        else:
            actions.append({"type": "PULL", "path": rel_path, "remote": listed_object, "hadLocal": False})

    for rel_path in previous:
        if rel_path not in remotes:
            ignored_remote_deletes += 1

    conflict_check_ms = round((time.perf_counter() - started) * 1000, 2) - scan_ms
    if conflicts:
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "ok": False, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
            "scannedRemote": len(listed), "planned": 0, "validated": 0, "applied": 0, "seeded": 0,
            "ignoredRemoteDeletes": ignored_remote_deletes, "errors": [], "conflicts": conflicts,
            "timingsMs": {"scan": scan_ms, "conflictCheck": conflict_check_ms, "fetchValidate": 0, "apply": 0, "total": total_ms},
        }

    prepared = []

    def prepare(action):
        remote_info = action["remote"]
        remote = fetch(remote_info["key"])
        clear = decoder.decrypt_content(remote.data)
        return {
            **action,
            "data": clear,
            "etag": remote.etag or remote_info.get("etag"),
            "mtimeMs": _remote_mtime_ms(remote, remote_info),
        }

    fetch_actions = actions + bootstrap
    errors = []
    completed = _run_parallel(fetch_actions, prepare, fetch_concurrency)
    for action, future in completed:
        try:
            item = future.result()
            if action["type"] == "SEED_OR_CONFLICT":
                target = _vault_path(vault, action["path"])
                if target.read_bytes() == item["data"]:
                    item["type"] = "SEED"
                    actions.append(item)
                else:
                    conflicts.append({"path": action["path"], "reason": "local and remote contents differ without a checkpoint"})
            else:
                prepared.append(item)
        except Exception as error:
            errors.append({"path": action["path"], "error": str(error)})
    fetch_validate_ms = round((time.perf_counter() - started) * 1000, 2) - scan_ms - conflict_check_ms
    if conflicts or errors:
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "ok": False, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
            "scannedRemote": len(listed), "planned": len(fetch_actions), "validated": len(prepared),
            "applied": 0, "seeded": 0, "ignoredRemoteDeletes": ignored_remote_deletes,
            "errors": errors, "conflicts": conflicts,
            "timingsMs": {"scan": scan_ms, "conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0, "total": total_ms},
        }

    seed_actions = [item for item in actions if item["type"] == "SEED"]
    result = {
        "ok": True, "mode": "apply" if apply else "dry-run", "scannedLocal": len(local),
        "scannedRemote": len(listed), "planned": len(fetch_actions), "validated": len(prepared) + len(seed_actions),
        "applied": 0, "seeded": 0, "ignoredRemoteDeletes": ignored_remote_deletes,
        "ignoredRemoteObjects": ignored_remote, "errors": [], "conflicts": [],
        "timingsMs": {"scan": scan_ms, "conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0},
    }
    if not apply:
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    # Recheck every target before the first write so a file edited while R2 was
    # being fetched cannot turn a safe plan into a partial overwrite.
    for item in prepared:
        target = _vault_path(vault, item["path"])
        if item["hadLocal"]:
            conflict = _local_conflict(vault, item["path"], previous.get(item["path"]))
        else:
            conflict = "target appeared during fetch" if target.exists() else None
        if conflict:
            result["ok"] = False
            result["conflicts"].append({"path": item["path"], "reason": conflict})
    for item in seed_actions:
        if not _vault_path(vault, item["path"]).is_file():
            result["ok"] = False
            result["conflicts"].append({"path": item["path"], "reason": "target disappeared during fetch"})
    if not result["ok"]:
        result["validated"] = len(prepared) + len(seed_actions)
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    entries = dict(previous)
    apply_started = time.perf_counter()
    for item in prepared + seed_actions:
        try:
            target = _vault_path(vault, item["path"])
            if item["type"] == "PULL":
                stat = atomic_replace(target, item["data"], item["mtimeMs"])
                result["applied"] += 1
            else:
                stat = target.stat()
                result["seeded"] += 1
            entries[item["path"]] = _with_base(_entry_from_stat(stat, item["etag"], target.read_bytes()), target.read_bytes())
            save_state(state_file, entries)
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": item["path"], "error": str(error)})
            break
    result["timingsMs"]["apply"] = round((time.perf_counter() - apply_started) * 1000, 2)
    result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _action_counts(actions: list[dict]) -> dict[str, int]:
    counts = {}
    for action in actions:
        counts[action["type"]] = counts.get(action["type"], 0) + 1
    return counts


def execute_full_sync(
    vault_path: str | Path,
    state_path: str | Path,
    list_remote,
    fetch,
    put,
    remove,
    decoder,
    remote_prefix: str = "",
    extra_patterns: list[str] | None = None,
    apply: bool = False,
    push: bool = False,
    allow_delete: bool = False,
    merge: bool = False,
    ignore_base_rel_path: str = "",
    extra_protected_paths: list[str] | None = None,
    text_merge_base_max_bytes: int | None = None,
    recheck_remote_before_apply: bool = True,
    fetch_concurrency: int = 2,
    progress=None,
) -> dict:
    """Run the opt-in full bidirectional sync path.

    All network reads and merge decisions finish before the first mutation.
    Remote deletes and local PUSHes remain explicit capabilities so a plain
    full PULL config cannot accidentally alter the remote or local vault.
    """
    started = time.perf_counter()
    vault = Path(vault_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    state_rel_path = _state_rel_path(vault, state_file)
    protected_paths = [state_rel_path] if state_rel_path else []
    protected_paths.extend(extra_protected_paths or [])
    previous, merge_bases_to_prune = _prune_merge_bases(
        load_state(state_file), text_merge_base_max_bytes
    )
    _emit_progress(progress, "VaultとR2を走査しています...")
    scan_local_started = time.perf_counter()
    local = _scan_vault(vault, extra_patterns, ignore_base_rel_path, protected_paths)
    scan_local_ms = round((time.perf_counter() - scan_local_started) * 1000, 2)
    list_remote_started = time.perf_counter()
    listed = list_remote(remote_prefix)
    list_remote_ms = round((time.perf_counter() - list_remote_started) * 1000, 2)
    decode_remote_started = time.perf_counter()
    remotes, ignored_remote, list_conflicts = _collect_remote_objects(
        listed, decoder, remote_prefix, extra_patterns, ignore_base_rel_path, protected_paths
    )
    decode_remote_ms = round((time.perf_counter() - decode_remote_started) * 1000, 2)
    _emit_progress(
        progress,
        f"local files: {len(local)}件 / remote objects: {len(listed)}件 / 前回状態: {len(previous)}件",
    )
    scan_finished = time.perf_counter()
    scan_ms = round((scan_finished - started) * 1000, 2)
    actions = []
    candidates = []
    conflicts = list(list_conflicts)
    # An ignored file can still be present in an old checkpoint.  Filter only
    # ignored paths here: absent non-ignored paths must remain so FORGET and
    # delete planning can observe that both sides disappeared.
    _, ignored_file = _ignore_matcher(extra_patterns, ignore_base_rel_path, protected_paths)
    active_previous = {path: entry for path, entry in previous.items() if not ignored_file(path)}
    all_paths = sorted(set(local) | set(remotes) | set(active_previous))

    def local_changed(rel_path: str, prev: dict | None) -> bool:
        current = local.get(rel_path)
        if not prev or not current:
            return False
        size_changed = prev.get("localSize") is not None and current["size"] != prev["localSize"]
        mtime_changed = (
            prev.get("localMtimeMs") is not None
            and abs(current["mtimeMs"] - float(prev["localMtimeMs"])) > 1.0
        )
        return size_changed or mtime_changed

    def remote_changed(remote: dict | None, prev: dict | None) -> bool:
        return bool(remote and prev and remote.get("etag") != prev.get("remoteETag"))

    def add_pull(rel_path: str, remote: dict, had_local: bool, reason: str | None = None) -> None:
        actions.append({"type": "PULL", "path": rel_path, "remote": remote, "hadLocal": had_local, "reason": reason})

    for rel_path in all_paths:
        # `target` may exist on disk while the scanner intentionally omitted it
        # because it matches ignoreExtra.  Only the scanner result is a valid
        # local sync candidate.
        has_local = rel_path in local
        remote = remotes.get(rel_path)
        prev = active_previous.get(rel_path)
        if not prev:
            if has_local and remote:
                candidates.append({"type": "BOOTSTRAP", "path": rel_path, "remote": remote, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local[rel_path]["mtimeMs"]})
            elif has_local:
                actions.append({"type": "PUSH", "path": rel_path, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local[rel_path]["mtimeMs"], "enabled": push})
            elif remote:
                candidates.append({"type": "NEW_REMOTE", "path": rel_path, "remote": remote})
            continue

        loc_gone = not has_local and prev.get("localMtimeMs") is not None
        rem_gone = remote is None and prev.get("remoteETag") is not None
        local_is_changed = local_changed(rel_path, prev)
        remote_is_changed = remote_changed(remote, prev)
        if loc_gone and rem_gone:
            actions.append({"type": "FORGET", "path": rel_path})
        elif loc_gone and not rem_gone:
            if remote_is_changed:
                candidates.append({"type": "RESTORE_REMOTE_CHANGED", "path": rel_path, "remote": remote, "reason": "local deleted but remote changed"})
            else:
                actions.append({"type": "DELETE_REMOTE", "path": rel_path, "remote": remote})
        elif not loc_gone and rem_gone:
            if local_is_changed:
                actions.append({"type": "PUSH", "path": rel_path, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local[rel_path]["mtimeMs"], "enabled": push})
            else:
                actions.append({"type": "DELETE_LOCAL", "path": rel_path, "localSnapshotHash": hashlib.sha256(_vault_path(vault, rel_path).read_bytes()).hexdigest()})
        elif local_is_changed and remote_is_changed:
            candidates.append({"type": "BOTH_CHANGED", "path": rel_path, "remote": remote, "prev": prev, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local[rel_path]["mtimeMs"]})
        elif local_is_changed:
            local_bytes = _vault_path(vault, rel_path).read_bytes()
            if prev.get("localContentHash") and hashlib.sha256(local_bytes).hexdigest() == prev["localContentHash"]:
                actions.append({"type": "SEED", "path": rel_path, "remote": remote, "localBytes": local_bytes, "reason": "mtime変化のみ・内容一致のため転送スキップ"})
            else:
                actions.append({"type": "PUSH", "path": rel_path, "localBytes": local_bytes, "localMtimeMs": local[rel_path]["mtimeMs"], "enabled": push})
        elif remote_is_changed:
            candidates.append({"type": "REMOTE_CHANGED", "path": rel_path, "remote": remote, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local[rel_path]["mtimeMs"]})
        else:
            actions.append({"type": "NOOP", "path": rel_path})

    conflict_check_finished = time.perf_counter()
    conflict_check_ms = round((conflict_check_finished - scan_finished) * 1000, 2)
    prepared_count = 0

    def fetch_candidate(candidate):
        remote_info = candidate["remote"]
        remote = fetch(remote_info["key"])
        clear = decoder.decrypt_content(remote.data)
        return candidate, remote, clear

    errors = []
    if candidates:
        _emit_progress(progress, f"R2から取得・検証しています(並列{min(fetch_concurrency, len(candidates))}件)...")
    completed = _run_parallel(
        candidates, fetch_candidate, fetch_concurrency,
        lambda done, total: _report_fetch_progress(progress, done, total),
    )
    for candidate, future in completed:
        try:
            candidate, remote_object, remote_bytes = future.result()
            prepared_count += 1
            path_name = candidate["path"]
            remote_info = candidate["remote"]
            local_bytes = candidate.get("localBytes")
            if candidate["type"] == "REMOTE_CHANGED" and remote_bytes == local_bytes:
                actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "reason": "リモート再アップロードのみ・内容一致のため転送スキップ"})
            elif candidate["type"] == "REMOTE_CHANGED":
                add_pull(path_name, remote_info, True)
                actions[-1].update({"data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest()})
            elif candidate["type"] in ("NEW_REMOTE", "RESTORE_REMOTE_CHANGED"):
                actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": False, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "reason": candidate.get("reason")})
            elif candidate["type"] == "BOOTSTRAP" and remote_bytes == local_bytes:
                actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes})
            elif candidate["type"] == "BOOTSTRAP":
                remote_mtime = _remote_mtime_ms(remote_object, remote_info)
                if candidate["localMtimeMs"] > remote_mtime and push:
                    actions.append({"type": "PUSH", "path": path_name, "localBytes": local_bytes, "localMtimeMs": candidate["localMtimeMs"], "enabled": True, "reason": "初回比較: 内容不一致・ローカルの方が新しいため上書き"})
                elif candidate["localMtimeMs"] > remote_mtime:
                    conflicts.append({"path": path_name, "reason": "local is newer on bootstrap and PUSH is disabled"})
                else:
                    actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": True, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": remote_mtime, "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "reason": "初回比較: 内容不一致・リモートの方が新しいため上書き"})
            else:
                if remote_bytes == local_bytes:
                    actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "reason": "両側でtouchされたが内容は一致・転送スキップ"})
                    continue
                prev = candidate["prev"]
                if merge:
                    base = _state_base_bytes(prev)
                    if base is None:
                        conflicts.append({"path": path_name, "reason": "merge base is unavailable in checkpoint state"})
                        continue
                    merged, has_conflict = three_way_merge(base, local_bytes, remote_bytes)
                    if has_conflict or merged is None:
                        conflicts.append({"path": path_name, "reason": "three-way merge conflict"})
                        continue
                    actions.append({"type": "MERGE", "path": path_name, "remote": remote_info, "data": merged, "localMtimeMs": max(candidate["localMtimeMs"], _remote_mtime_ms(remote_object, remote_info)), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "remoteBytes": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "reason": "両側変更・3-way merge"})
                elif candidate["localMtimeMs"] > _remote_mtime_ms(remote_object, remote_info) and push:
                    actions.append({"type": "PUSH", "path": path_name, "localBytes": local_bytes, "localMtimeMs": candidate["localMtimeMs"], "enabled": True, "reason": "両側変更・ローカルの方が新しいため上書き"})
                elif candidate["localMtimeMs"] > _remote_mtime_ms(remote_object, remote_info):
                    conflicts.append({"path": path_name, "reason": "both sides changed and PUSH is disabled"})
                else:
                    actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": True, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "reason": "両側変更・リモートの方が新しいため上書き"})
        except Exception as error:
            errors.append({"path": candidate["path"], "error": str(error)})

    fetch_validate_ms = round((time.perf_counter() - conflict_check_finished) * 1000, 2)
    unchanged = sum(action["type"] == "NOOP" for action in actions)
    applicable_actions = [action for action in actions if action["type"] != "NOOP"]
    counts = _action_counts(applicable_actions)
    _report_action_plan(progress, actions)
    result = {
        "ok": not conflicts and not errors,
        "mode": "apply" if apply else "dry-run", "unchanged": unchanged,
        "scannedLocal": len(local), "scannedRemote": len(listed), "planned": len(applicable_actions), "validated": prepared_count,
        "applied": 0, "errors": errors, "conflicts": conflicts, "ignoredRemoteObjects": ignored_remote,
        "remoteSnapshotRecheckEnabled": recheck_remote_before_apply,
        "remoteSnapshotRechecked": False,
        "fetchConcurrency": fetch_concurrency,
        "mergeBasesPruned": 0, "mergeBasesPendingPrune": merge_bases_to_prune,
        "plannedByType": counts, "appliedByType": {}, "skippedByType": {},
        "timingsMs": {
            "scan": scan_ms, "scanLocal": scan_local_ms, "listRemote": list_remote_ms,
            "decodeRemote": decode_remote_ms, "conflictCheck": conflict_check_ms,
            "fetchValidate": fetch_validate_ms, "snapshotCheck": 0, "apply": 0,
            "applyRemote": 0, "applyCheckpoint": 0,
        },
    }

    entries = dict(previous)
    apply_remote_seconds = 0.0
    apply_checkpoint_seconds = 0.0
    state_dirty = merge_bases_to_prune > 0

    def mark_merge_bases_pruned() -> None:
        result["mergeBasesPruned"] += result["mergeBasesPendingPrune"]
        result["mergeBasesPendingPrune"] = 0

    def persist_entries() -> None:
        nonlocal apply_checkpoint_seconds, state_dirty
        checkpoint_started = time.perf_counter()
        try:
            save_state(state_file, entries)
        finally:
            apply_checkpoint_seconds += time.perf_counter() - checkpoint_started
        state_dirty = False
        mark_merge_bases_pruned()

    def timed_remote(call, *args):
        nonlocal apply_remote_seconds
        remote_started = time.perf_counter()
        try:
            return call(*args)
        finally:
            apply_remote_seconds += time.perf_counter() - remote_started

    def finish_apply_timing(apply_started: float) -> None:
        result["timingsMs"]["apply"] = round((time.perf_counter() - apply_started) * 1000, 2)
        result["timingsMs"]["applyRemote"] = round(apply_remote_seconds * 1000, 2)
        result["timingsMs"]["applyCheckpoint"] = round(apply_checkpoint_seconds * 1000, 2)

    if conflicts or errors or not apply:
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    # Recheck local state for every operation before the first remote or local mutation.
    for action in applicable_actions:
        path_name = action["path"]
        target = _vault_path(vault, path_name)
        if action["type"] in ("PUSH", "MERGE", "SEED"):
            expected_hash = action.get("localSnapshotHash") or hashlib.sha256(action.get("localBytes", b"")).hexdigest()
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                result["conflicts"].append({"path": path_name, "reason": "local file changed during planning"})
        elif action["type"] == "PULL":
            if action.get("hadLocal"):
                expected_hash = action.get("localSnapshotHash")
                if not expected_hash or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                    result["conflicts"].append({"path": path_name, "reason": "local file changed during fetch"})
            elif target.exists():
                result["conflicts"].append({"path": path_name, "reason": "target appeared during fetch"})
        elif action["type"] == "DELETE_LOCAL" and action.get("localSnapshotHash"):
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != action["localSnapshotHash"]:
                result["conflicts"].append({"path": path_name, "reason": "local file changed during planning"})
        elif action["type"] in ("DELETE_REMOTE", "FORGET") and target.exists():
            result["conflicts"].append({"path": path_name, "reason": "local file appeared during planning"})
    if result["conflicts"]:
        result["ok"] = False
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    if not applicable_actions:
        _emit_progress(progress, "適用対象の変更はありません。")
        if state_dirty:
            apply_started = time.perf_counter()
            try:
                persist_entries()
            except Exception as error:
                result["ok"] = False
                result["errors"].append({"path": "<state>", "error": str(error)})
            finish_apply_timing(apply_started)
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    # A second full listing closes the fetch/validation window.  Any change in
    # the remote snapshot aborts the whole batch before its first mutation.
    if recheck_remote_before_apply:
        _emit_progress(progress, "R2 snapshotを再確認しています...")
        snapshot_started = time.perf_counter()
        try:
            latest_listed = list_remote(remote_prefix)
            latest_remotes, _, latest_conflicts = _collect_remote_objects(
                latest_listed, decoder, remote_prefix, extra_patterns, ignore_base_rel_path, protected_paths
            )
            result["conflicts"].extend(latest_conflicts)
            snapshot_paths = sorted(set(remotes) | set(latest_remotes))
            for path_name in snapshot_paths:
                before = remotes.get(path_name)
                after = latest_remotes.get(path_name)
                before_identity = None if before is None else (before.get("key"), before.get("etag"), before.get("size"), before.get("lastModified"))
                after_identity = None if after is None else (after.get("key"), after.get("etag"), after.get("size"), after.get("lastModified"))
                if before_identity != after_identity:
                    result["conflicts"].append({"path": path_name, "reason": "remote object changed during planning"})
            result["remoteSnapshotRechecked"] = True
        except Exception as error:
            result["errors"].append({"path": "<remote-list>", "error": str(error)})
        finally:
            result["timingsMs"]["snapshotCheck"] = round((time.perf_counter() - snapshot_started) * 1000, 2)
    else:
        _emit_progress(progress, "警告: 適用前のR2 snapshot再確認をスキップします。")
    if result["conflicts"] or result["errors"]:
        result["ok"] = False
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    apply_started = time.perf_counter()
    _emit_progress(progress, "変更を適用しています(並列1件)...")

    def checkpoint(path_name: str, etag: str | None, data: bytes, persist: bool = True) -> None:
        nonlocal state_dirty
        target = _vault_path(vault, path_name)
        entries[path_name] = _with_base(
            _entry_from_stat(target.stat(), etag, data), data, text_merge_base_max_bytes
        )
        state_dirty = True
        if persist:
            persist_entries()

    def bump(mapping: dict, action_type: str) -> None:
        mapping[action_type] = mapping.get(action_type, 0) + 1

    for index, action in enumerate(applicable_actions, start=1):
        path_name = action["path"]
        target = _vault_path(vault, path_name)
        try:
            action_type = action["type"]
            if action_type == "SEED":
                checkpoint(path_name, action["remote"].get("etag"), action["localBytes"], persist=False)
                bump(result["appliedByType"], action_type)
            elif action_type == "PUSH":
                if not push:
                    bump(result["skippedByType"], action_type)
                    continue
                etag = timed_remote(
                    put,
                    action["remote"]["key"] if action.get("remote") else _remote_key(decoder, remote_prefix, path_name),
                    decoder.encrypt_content(action["localBytes"]),
                    action["localMtimeMs"],
                )
                checkpoint(path_name, etag, action["localBytes"])
                result["applied"] += 1
                bump(result["appliedByType"], action_type)
            elif action_type == "PULL":
                atomic_replace(target, action["data"], action["mtimeMs"])
                checkpoint(path_name, action["etag"], action["data"])
                result["applied"] += 1
                bump(result["appliedByType"], action_type)
            elif action_type == "MERGE":
                atomic_replace(target, action["data"], action["localMtimeMs"])
                etag = timed_remote(
                    put, action["remote"]["key"], decoder.encrypt_content(action["data"]), action["localMtimeMs"]
                )
                checkpoint(path_name, etag, action["data"])
                result["applied"] += 1
                bump(result["appliedByType"], action_type)
            elif action_type == "DELETE_REMOTE":
                if allow_delete:
                    timed_remote(remove, action["remote"]["key"])
                    entries.pop(path_name, None)
                    state_dirty = True
                    persist_entries()
                    result["applied"] += 1
                    bump(result["appliedByType"], action_type)
                else:
                    bump(result["skippedByType"], action_type)
            elif action_type == "DELETE_LOCAL":
                if allow_delete:
                    target.unlink()
                    entries.pop(path_name, None)
                    state_dirty = True
                    persist_entries()
                    result["applied"] += 1
                    bump(result["appliedByType"], action_type)
                else:
                    bump(result["skippedByType"], action_type)
            elif action_type == "FORGET":
                entries.pop(path_name, None)
                state_dirty = True
                persist_entries()
                bump(result["appliedByType"], action_type)
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": path_name, "error": str(error)})
            _emit_progress(progress, f"  適用中: {index}/{len(applicable_actions)}")
            break
        if index % 50 == 0 or index == len(applicable_actions):
            _emit_progress(progress, f"  適用中: {index}/{len(applicable_actions)}")
    if state_dirty and not result["errors"]:
        try:
            persist_entries()
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": "<state>", "error": str(error)})
    finish_apply_timing(apply_started)
    result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _remote_key(decoder, remote_prefix: str, rel_path: str) -> str:
    prefix = remote_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""
    encoded = decoder.encrypt_path(rel_path) if hasattr(decoder, "encrypt_path") else _safe_relative(rel_path)
    return prefix + encoded


def _ignore_base_rel_path(vault_path: str | Path, config_path: Path) -> str:
    """Return the config directory relative to the vault, or empty for legacy external configs."""
    vault = Path(vault_path).expanduser().resolve()
    base = config_path.expanduser().resolve().parent
    try:
        relative = base.relative_to(vault).as_posix()
        return "" if relative == "." else relative
    except ValueError:
        return ""


def _config_relative_path(value: str | Path, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    config_relative = (config_path.parent / path).resolve()
    legacy_cwd_relative = path.resolve()
    if config_relative != legacy_cwd_relative and not config_relative.exists() and legacy_cwd_relative.exists():
        return legacy_cwd_relative
    return config_relative


def _load_config(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise PullError(f"cannot read config: {path}") from error
    if not isinstance(config, dict):
        raise PullError("config root must be an object")
    required = ("vaultPath", "statePath", "endpoint", "bucket", "accessKeyId", "secretAccessKey")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise PullError("config is missing: " + ", ".join(missing))
    mode = config.get("mode", "probe")
    if mode not in ("probe", "full"):
        raise PullError("mode must be probe or full")
    if mode == "probe" and not isinstance(config.get("files"), list):
        raise PullError("files must be an array of objects in probe mode")
    if "ignoreExtra" in config and (
        not isinstance(config["ignoreExtra"], list) or not all(isinstance(item, str) for item in config["ignoreExtra"])
    ):
        raise PullError("ignoreExtra must be an array of strings")
    max_base_bytes = config.get("textMergeBaseMaxBytes")
    if max_base_bytes is not None and (isinstance(max_base_bytes, bool) or not isinstance(max_base_bytes, int) or max_base_bytes < 0):
        raise PullError("textMergeBaseMaxBytes must be a non-negative integer")
    if "recheckRemoteBeforeApply" in config and not isinstance(config["recheckRemoteBeforeApply"], bool):
        raise PullError("recheckRemoteBeforeApply must be true or false")
    fetch_concurrency = config.get("fetchConcurrency", 2)
    if isinstance(fetch_concurrency, bool) or not isinstance(fetch_concurrency, int) or not 1 <= fetch_concurrency <= 16:
        raise PullError("fetchConcurrency must be an integer from 1 to 16")
    request_timeout = config.get("requestTimeoutSeconds", 30)
    if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or not 1 <= request_timeout <= 300:
        raise PullError("requestTimeoutSeconds must be a number from 1 to 300")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="r2-sync iOS sync")
    parser.add_argument("--config", required=True, help="path to the JSON config")
    parser.add_argument("--apply", action="store_true", help="replace vault files and checkpoint state")
    parser.add_argument("--allow-delete", action="store_true", help="allow planned remote/local deletions in full mode")
    # Kept hidden so an already-installed Shortcut can be migrated separately.
    parser.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--push", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--merge", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = _load_config(config_path)
        ignore_base_rel_path = _ignore_base_rel_path(config["vaultPath"], config_path)
        state_path = _config_relative_path(config["statePath"], config_path)
        config_rel_path = _state_rel_path(Path(config["vaultPath"]).expanduser().resolve(), config_path)
        extra_protected_paths = [config_rel_path] if config_rel_path else []
        mode = config.get("encryption", "rclone-base64")
        if mode not in ("rclone-base64", "plain"):
            raise PullError("encryption must be rclone-base64 or plain")
        decoder = RcloneBase64(config.get("password", "")) if mode == "rclone-base64" else PlainContent()
        fetch_concurrency = config.get("fetchConcurrency", 2)
        request_timeout = config.get("requestTimeoutSeconds", 30)
        r2 = R2Client(
            config["endpoint"], config["bucket"], config["accessKeyId"], config["secretAccessKey"],
            timeout=request_timeout,
        )
        prefix = str(config.get("remotePrefix", "")).replace("\\", "/").strip("/")
        prefix = prefix + "/" if prefix else ""
        full = args.full or config.get("mode") == "full"
        progress(f"vault: {config['vaultPath']}")
        progress(f"mode: {'APPLY (削除含む)' if args.apply and args.allow_delete else 'APPLY (削除は警告のみ)' if args.apply else 'DRY-RUN'}")
        progress(f"fetch concurrency: {fetch_concurrency} / request timeout: {request_timeout}秒")
        if full:
            result = execute_full_sync(
                config["vaultPath"], state_path, r2.list_all, r2.get_object, r2.put_object, r2.delete_object, decoder,
                remote_prefix=prefix, extra_patterns=config.get("ignoreExtra", []), apply=args.apply,
                push=True, allow_delete=args.allow_delete, merge=True, ignore_base_rel_path=ignore_base_rel_path,
                extra_protected_paths=extra_protected_paths,
                text_merge_base_max_bytes=config.get("textMergeBaseMaxBytes"),
                recheck_remote_before_apply=config.get("recheckRemoteBeforeApply", True),
                fetch_concurrency=fetch_concurrency,
                progress=progress,
            )
        else:
            files = config["files"]
            if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
                raise PullError("files must be an array of objects")
            for item in files:
                if not item.get("key"):
                    item["key"] = prefix + (decoder.encrypt_path(_safe_relative(item["path"])) if mode == "rclone-base64" else _safe_relative(item["path"]))
            result = execute_probe(
                config["vaultPath"], state_path, files, r2.get_object, decoder, args.apply,
                extra_protected_paths=extra_protected_paths, fetch_concurrency=fetch_concurrency, progress=progress,
            )
        _report_result(progress, result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["ok"] else 1
    except PullError as error:
        result = {"ok": False, "mode": "apply" if args.apply else "dry-run", "planned": 0, "validated": 0, "applied": 0, "errors": [{"error": str(error)}], "conflicts": [], "timingsMs": {}}
        _report_result(progress, result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParallelInterrupted:
        print("\n中断しました。", file=sys.stderr, flush=True)
        os._exit(130)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr, flush=True)
        raise SystemExit(130)
