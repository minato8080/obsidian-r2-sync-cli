"""Checkpoint persistence and merge-base retention."""

from __future__ import annotations

import base64
import datetime as _datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import PullError
from .local import _choose_nfc_candidate, _record_unicode_aliases, _safe_relative


def load_state(
    path: Path, unicode_collision_policy: str = "error", collision_stats: dict | None = None,
) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("entries", {}), dict):
        raise PullError("state file has an invalid format")
    grouped = {}
    for raw_path, entry in value["entries"].items():
        if not isinstance(raw_path, str):
            raise PullError("state file has an invalid path")
        rel_path = _safe_relative(raw_path)
        grouped.setdefault(rel_path, []).append((raw_path, entry))
    entries = {}
    for rel_path, candidates in grouped.items():
        selected = _choose_nfc_candidate(
            [raw_path for raw_path, _ in candidates], rel_path, unicode_collision_policy, "state"
        )
        entries[rel_path] = candidates[selected][1]
        _record_unicode_aliases(collision_stats, len(candidates) - 1)
    return entries


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
def _entry_from_stat(stat: os.stat_result, etag: str | None, data: bytes | None = None) -> dict:
    return {
        "localMtimeMs": stat.st_mtime_ns / 1_000_000,
        "localSize": stat.st_size,
        "remoteETag": etag,
        "localContentHash": hashlib.sha256(data).hexdigest() if data is not None else None,
    }
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
