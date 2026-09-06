"""JSON configuration loading, validation, and path resolution."""

from __future__ import annotations

import json
from pathlib import Path

from . import PullError


UNICODE_COLLISION_POLICIES = {"error", "prefer-nfc"}


def _config_relative_path(value: str | Path, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config_path.parent / path).resolve()


def _load_config(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise PullError(f"cannot read config: {path}") from error
    if not isinstance(config, dict):
        raise PullError("config root must be an object")
    required = ("vaultPath", "statePath", "endpoint", "bucket", "accessKeyId", "secretAccessKey", "mode")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise PullError("config is missing: " + ", ".join(missing))
    mode = config["mode"]
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
    apply_concurrency = config.get("applyConcurrency", 1)
    if isinstance(apply_concurrency, bool) or not isinstance(apply_concurrency, int) or not 1 <= apply_concurrency <= 16:
        raise PullError("applyConcurrency must be an integer from 1 to 16")
    request_timeout = config.get("requestTimeoutSeconds", 30)
    if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or not 1 <= request_timeout <= 300:
        raise PullError("requestTimeoutSeconds must be a number from 1 to 300")
    unicode_collision_policy = config.get("unicodeCollisionPolicy", "error")
    if not isinstance(unicode_collision_policy, str) or unicode_collision_policy not in UNICODE_COLLISION_POLICIES:
        raise PullError("unicodeCollisionPolicy must be error or prefer-nfc")
    return config
