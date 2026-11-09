"""Self-update of sync.py and r2sync/ from GitHub Releases.

Only runs when explicitly requested. The release zip is checked against the
release's SHA256SUMS, its entry names, its embedded version and a syntax
compile before the installed files are swapped; the previous version is kept
in .r2sync-backup/ and restored if the swap fails midway.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import urllib.error
import urllib.request
import zipfile

from . import PullError, __version__


DEFAULT_REPOSITORY = "minato8080/obsidian-r2-sync-cli"
CHECKSUM_ASSET = "SHA256SUMS"
BACKUP_DIR = ".r2sync-backup"
STAGING_PREFIX = ".r2sync-update-"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
USER_AGENT = "r2-sync-self-update"

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_ENTRY_RE = re.compile(r"^(sync\.py|r2sync/[A-Za-z_][A-Za-z0-9_]*\.py)$")
_EMBEDDED_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def asset_name(tag: str) -> str:
    return f"r2sync-python-{tag}.zip"


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(value))
    if not match:
        raise PullError(f"invalid version: {value}")
    return tuple(int(part) for part in match.groups())


def embedded_version(init_source: bytes) -> str | None:
    match = _EMBEDDED_VERSION_RE.search(init_source.decode("utf-8"))
    return match.group(1) if match else None


def http_get(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream, application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise PullError(f"download is larger than {MAX_DOWNLOAD_BYTES} bytes: {url}")
    return data


def default_install_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _inside_git_checkout(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _read_release_zip(data: bytes, version: str) -> dict[str, bytes]:
    files = {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise PullError(f"release zip is invalid: {error}") from error
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                if info.filename != "r2sync/":
                    raise PullError(f"unexpected directory in release zip: {info.filename}")
                continue
            if not _ENTRY_RE.match(info.filename) or info.filename in files:
                raise PullError(f"unexpected file in release zip: {info.filename}")
            files[info.filename] = archive.read(info)
    for required in ("sync.py", "r2sync/__init__.py"):
        if required not in files:
            raise PullError(f"release zip is missing {required}")
    found = embedded_version(files["r2sync/__init__.py"])
    if found != version:
        raise PullError(f"release zip version {found} does not match release {version}")
    for name, source in files.items():
        try:
            compile(source, name, "exec")
        except SyntaxError as error:
            raise PullError(f"release file does not compile: {name}: {error}") from error
    return files


def _expected_checksum(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise PullError(f"{CHECKSUM_ASSET} has no entry for {name}")


def _install(install_dir: Path, files: dict[str, bytes]) -> None:
    staging = install_dir / f"{STAGING_PREFIX}{os.getpid()}"
    backup = install_dir / BACKUP_DIR
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for name, data in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir()
        moved = []  # (current location, original location) for rollback
        try:
            for name in ("sync.py", "r2sync"):
                os.replace(install_dir / name, backup / name)
                moved.append((backup / name, install_dir / name))
            for name in ("sync.py", "r2sync"):
                os.replace(staging / name, install_dir / name)
        except BaseException:
            for name in ("sync.py", "r2sync"):
                if (install_dir / name).exists() and (backup / name).exists():
                    if (install_dir / name).is_dir():
                        shutil.rmtree(install_dir / name)
                    else:
                        (install_dir / name).unlink()
            for current, original in reversed(moved):
                os.replace(current, original)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def self_update(
    repository: str = DEFAULT_REPOSITORY,
    check_only: bool = False,
    install_dir: Path | None = None,
    current_version: str = __version__,
    fetch=http_get,
    progress=None,
) -> dict:
    emit = progress or (lambda message: None)
    install_dir = Path(install_dir or default_install_dir()).resolve()
    result = {
        "ok": False, "mode": "self-update-check" if check_only else "self-update",
        "repository": repository, "currentVersion": current_version, "latestVersion": None,
        "status": None, "updated": False, "errors": [],
    }
    if not _REPOSITORY_RE.match(repository):
        raise PullError(f"invalid repository: {repository}")
    if not (install_dir / "sync.py").is_file() or not (install_dir / "r2sync" / "__init__.py").is_file():
        raise PullError(f"install directory does not contain sync.py and r2sync/: {install_dir}")
    if not check_only and _inside_git_checkout(install_dir):
        raise PullError("refusing to self-update inside a Git checkout; use git pull instead")

    emit(f"現在の版: {current_version}")
    emit(f"最新リリースを確認しています: {repository}")
    try:
        release = json.loads(fetch(f"https://api.github.com/repos/{repository}/releases/latest").decode("utf-8"))
    except PullError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise PullError(f"no published release found in {repository}") from error
        raise PullError(f"cannot read latest release: {error}") from error
    except Exception as error:
        raise PullError(f"cannot read latest release: {error}") from error
    tag = str(release.get("tag_name", ""))
    latest = parse_version(tag)
    version = ".".join(str(part) for part in latest)
    result["latestVersion"] = version
    if latest <= parse_version(current_version):
        result.update(ok=True, status="up-to-date")
        emit("最新版です。")
        return result
    if check_only:
        result.update(ok=True, status="available")
        emit(f"新しい版があります: {version}")
        return result

    download_prefix = f"https://github.com/{repository}/releases/download/"
    assets = {
        asset.get("name"): asset.get("browser_download_url")
        for asset in release.get("assets", []) if isinstance(asset, dict)
    }
    zip_name = asset_name(tag)
    for name in (zip_name, CHECKSUM_ASSET):
        url = assets.get(name)
        if not isinstance(url, str) or not url.startswith(download_prefix):
            raise PullError(f"release asset is missing or not hosted by the repository: {name}")
    emit(f"{zip_name}をダウンロードしています...")
    try:
        sums = fetch(assets[CHECKSUM_ASSET])
        archive = fetch(assets[zip_name])
    except PullError:
        raise
    except Exception as error:
        raise PullError(f"cannot download release assets: {error}") from error
    if hashlib.sha256(archive).hexdigest() != _expected_checksum(sums, zip_name):
        raise PullError(f"SHA-256 mismatch for {zip_name}")
    files = _read_release_zip(archive, version)
    emit("検証に成功しました。置き換えています...")
    _install(install_dir, files)
    result.update(ok=True, status="updated", updated=True)
    emit(f"{version}へ更新しました。直前の版は{BACKUP_DIR}/に保存しました。")
    return result
