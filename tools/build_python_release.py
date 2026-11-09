#!/usr/bin/env python3
"""Build the Python release zip and SHA256SUMS consumed by `sync.py --self-update`."""

from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PY_SOURCE = ROOT / "py"
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def release_files(source: Path = PY_SOURCE) -> dict[str, bytes]:
    files = {"sync.py": (source / "sync.py").read_bytes()}
    for path in sorted((source / "r2sync").glob("*.py")):
        files[f"r2sync/{path.name}"] = path.read_bytes()
    return files


def source_version(files: dict[str, bytes]) -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', files["r2sync/__init__.py"].decode("utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit("py/r2sync/__init__.py has no __version__")
    return match.group(1)


def build_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()


def build_release(tag: str, out_dir: Path, source: Path = PY_SOURCE) -> tuple[Path, Path]:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"tag must look like vX.Y.Z: {tag}")
    files = release_files(source)
    version = source_version(files)
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag} does not match __version__ {version}")
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"r2sync-python-{tag}.zip"
    zip_path.write_bytes(build_zip(files))
    sums_path = out_dir / "SHA256SUMS"
    sums_path.write_bytes(f"{hashlib.sha256(zip_path.read_bytes()).hexdigest()}  {zip_path.name}\n".encode("utf-8"))
    return zip_path, sums_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, e.g. v1.0.0")
    parser.add_argument("--out", default=str(ROOT / "dist" / "release"), help="output directory")
    args = parser.parse_args(argv)
    for path in build_release(args.tag, Path(args.out)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
