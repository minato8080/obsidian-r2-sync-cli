import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

PYTHON_SOURCE = Path(__file__).resolve().parents[2] / "py"
sys.path.insert(0, str(PYTHON_SOURCE))

from r2sync import PullError
from r2sync import checkpoint as checkpoint_module
from r2sync import cli as cli_module
from r2sync import config as config_module
from r2sync import crypto as crypto_module
from r2sync import executor as executor_module
from r2sync import local as local_module
from r2sync import remote as remote_module
from r2sync.checkpoint import load_state
from r2sync.cli import _report_result
from r2sync.config import _config_relative_path
from r2sync.crypto import (
    MAGIC,
    PlainContent,
    RcloneBase64,
    _aes_block,
    _poly1305,
    _xsalsa_stream,
)
from r2sync.executor import execute_full_sync, execute_probe
from r2sync.local import _ignore_matcher, _scan_vault
from r2sync.remote import R2Client, RemoteObject


class FakeRemote:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def get(self, key):
        self.calls.append(key)
        value = self.objects[key]
        if isinstance(value, Exception):
            raise value
        return value

    def list(self, prefix=""):
        return [
            {"key": key, "etag": value.etag if isinstance(value, RemoteObject) else None, "size": len(value.data) if isinstance(value, RemoteObject) else 0, "lastModified": "2023-11-14T22:13:20Z"}
            for key, value in self.objects.items()
            if key.startswith(prefix)
        ]

    def put(self, key, data, mtime_ms):
        etag = f'"put-{len(self.objects)}-{key}"'
        self.objects[key] = RemoteObject(data, etag, {"mtime": str(mtime_ms / 1000)})
        return etag

    def delete(self, key):
        self.objects.pop(key, None)


def encrypt_content_for_test(clear, cipher, nonce):
    output = bytearray(MAGIC + nonce)
    for offset in range(0, len(clear), 64 * 1024):
        block = clear[offset : offset + 64 * 1024]
        stream = _xsalsa_stream(cipher.data_key, nonce, len(block) + 32)
        encrypted = bytes(a ^ b for a, b in zip(block, stream[32:]))
        output.extend(_poly1305(encrypted, stream[:32]))
        output.extend(encrypted)
        nonce = bytearray(nonce)
        for index in range(24):
            nonce[index] = (nonce[index] + 1) & 0xFF
            if nonce[index]:
                break
        nonce = bytes(nonce)
    return bytes(output)


__all__ = [name for name in globals() if not name.startswith("__")]
