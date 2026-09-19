"""Cross-client R2 sidecar manifest fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import json


MANIFEST_VERSION = 1
MANIFEST_ALGORITHM = "HMAC-SHA256"


def content_fingerprint(data: bytes, manifest_key: str) -> str:
    if not isinstance(manifest_key, str) or not manifest_key:
        raise ValueError("manifestKey must be a non-empty string")
    return hmac.new(manifest_key.encode("utf-8"), data, hashlib.sha256).hexdigest()


def parse_manifest(data: bytes, listed_keys: set[str], prefix: str, reserved_key: str | None = None) -> dict | None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != MANIFEST_VERSION
        or value.get("algorithm") != MANIFEST_ALGORITHM
        or value.get("prefix") != prefix
        or not isinstance(value.get("entries"), dict)
    ):
        return None
    entries = value["entries"]
    expected = set(listed_keys)
    if reserved_key:
        expected.discard(reserved_key)
    if set(entries) != expected:
        return None
    for key in expected:
        entry = entries[key]
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("etag"), str)
            or isinstance(entry.get("size"), bool)
            or not isinstance(entry.get("size"), int)
            or entry["size"] < 0
            or not isinstance(entry.get("fingerprint"), str)
            or len(entry["fingerprint"]) != 64
            or any(char not in "0123456789abcdef" for char in entry["fingerprint"])
        ):
            return None
    return value


def build_manifest(entries: dict, prefix: str = "") -> bytes:
    return (json.dumps({"version": MANIFEST_VERSION, "algorithm": MANIFEST_ALGORITHM, "prefix": prefix, "entries": entries}, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")
