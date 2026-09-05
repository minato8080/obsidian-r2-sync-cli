"""Vault path validation, ignore matching, scanning, and local mutation."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata

from . import PullError


def _sync_lock_path(vault: Path) -> Path:
    identity = unicodedata.normalize(
        "NFC", os.path.normcase(os.path.normpath(str(vault.expanduser().resolve())))
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"r2-sync-{digest}.lock"


class _SyncRunLock:
    """Non-blocking process lock for sync runs that target the same local vault."""

    def __init__(self, vault: Path):
        self.path = _sync_lock_path(vault)
        self._stream = None

    def acquire(self) -> None:
        try:
            stream = self.path.open("a+b")
        except OSError as error:
            raise PullError("cannot open sync lock") from error
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN) or getattr(error, "winerror", None) in (33, 36):
                raise PullError("another sync is already running for this vault") from error
            raise PullError("cannot acquire sync lock") from error
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            stream.close()
def _safe_relative(value: str) -> str:
    return unicodedata.normalize("NFC", _safe_relative_spelling(value))


def _safe_relative_spelling(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PullError(f"unsafe relative path: {value!r}")
    return "/".join(path.parts)


def _choose_nfc_candidate(raw_names: list[str], canonical: str, policy: str, source: str) -> int:
    if len(raw_names) == 1:
        return 0
    if policy == "prefer-nfc":
        exact = [index for index, raw_name in enumerate(raw_names) if raw_name == canonical]
        if len(exact) == 1:
            return exact[0]
    raise PullError(f"multiple {source} paths normalize to the same path: {canonical!r}")


def _record_unicode_aliases(stats: dict | None, count: int) -> None:
    if stats is not None and count:
        stats["ignored"] = stats.get("ignored", 0) + count


def _vault_path(vault: Path, rel_path: str) -> Path:
    vault_resolved = vault.resolve()
    target = vault
    for part in _safe_relative(rel_path).split("/"):
        try:
            target.resolve().relative_to(vault_resolved)
        except ValueError as error:
            raise PullError(f"path escapes vault: {rel_path!r}") from error
        direct = target / part
        if os.path.lexists(direct):
            target = direct
            continue
        matches = []
        try:
            with os.scandir(target) as entries:
                matches = [
                    Path(entry.path) for entry in entries
                    if unicodedata.normalize("NFC", entry.name) == part
                ]
        except (FileNotFoundError, NotADirectoryError):
            pass
        if len(matches) > 1:
            raise PullError(f"multiple local paths normalize to the same path: {rel_path!r}")
        target = matches[0] if matches else direct
    target = target.resolve()
    try:
        target.relative_to(vault_resolved)
    except ValueError as error:
        raise PullError(f"path escapes vault: {rel_path!r}") from error
    return target


def _state_rel_path(vault: Path, state_path: Path) -> str | None:
    try:
        return unicodedata.normalize("NFC", state_path.resolve().relative_to(vault.resolve()).as_posix())
    except ValueError:
        return None
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


def _ignore_matcher(
    extra_patterns: list[str] | None = None, protected_paths: list[str] | None = None
):
    """Return a gitignore-style glob matcher for vault-relative paths."""
    protected = {
        unicodedata.normalize("NFC", path.replace("\\", "/").strip("/"))
        for path in (protected_paths or []) if path
    }
    patterns = []
    for raw in extra_patterns or []:
        if not isinstance(raw, str):
            continue
        value = unicodedata.normalize("NFC", raw.replace("\\", "/").strip())
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
        patterns.append({
            "pattern": value,
            "regex": _glob_regex(value),
            "any_depth": not anchored and not has_slash,
            "dir_only": dir_only,
        })

    def path_prefixes(rel_path: str) -> list[str]:
        parts = rel_path.split("/")
        return ["/".join(parts[:index + 1]) for index in range(len(parts))]

    def matches_extra(rel_path: str, is_file: bool) -> bool:
        normalized = unicodedata.normalize("NFC", rel_path.replace("\\", "/").strip("/"))
        for item in patterns:
            pattern = item["pattern"]
            regex = item["regex"]
            if item["any_depth"]:
                parts = normalized.split("/")
                candidates = parts[:-1] if is_file and item["dir_only"] else parts
                if any(regex.match(part) for part in candidates):
                    return True
                continue
            for prefix in path_prefixes(normalized):
                if item["dir_only"] and is_file and prefix == normalized:
                    continue
                if regex.match(prefix) or (
                    pattern.endswith("/**") and prefix == pattern[:-3]
                ):
                    return True
        return False

    def ignored_dir(rel_path: str) -> bool:
        rel_path = unicodedata.normalize("NFC", rel_path.replace("\\", "/").strip("/"))
        name = rel_path.rsplit("/", 1)[-1]
        return name in {".git", "node_modules"} or matches_extra(rel_path, False)

    def ignored_file(rel_path: str) -> bool:
        rel_path = unicodedata.normalize("NFC", rel_path.replace("\\", "/").strip("/"))
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
            or basename in {".DS_Store", "Thumbs.db", "state.json"}
            or any(part in {".git", "node_modules"} for part in parts[:-1])
            or matches_extra(rel_path, True)
        )

    return ignored_dir, ignored_file


def _normalized_directory_entries(
    directory: Path, rel_dir: str, unicode_collision_policy: str, collision_stats: dict | None,
):
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        raise PullError(f"cannot scan vault: {directory}") from error
    grouped = {}
    for entry in entries:
        entry_name = unicodedata.normalize("NFC", entry.name)
        grouped.setdefault(entry_name, []).append(entry)
    selected_entries = []
    for entry_name, candidates in grouped.items():
        canonical_path = f"{rel_dir}/{entry_name}" if rel_dir else entry_name
        raw_paths = [f"{rel_dir}/{entry.name}" if rel_dir else entry.name for entry in candidates]
        selected = _choose_nfc_candidate(
            raw_paths, canonical_path, unicode_collision_policy, "local"
        )
        selected_entries.append((candidates[selected], entry_name))
        _record_unicode_aliases(collision_stats, len(candidates) - 1)
    return selected_entries


def _scan_vault(
    vault: Path, extra_patterns: list[str] | None = None, protected_paths: list[str] | None = None,
    unicode_collision_policy: str = "error", collision_stats: dict | None = None,
) -> dict[str, dict]:
    """Recursively list regular files without following directory symlinks."""
    ignored_dir, ignored_file = _ignore_matcher(extra_patterns, protected_paths)
    result = {}

    def walk(directory: Path, rel_dir: str) -> None:
        entries = _normalized_directory_entries(
            directory, rel_dir, unicode_collision_policy, collision_stats
        )
        for entry, entry_name in entries:
            rel_path = f"{rel_dir}/{entry_name}" if rel_dir else entry_name
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


def _reconcile_local_files(vault: Path, local: dict[str, dict], remote_paths) -> int:
    """Recover existing files omitted by directory enumeration or Unicode spelling differences."""
    recovered = 0
    for rel_path in sorted(set(remote_paths) - set(local)):
        target = _vault_path(vault, rel_path)
        if not target.is_file():
            continue
        try:
            stat = target.stat()
        except OSError as error:
            raise PullError(f"cannot stat vault file: {rel_path}") from error
        local[rel_path] = {"mtimeMs": stat.st_mtime_ns / 1_000_000, "size": stat.st_size}
        recovered += 1
    return recovered


def _classify_vault_paths(
    vault: Path, extra_patterns: list[str] | None = None, protected_paths: list[str] | None = None,
    verbose: bool = False, unicode_collision_policy: str = "error",
) -> tuple[list[str], list[str]]:
    """Classify local paths with the sync matcher without changing the vault."""
    ignored_dir, ignored_file = _ignore_matcher(extra_patterns, protected_paths)
    ignored_paths = []
    included_paths = []

    def walk(directory: Path, rel_dir: str, inherited_ignore: bool = False) -> None:
        entries = sorted(
            _normalized_directory_entries(directory, rel_dir, unicode_collision_policy, None),
            key=lambda item: item[1],
        )
        for entry, entry_name in entries:
            rel_path = f"{rel_dir}/{entry_name}" if rel_dir else entry_name
            if entry.is_dir(follow_symlinks=False):
                directory_ignored = inherited_ignore or ignored_dir(rel_path)
                if directory_ignored:
                    ignored_paths.append(rel_path + "/")
                    if verbose:
                        walk(Path(entry.path), rel_path, True)
                else:
                    walk(Path(entry.path), rel_path, False)
            elif entry.is_file(follow_symlinks=False):
                (ignored_paths if inherited_ignore or ignored_file(rel_path) else included_paths).append(rel_path)

    if not vault.exists() or not vault.is_dir():
        raise PullError("vaultPath must be an existing directory")
    walk(vault, "")
    return ignored_paths, included_paths


def _format_count(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def _summarize_included_paths(
    included_paths: list[str], ignored_paths: list[str],
) -> list[tuple[str, int | None]]:
    """Collapse each highest directory whose entire visible subtree is included."""
    direct_files: dict[str, list[str]] = {}
    child_dirs: dict[str, set[str]] = {}
    included_counts: dict[str, int] = {}
    blocked_dirs: set[str] = set()

    for rel_path in included_paths:
        parts = PurePosixPath(rel_path).parts
        parent = "/".join(parts[:-1])
        direct_files.setdefault(parent, []).append(rel_path)
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            directory_parent = "/".join(parts[:index - 1])
            child_dirs.setdefault(directory_parent, set()).add(directory)
            included_counts[directory] = included_counts.get(directory, 0) + 1

    for rel_path in ignored_paths:
        parts = PurePosixPath(rel_path.rstrip("/")).parts
        for index in range(1, len(parts) + 1):
            blocked_dirs.add("/".join(parts[:index]))

    summary: list[tuple[str, int | None]] = []

    def emit(directory: str) -> None:
        summary.extend((rel_path, None) for rel_path in direct_files.get(directory, []))
        for child in child_dirs.get(directory, set()):
            if child not in blocked_dirs:
                summary.append((child + "/", included_counts[child]))
            else:
                emit(child)

    emit("")
    return sorted(summary, key=lambda item: item[0])
