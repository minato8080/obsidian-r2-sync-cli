"""Command-line parsing, reporting, and dependency assembly."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import PullError
from .config import _config_relative_path, _load_config
from .crypto import PlainContent, RcloneBase64
from .executor import execute_full_sync, execute_probe
from .local import (
    _SyncRunLock,
    _classify_vault_paths,
    _format_count,
    _ignore_matcher,
    _safe_relative,
    _state_rel_path,
    _summarize_included_paths,
    _vault_path,
)
from .remote import R2Client


REMOTE_OBJECT_DETAIL_LIMIT = 20


def _emit_progress(progress, message: str) -> None:
    if progress is not None:
        progress(message)


def _report_result(progress, result: dict) -> None:
    if result.get("mode") == "dry-run" and result.get("ok"):
        _emit_progress(progress, "\ndry-runのため実際の変更は行っていません。--apply を付けて実行してください。")
    _emit_progress(progress, "\n=== 実行結果 ===")
    _emit_progress(progress, f"ok: {'true' if result.get('ok') else 'false'}")
    for name in (
        "planned", "unchanged", "validated", "applied", "seeded", "ignoredRemoteDeletes",
        "reconciledLocalFiles", "unicodeAliasesIgnored", "mergeBasesPruned", "mergeBasesPendingPrune",
    ):
        if name in result:
            _emit_progress(progress, f"{name}: {result[name]}")
    ignored_remote_objects = result.get("ignoredRemoteObjects", [])
    if ignored_remote_objects:
        _emit_progress(progress, f"ignoredRemoteObjects: {len(ignored_remote_objects)}")
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


def _result_for_json(result: dict) -> dict:
    """Compact routine ignored-remote details without mutating the sync result."""
    ignored_remote_objects = result.get("ignoredRemoteObjects")
    if not isinstance(ignored_remote_objects, list):
        return result
    details = [
        item for item in ignored_remote_objects
        if not isinstance(item, dict) or item.get("reason") != "ignored"
    ][:REMOTE_OBJECT_DETAIL_LIMIT]
    output = {}
    for name, value in result.items():
        if name == "ignoredRemoteObjects":
            output[name] = details
            output["ignoredRemoteObjectsTotal"] = len(ignored_remote_objects)
            output["ignoredRemoteObjectsOmitted"] = len(ignored_remote_objects) - len(details)
        else:
            output[name] = value
    return output
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="r2-sync iOS sync")
    parser.add_argument("--config", required=True, help="path to the JSON config")
    parser.add_argument("--apply", action="store_true", help="replace vault files and checkpoint state")
    parser.add_argument("--allow-delete", action="store_true", help="allow planned remote/local deletions in full mode")
    parser.add_argument(
        "--check-ignore", nargs="*", metavar="PATH",
        help="check ignored vault-relative paths locally; omit PATH to list ignored vault entries",
    )
    parser.add_argument("--verbose", action="store_true", help="show every path in --check-ignore mode")
    # Kept hidden so an already-installed Shortcut can be migrated separately.
    parser.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--push", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--merge", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    run_lock = None
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = _load_config(config_path)
        state_path = _config_relative_path(config["statePath"], config_path)
        vault_path = Path(config["vaultPath"]).expanduser().resolve()
        config_rel_path = _state_rel_path(vault_path, config_path)
        extra_protected_paths = [config_rel_path] if config_rel_path else []
        state_rel_path = _state_rel_path(vault_path, state_path)
        protected_paths = ([state_rel_path] if state_rel_path else []) + extra_protected_paths
        unicode_collision_policy = config.get("unicodeCollisionPolicy", "error")
        if args.check_ignore is not None:
            print("=== 除外判定（ローカルのみ / R2通信なし）===")
            print(f"vault: {vault_path}")
            print(f"ignoreExtra: {_format_count(len(config.get('ignoreExtra', [])), 'pattern')}")
            print(f"unicodeCollisionPolicy: {unicode_collision_policy}")
            if args.check_ignore:
                ignored_dir, ignored_file = _ignore_matcher(config.get("ignoreExtra", []), protected_paths)
                all_ignored = True
                for raw_path in args.check_ignore:
                    is_dir = raw_path.endswith(("/", "\\"))
                    rel_path = _safe_relative(raw_path.rstrip("/\\"))
                    target = _vault_path(vault_path, rel_path)
                    is_dir = is_dir or target.is_dir()
                    ignored = ignored_dir(rel_path) if is_dir else ignored_file(rel_path)
                    print(f"[{'IGNORE' if ignored else 'INCLUDE'}] {rel_path}{'/' if is_dir else ''}")
                    all_ignored = all_ignored and ignored
                return 0 if all_ignored else 1
            ignored_paths, included_paths = _classify_vault_paths(
                vault_path, config.get("ignoreExtra", []), protected_paths, verbose=args.verbose,
                unicode_collision_policy=unicode_collision_policy,
            )
            print(f"\n[IGNORE] {_format_count(len(ignored_paths), 'entry', 'entries')}")
            if args.verbose:
                for rel_path in ignored_paths:
                    print(f"  {rel_path}")
            else:
                for rel_path in ignored_paths:
                    print(f"  {rel_path}{' (all files)' if rel_path.endswith('/') else ''}")
            print(f"\n[INCLUDE] {_format_count(len(included_paths), 'file')}")
            if args.verbose:
                for rel_path in included_paths:
                    print(f"  {rel_path}")
            else:
                for rel_path, count in _summarize_included_paths(included_paths, ignored_paths):
                    suffix = f" ({_format_count(count, 'file')})" if count is not None else ""
                    print(f"  {rel_path}{suffix}")
            return 0
        run_lock = _SyncRunLock(vault_path)
        run_lock.acquire()
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
        progress(f"unicode collision policy: {unicode_collision_policy}")
        if full:
            result = execute_full_sync(
                config["vaultPath"], state_path, r2.list_all, r2.get_object, r2.put_object, r2.delete_object, decoder,
                remote_prefix=prefix, extra_patterns=config.get("ignoreExtra", []), apply=args.apply,
                push=True, allow_delete=args.allow_delete, merge=True,
                extra_protected_paths=extra_protected_paths,
                text_merge_base_max_bytes=config.get("textMergeBaseMaxBytes"),
                recheck_remote_before_apply=config.get("recheckRemoteBeforeApply", True),
                fetch_concurrency=fetch_concurrency,
                unicode_collision_policy=unicode_collision_policy,
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
                extra_protected_paths=extra_protected_paths, fetch_concurrency=fetch_concurrency,
                unicode_collision_policy=unicode_collision_policy, progress=progress,
            )
        _report_result(progress, result)
        print(json.dumps(_result_for_json(result), ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except PullError as error:
        result = {"ok": False, "mode": "apply" if args.apply else "dry-run", "planned": 0, "validated": 0, "applied": 0, "errors": [{"error": str(error)}], "conflicts": [], "timingsMs": {}}
        _report_result(progress, result)
        print(json.dumps(_result_for_json(result), ensure_ascii=False, indent=2))
        return 1
    finally:
        if run_lock is not None:
            run_lock.release()
