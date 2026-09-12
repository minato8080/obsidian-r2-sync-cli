"""Offline runtime benchmark for the Node and Python implementations.

The Android profile is intentionally a workload profile, not an Android emulator.
It keeps the same operations and data shape while using a smaller Termux-like
workload; real device numbers must be collected on the target device.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import tempfile
import time

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "py"
import sys
sys.path.insert(0, str(SOURCE_ROOT))

from r2sync.crypto import RcloneBase64
from r2sync.local import _scan_vault
from r2sync.planner import classify_sync_path
from r2sync.remote import _collect_remote_objects


def timed(function):
    started = time.perf_counter()
    value = function()
    return value, (time.perf_counter() - started) * 1000


def median(samples):
    return statistics.median(samples)


def make_dataset(root: Path, file_count: int, content_bytes: int) -> None:
    payload = bytes((index * 31 + 17) & 0xff for index in range(content_bytes))
    for index in range(file_count):
        relative = Path(f"dir-{index % 50:02d}") / f"note-{index:05d}.md"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("windows", "android-pseudo"), default="windows")
    parser.add_argument("--files", type=int)
    parser.add_argument("--bytes", type=int)
    parser.add_argument("--repeats", type=int, default=5)
    options = parser.parse_args()
    defaults = (1000, 8192) if options.profile == "android-pseudo" else (2000, 16384)
    file_count = options.files or defaults[0]
    content_bytes = options.bytes or defaults[1]
    root = Path(tempfile.mkdtemp(prefix="r2-sync-bench-python-"))

    try:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        make_dataset(root, file_count, content_bytes)

        cipher = RcloneBase64("benchmark-password")
        scan_samples = []
        local = None
        for _ in range(options.repeats):
            local, elapsed = timed(lambda: _scan_vault(root))
            scan_samples.append(elapsed)

        paths = list(local)
        remote_items = []
        for relative in paths:
            file_index = int(relative.rsplit("note-", 1)[1][:-3])
            remote_items.append({"key": cipher.encrypt_path(relative), "etag": f"etag-{file_index}"})
        previous = {
            relative: {
                "localMtimeMs": info["mtimeMs"],
                "localSize": info["size"],
                "remoteETag": f"etag-{int(relative.rsplit('note-', 1)[1][:-3])}",
                "localContentHash": "benchmark-hash",
            }
            for relative, info in local.items()
        }

        def decode_and_plan():
            remotes, ignored, conflicts = _collect_remote_objects(
                remote_items, cipher, "", [], [], "error", {"ignored": 0}
            )
            actions = [
                classify_sync_path(relative, local.get(relative), remotes.get(relative), previous.get(relative))
                for relative in sorted(set(local) | set(remotes) | set(previous))
            ]
            return actions, ignored, conflicts

        plan_samples = []
        planned = None
        for _ in range(options.repeats):
            planned, elapsed = timed(decode_and_plan)
            plan_samples.append(elapsed)

        content = bytes(content_bytes)
        crypto_samples = []
        checksum = 0
        crypto_items = min(file_count, 256)
        for _ in range(options.repeats):
            def encrypt_decrypt():
                total = 0
                for _ in range(crypto_items):
                    encrypted = cipher.encrypt_content(content)
                    clear = cipher.decrypt_content(encrypted)
                    total += len(clear) + len(encrypted)
                return total
            value, elapsed = timed(encrypt_decrypt)
            crypto_samples.append(elapsed)
            checksum += value

        print(json.dumps({
            "runtime": "python",
            "profile": options.profile,
            "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
            "platform": f"{os.name}-{os.uname().machine if hasattr(os, 'uname') else 'windows'}",
            "dataset": {"files": file_count, "bytesPerFile": content_bytes, "repeats": options.repeats},
            "benchmarks": {
                "scanLocalFiles": {"medianMs": median(scan_samples), "samplesMs": scan_samples, "items": file_count},
                "decodeAndPlanNoop": {"medianMs": median(plan_samples), "samplesMs": plan_samples, "items": file_count, "noop": sum(item["type"] == "NOOP" for item in planned[0])},
                "encryptDecryptContent": {"medianMs": median(crypto_samples), "samplesMs": crypto_samples, "items": crypto_items, "bytesPerSample": content_bytes * crypto_items, "checksum": checksum},
            },
        }, ensure_ascii=False))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
