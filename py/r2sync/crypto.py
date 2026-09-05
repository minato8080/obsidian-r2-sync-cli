"""Remotely Save compatible filename and content encryption."""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import os

from . import PullError


DEFAULT_SALT = bytes.fromhex("a80df43a8fbd0308a7cab83e581f86b1")
MAGIC = b"RCLONE\x00\x00"
BLOCK_DATA_SIZE = 64 * 1024
BLOCK_TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + 24


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
