"""Synchronization workflows and guarded mutation application."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import PullError
from .checkpoint import (
    _entry_from_stat,
    _prune_merge_bases,
    _state_base_bytes,
    _with_base,
    load_state,
    save_state,
)
from .local import (
    _ignore_matcher,
    _local_conflict,
    _reconcile_local_files,
    _safe_relative,
    _scan_vault,
    _state_rel_path,
    _vault_path,
    atomic_replace,
)
from .planner import _action_counts, classify_sync_path, three_way_merge
from .remote import (
    _collect_remote_objects,
    _mtime_ms,
    _remote_key,
    _remote_mtime_ms,
    _run_parallel,
)


ACTION_PATH_DISPLAY_LIMIT = 20


def _emit_progress(progress, message: str) -> None:
    if progress is not None:
        progress(message)


def _report_action_plan(progress, actions: list[dict]) -> None:
    grouped = {}
    for action in actions:
        grouped.setdefault(action["type"], []).append(action)
    for action_type, items in grouped.items():
        _emit_progress(progress, f"\n[{action_type}] {len(items)}件")
        for action in items[:ACTION_PATH_DISPLAY_LIMIT]:
            reason = f" ({action['reason']})" if action.get("reason") else ""
            _emit_progress(progress, f"  {action['path']}{reason}")
        if len(items) > ACTION_PATH_DISPLAY_LIMIT:
            _emit_progress(progress, f"  ...ほか{len(items) - ACTION_PATH_DISPLAY_LIMIT}件")


def _report_fetch_progress(progress, done: int, total: int) -> None:
    if total <= 10 or done % 50 == 0 or done == total:
        _emit_progress(progress, f"  取得・検証中: {done}/{total}")


def _report_push_progress(progress, done: int, total: int) -> None:
    if total <= 10 or done % 50 == 0 or done == total:
        _emit_progress(progress, f"  PUSH中: {done}/{total}")


def execute_probe(
    vault_path: str | Path,
    state_path: str | Path,
    files: list[dict],
    fetch,
    decoder,
    apply: bool = False,
    extra_protected_paths: list[str] | None = None,
    fetch_concurrency: int = 2,
    unicode_collision_policy: str = "error",
    progress=None,
) -> dict:
    """Fetch and optionally apply explicit PULL files.

    ``fetch(key)`` returns RemoteObject and is injectable so all safety tests
    can run without touching a real R2 bucket.
    """
    started = time.perf_counter()
    vault = Path(vault_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    state_rel_path = _state_rel_path(vault, state_file)
    protected_paths = [state_rel_path] if state_rel_path else []
    protected_paths.extend(extra_protected_paths or [])
    _, is_protected_file = _ignore_matcher([], protected_paths=protected_paths)
    if len(files) < 1 or len(files) > 2:
        raise PullError("Probe accepts one or two files")
    collision_stats = {"ignored": 0}
    previous = load_state(state_file, unicode_collision_policy, collision_stats)
    specs = []
    conflicts = []
    for raw in files:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise PullError("each files entry needs a path")
        rel_path = _safe_relative(raw["path"])
        if is_protected_file(rel_path):
            raise PullError("protected file cannot be a Probe sync target")
        conflict = _local_conflict(vault, rel_path, previous.get(rel_path))
        if conflict:
            conflicts.append({"path": rel_path, "reason": conflict})
        specs.append((rel_path, raw.get("key")))
    conflict_check_ms = round((time.perf_counter() - started) * 1000, 2)
    if conflicts:
        return {"ok": False, "mode": "apply" if apply else "dry-run", "planned": 0, "validated": 0, "applied": 0, "errors": [], "conflicts": conflicts, "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": 0, "apply": 0, "total": conflict_check_ms}}

    _emit_progress(progress, f"probe files: {len(specs)}件 / 前回状態: {len(previous)}件")
    _report_action_plan(progress, [{"type": "PULL", "path": rel_path} for rel_path, _ in specs])

    prepared = []
    errors = []
    _emit_progress(progress, f"R2から取得・検証しています(並列{min(fetch_concurrency, len(specs))}件)...")

    def prepare(spec):
        rel_path, key = spec
        remote = fetch(key or rel_path)
        clear = decoder.decrypt_content(remote.data)
        try:
            clear.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PullError("decrypted content is not UTF-8") from error
        return {"path": rel_path, "data": clear, "etag": remote.etag, "mtimeMs": _mtime_ms(remote.metadata)}

    completed = _run_parallel(
        specs, prepare, fetch_concurrency,
        lambda done, total: _report_fetch_progress(progress, done, total),
    )
    for spec, future in completed:
        try:
            prepared.append(future.result())
        except Exception as error:  # keep result JSON safe; no file has been written yet
            errors.append({"path": spec[0], "error": str(error)})
    fetch_validate_ms = round((time.perf_counter() - started) * 1000, 2) - conflict_check_ms
    if errors:
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"ok": False, "mode": "apply" if apply else "dry-run", "planned": len(specs), "validated": len(prepared), "applied": 0, "errors": errors, "conflicts": [], "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0, "total": total_ms}}

    result = {"ok": True, "mode": "apply" if apply else "dry-run", "planned": len(prepared), "validated": len(prepared), "applied": 0, "unicodeAliasesIgnored": collision_stats["ignored"], "errors": [], "conflicts": [], "timingsMs": {"conflictCheck": conflict_check_ms, "fetchValidate": fetch_validate_ms, "apply": 0}}
    if not apply:
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    entries = dict(previous)
    apply_started = time.perf_counter()
    if prepared:
        _emit_progress(progress, "変更を適用しています(並列1件)...")
    for index, item in enumerate(prepared, start=1):
        try:
            stat = atomic_replace(_vault_path(vault, item["path"]), item["data"], item["mtimeMs"])
            entries[item["path"]] = _with_base({"localMtimeMs": stat.st_mtime_ns / 1_000_000, "localSize": stat.st_size, "remoteETag": item["etag"], "localContentHash": hashlib.sha256(item["data"]).hexdigest()}, item["data"])
            save_state(state_file, entries)
            result["applied"] += 1
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": item["path"], "error": str(error)})
            _emit_progress(progress, f"  適用中: {index}/{len(prepared)}")
            break
        if index % 50 == 0 or index == len(prepared):
            _emit_progress(progress, f"  適用中: {index}/{len(prepared)}")
    result["timingsMs"]["apply"] = round((time.perf_counter() - apply_started) * 1000, 2)
    result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    return result
def execute_full_sync(
    vault_path: str | Path,
    state_path: str | Path,
    list_remote,
    fetch,
    put,
    remove,
    decoder,
    remote_prefix: str = "",
    extra_patterns: list[str] | None = None,
    apply: bool = False,
    push: bool = False,
    allow_delete: bool = False,
    merge: bool = False,
    extra_protected_paths: list[str] | None = None,
    text_merge_base_max_bytes: int | None = None,
    recheck_remote_before_apply: bool = True,
    fetch_concurrency: int = 2,
    apply_concurrency: int = 1,
    unicode_collision_policy: str = "error",
    progress=None,
) -> dict:
    """Run the opt-in full bidirectional sync path.

    All network reads and merge decisions finish before the first mutation.
    Remote deletes and local PUSHes remain explicit capabilities so a plain
    full PULL config cannot accidentally alter the remote or local vault.
    """
    started = time.perf_counter()
    vault = Path(vault_path).expanduser().resolve()
    state_file = Path(state_path).expanduser().resolve()
    state_rel_path = _state_rel_path(vault, state_file)
    protected_paths = [state_rel_path] if state_rel_path else []
    protected_paths.extend(extra_protected_paths or [])
    collision_stats = {"ignored": 0}
    previous, merge_bases_to_prune = _prune_merge_bases(
        load_state(state_file, unicode_collision_policy, collision_stats), text_merge_base_max_bytes
    )
    _emit_progress(progress, "VaultとR2を走査しています...")
    scan_local_started = time.perf_counter()
    local = _scan_vault(
        vault, extra_patterns, protected_paths, unicode_collision_policy, collision_stats
    )
    scan_local_ms = round((time.perf_counter() - scan_local_started) * 1000, 2)
    list_remote_started = time.perf_counter()
    listed = list_remote(remote_prefix)
    list_remote_ms = round((time.perf_counter() - list_remote_started) * 1000, 2)
    decode_remote_started = time.perf_counter()
    remotes, ignored_remote, list_conflicts = _collect_remote_objects(
        listed, decoder, remote_prefix, extra_patterns, protected_paths,
        unicode_collision_policy, collision_stats,
    )
    decode_remote_ms = round((time.perf_counter() - decode_remote_started) * 1000, 2)
    reconcile_local_started = time.perf_counter()
    reconciled_local_files = _reconcile_local_files(vault, local, remotes)
    reconcile_local_ms = round((time.perf_counter() - reconcile_local_started) * 1000, 2)
    _emit_progress(
        progress,
        f"local files: {len(local)}件 / remote objects: {len(listed)}件 / 前回状態: {len(previous)}件",
    )
    if reconciled_local_files:
        _emit_progress(progress, f"local paths reconciled: {reconciled_local_files}件")
    scan_finished = time.perf_counter()
    scan_ms = round((scan_finished - started) * 1000, 2)
    actions = []
    candidates = []
    conflicts = list(list_conflicts)
    # An ignored file can still be present in an old checkpoint.  Filter only
    # ignored paths here: absent non-ignored paths must remain so FORGET and
    # delete planning can observe that both sides disappeared.
    _, ignored_file = _ignore_matcher(extra_patterns, protected_paths)
    active_previous = {path: entry for path, entry in previous.items() if not ignored_file(path)}
    all_paths = sorted(set(local) | set(remotes) | set(active_previous))

    def add_pull(rel_path: str, remote: dict, had_local: bool, reason: str | None = None) -> None:
        actions.append({"type": "PULL", "path": rel_path, "remote": remote, "hadLocal": had_local, "reason": reason})

    for rel_path in all_paths:
        # `target` may exist on disk while the scanner intentionally omitted it
        # because it matches ignoreExtra.  Only the scanner result is a valid
        # local sync candidate.
        local_info = local.get(rel_path)
        remote = remotes.get(rel_path)
        prev = active_previous.get(rel_path)
        decision = classify_sync_path(rel_path, local_info, remote, prev)
        decision_type = decision["type"]
        if decision_type == "BOOTSTRAP":
            candidates.append({"type": "BOOTSTRAP", "path": rel_path, "remote": remote, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local_info["mtimeMs"]})
        elif decision_type == "NEW_LOCAL":
            actions.append({"type": "PUSH", "path": rel_path, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local_info["mtimeMs"], "enabled": push})
        elif decision_type == "NEW_REMOTE":
            candidates.append({"type": "NEW_REMOTE", "path": rel_path, "remote": remote})
        elif decision_type == "FORGET":
            actions.append({"type": "FORGET", "path": rel_path})
        elif decision_type == "RESTORE_REMOTE_CHANGED":
            candidates.append({"type": "RESTORE_REMOTE_CHANGED", "path": rel_path, "remote": remote, "reason": "local deleted but remote changed"})
        elif decision_type == "DELETE_REMOTE":
            actions.append({"type": "DELETE_REMOTE", "path": rel_path, "remote": remote})
        elif decision_type == "RESTORE_LOCAL_CHANGED":
            actions.append({"type": "PUSH", "path": rel_path, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local_info["mtimeMs"], "enabled": push})
        elif decision_type == "DELETE_LOCAL":
            actions.append({"type": "DELETE_LOCAL", "path": rel_path, "localSnapshotHash": hashlib.sha256(_vault_path(vault, rel_path).read_bytes()).hexdigest()})
        elif decision_type == "BOTH_CHANGED":
            candidates.append({"type": "BOTH_CHANGED", "path": rel_path, "remote": remote, "prev": prev, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local_info["mtimeMs"]})
        elif decision_type == "LOCAL_CHANGED":
            local_bytes = _vault_path(vault, rel_path).read_bytes()
            if prev.get("localContentHash") and hashlib.sha256(local_bytes).hexdigest() == prev["localContentHash"]:
                actions.append({"type": "SEED", "path": rel_path, "remote": remote, "localBytes": local_bytes, "reason": "mtime変化のみ・内容一致のため転送スキップ"})
            else:
                actions.append({"type": "PUSH", "path": rel_path, "remote": remote, "localBytes": local_bytes, "localMtimeMs": local_info["mtimeMs"], "enabled": push})
        elif decision_type == "REMOTE_CHANGED":
            candidates.append({"type": "REMOTE_CHANGED", "path": rel_path, "remote": remote, "localBytes": _vault_path(vault, rel_path).read_bytes(), "localMtimeMs": local_info["mtimeMs"]})
        else:
            actions.append({"type": "NOOP", "path": rel_path})

    conflict_check_finished = time.perf_counter()
    conflict_check_ms = round((conflict_check_finished - scan_finished) * 1000, 2)
    prepared_count = 0

    def fetch_candidate(candidate):
        remote_info = candidate["remote"]
        remote = fetch(remote_info["key"])
        clear = decoder.decrypt_content(remote.data)
        return candidate, remote, clear

    errors = []
    if candidates:
        _emit_progress(progress, f"R2から取得・検証しています(並列{min(fetch_concurrency, len(candidates))}件)...")
    completed = _run_parallel(
        candidates, fetch_candidate, fetch_concurrency,
        lambda done, total: _report_fetch_progress(progress, done, total),
    )
    for candidate, future in completed:
        try:
            candidate, remote_object, remote_bytes = future.result()
            prepared_count += 1
            path_name = candidate["path"]
            remote_info = candidate["remote"]
            local_bytes = candidate.get("localBytes")
            if candidate["type"] == "REMOTE_CHANGED" and remote_bytes == local_bytes:
                actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "reason": "リモート再アップロードのみ・内容一致のため転送スキップ"})
            elif candidate["type"] == "REMOTE_CHANGED":
                add_pull(path_name, remote_info, True)
                actions[-1].update({"data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest()})
            elif candidate["type"] in ("NEW_REMOTE", "RESTORE_REMOTE_CHANGED"):
                actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": False, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "reason": candidate.get("reason")})
            elif candidate["type"] == "BOOTSTRAP" and remote_bytes == local_bytes:
                actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes})
            elif candidate["type"] == "BOOTSTRAP":
                remote_mtime = _remote_mtime_ms(remote_object, remote_info)
                if candidate["localMtimeMs"] > remote_mtime and push:
                    actions.append({"type": "PUSH", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "localMtimeMs": candidate["localMtimeMs"], "enabled": True, "reason": "初回比較: 内容不一致・ローカルの方が新しいため上書き"})
                elif candidate["localMtimeMs"] > remote_mtime:
                    conflicts.append({"path": path_name, "reason": "local is newer on bootstrap and PUSH is disabled"})
                else:
                    actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": True, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": remote_mtime, "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "reason": "初回比較: 内容不一致・リモートの方が新しいため上書き"})
            else:
                if remote_bytes == local_bytes:
                    actions.append({"type": "SEED", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "reason": "両側でtouchされたが内容は一致・転送スキップ"})
                    continue
                prev = candidate["prev"]
                if merge:
                    base = _state_base_bytes(prev)
                    if base is None:
                        conflicts.append({"path": path_name, "reason": "merge base is unavailable in checkpoint state"})
                        continue
                    merged, has_conflict = three_way_merge(base, local_bytes, remote_bytes)
                    if has_conflict or merged is None:
                        conflicts.append({"path": path_name, "reason": "three-way merge conflict"})
                        continue
                    actions.append({"type": "MERGE", "path": path_name, "remote": remote_info, "data": merged, "localMtimeMs": max(candidate["localMtimeMs"], _remote_mtime_ms(remote_object, remote_info)), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "remoteBytes": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "reason": "両側変更・3-way merge"})
                elif candidate["localMtimeMs"] > _remote_mtime_ms(remote_object, remote_info) and push:
                    actions.append({"type": "PUSH", "path": path_name, "remote": remote_info, "localBytes": local_bytes, "localMtimeMs": candidate["localMtimeMs"], "enabled": True, "reason": "両側変更・ローカルの方が新しいため上書き"})
                elif candidate["localMtimeMs"] > _remote_mtime_ms(remote_object, remote_info):
                    conflicts.append({"path": path_name, "reason": "both sides changed and PUSH is disabled"})
                else:
                    actions.append({"type": "PULL", "path": path_name, "remote": remote_info, "hadLocal": True, "data": remote_bytes, "etag": remote_object.etag or remote_info.get("etag"), "mtimeMs": _remote_mtime_ms(remote_object, remote_info), "localSnapshotHash": hashlib.sha256(local_bytes).hexdigest(), "reason": "両側変更・リモートの方が新しいため上書き"})
        except Exception as error:
            errors.append({"path": candidate["path"], "error": str(error)})

    fetch_validate_ms = round((time.perf_counter() - conflict_check_finished) * 1000, 2)
    unchanged = sum(action["type"] == "NOOP" for action in actions)
    applicable_actions = [action for action in actions if action["type"] != "NOOP"]
    counts = _action_counts(applicable_actions)
    _report_action_plan(progress, actions)
    result = {
        "ok": not conflicts and not errors,
        "mode": "apply" if apply else "dry-run", "unchanged": unchanged,
        "scannedLocal": len(local), "scannedRemote": len(listed), "planned": len(applicable_actions), "validated": prepared_count,
        "applied": 0, "reconciledLocalFiles": reconciled_local_files,
        "unicodeAliasesIgnored": collision_stats["ignored"],
        "errors": errors, "conflicts": conflicts, "ignoredRemoteObjects": ignored_remote,
        "remoteSnapshotRecheckEnabled": recheck_remote_before_apply,
        "remoteSnapshotRechecked": False,
        "fetchConcurrency": fetch_concurrency,
        "applyConcurrency": apply_concurrency,
        "mergeBasesPruned": 0, "mergeBasesPendingPrune": merge_bases_to_prune,
        "plannedByType": counts, "appliedByType": {}, "skippedByType": {},
        "timingsMs": {
            "scan": scan_ms, "scanLocal": scan_local_ms, "listRemote": list_remote_ms,
            "decodeRemote": decode_remote_ms, "reconcileLocal": reconcile_local_ms,
            "conflictCheck": conflict_check_ms,
            "fetchValidate": fetch_validate_ms, "snapshotCheck": 0, "apply": 0,
            "applyRemote": 0, "applyCheckpoint": 0,
        },
    }

    entries = dict(previous)
    apply_remote_seconds = 0.0
    apply_checkpoint_seconds = 0.0
    state_dirty = merge_bases_to_prune > 0

    def mark_merge_bases_pruned() -> None:
        result["mergeBasesPruned"] += result["mergeBasesPendingPrune"]
        result["mergeBasesPendingPrune"] = 0

    def persist_entries() -> None:
        nonlocal apply_checkpoint_seconds, state_dirty
        checkpoint_started = time.perf_counter()
        try:
            save_state(state_file, entries)
        finally:
            apply_checkpoint_seconds += time.perf_counter() - checkpoint_started
        state_dirty = False
        mark_merge_bases_pruned()

    def timed_remote(call, *args):
        nonlocal apply_remote_seconds
        remote_started = time.perf_counter()
        try:
            return call(*args)
        finally:
            apply_remote_seconds += time.perf_counter() - remote_started

    def finish_apply_timing(apply_started: float) -> None:
        result["timingsMs"]["apply"] = round((time.perf_counter() - apply_started) * 1000, 2)
        result["timingsMs"]["applyRemote"] = round(apply_remote_seconds * 1000, 2)
        result["timingsMs"]["applyCheckpoint"] = round(apply_checkpoint_seconds * 1000, 2)

    def exclude_conflicted_actions(actions: list[dict], paths: set[str]) -> list[dict]:
        if not paths:
            return actions
        retained = []
        for action in actions:
            if action["path"] not in paths:
                retained.append(action)
                continue
            action_type = action["type"]
            result["skippedByType"][action_type] = result["skippedByType"].get(action_type, 0) + 1
        result["ok"] = False
        return retained

    def has_unscoped_conflict(items: list[dict]) -> bool:
        return any(item.get("path") == "<remote-list>" for item in items)

    if errors or not apply or has_unscoped_conflict(result["conflicts"]):
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    applicable_actions = exclude_conflicted_actions(
        applicable_actions,
        {item.get("path") for item in result["conflicts"] if isinstance(item.get("path"), str)},
    )

    # Recheck local state for every operation before the first remote or local mutation.
    preflight_conflict_paths = set()
    for action in applicable_actions:
        path_name = action["path"]
        target = _vault_path(vault, path_name)
        if action["type"] in ("PUSH", "MERGE", "SEED"):
            expected_hash = action.get("localSnapshotHash") or hashlib.sha256(action.get("localBytes", b"")).hexdigest()
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                result["conflicts"].append({"path": path_name, "reason": "local file changed during planning"})
                preflight_conflict_paths.add(path_name)
        elif action["type"] == "PULL":
            if action.get("hadLocal"):
                expected_hash = action.get("localSnapshotHash")
                if not expected_hash or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                    result["conflicts"].append({"path": path_name, "reason": "local file changed during fetch"})
                    preflight_conflict_paths.add(path_name)
            elif target.exists():
                result["conflicts"].append({"path": path_name, "reason": "target appeared during fetch"})
                preflight_conflict_paths.add(path_name)
        elif action["type"] == "DELETE_LOCAL" and action.get("localSnapshotHash"):
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != action["localSnapshotHash"]:
                result["conflicts"].append({"path": path_name, "reason": "local file changed during planning"})
                preflight_conflict_paths.add(path_name)
        elif action["type"] in ("DELETE_REMOTE", "FORGET") and target.exists():
            result["conflicts"].append({"path": path_name, "reason": "local file appeared during planning"})
            preflight_conflict_paths.add(path_name)
    applicable_actions = exclude_conflicted_actions(applicable_actions, preflight_conflict_paths)

    if not applicable_actions:
        _emit_progress(
            progress,
            "競合以外に適用可能な変更はありません。"
            if result["conflicts"] else "適用対象の変更はありません。",
        )
        if state_dirty:
            apply_started = time.perf_counter()
            try:
                persist_entries()
            except Exception as error:
                result["ok"] = False
                result["errors"].append({"path": "<state>", "error": str(error)})
            finish_apply_timing(apply_started)
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    # A second full listing closes the fetch/validation window. Paths changed
    # since planning are excluded while independent actions remain applicable.
    snapshot_conflict_paths = set()
    latest_conflicts = []
    if recheck_remote_before_apply:
        _emit_progress(progress, "R2 snapshotを再確認しています...")
        snapshot_started = time.perf_counter()
        try:
            latest_listed = list_remote(remote_prefix)
            latest_remotes, _, latest_conflicts = _collect_remote_objects(
                latest_listed, decoder, remote_prefix, extra_patterns, protected_paths,
                unicode_collision_policy,
            )
            result["conflicts"].extend(latest_conflicts)
            snapshot_conflict_paths.update(
                item.get("path") for item in latest_conflicts if isinstance(item.get("path"), str)
            )
            snapshot_paths = sorted(set(remotes) | set(latest_remotes))
            for path_name in snapshot_paths:
                before = remotes.get(path_name)
                after = latest_remotes.get(path_name)
                before_identity = None if before is None else (before.get("key"), before.get("etag"), before.get("size"), before.get("lastModified"))
                after_identity = None if after is None else (after.get("key"), after.get("etag"), after.get("size"), after.get("lastModified"))
                if before_identity != after_identity:
                    result["conflicts"].append({"path": path_name, "reason": "remote object changed during planning"})
                    snapshot_conflict_paths.add(path_name)
            result["remoteSnapshotRechecked"] = True
        except Exception as error:
            result["errors"].append({"path": "<remote-list>", "error": str(error)})
        finally:
            result["timingsMs"]["snapshotCheck"] = round((time.perf_counter() - snapshot_started) * 1000, 2)
    else:
        _emit_progress(progress, "警告: 適用前のR2 snapshot再確認をスキップします。")
    if result["errors"] or has_unscoped_conflict(latest_conflicts):
        result["ok"] = False
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result
    applicable_actions = exclude_conflicted_actions(applicable_actions, snapshot_conflict_paths)

    if not applicable_actions:
        _emit_progress(progress, "競合以外に適用可能な変更はありません。")
        if state_dirty:
            apply_started = time.perf_counter()
            try:
                persist_entries()
            except Exception as error:
                result["ok"] = False
                result["errors"].append({"path": "<state>", "error": str(error)})
            finish_apply_timing(apply_started)
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    apply_started = time.perf_counter()
    _emit_progress(progress, "変更を適用しています...")

    def checkpoint(path_name: str, etag: str | None, data: bytes, persist: bool = True) -> None:
        nonlocal state_dirty
        target = _vault_path(vault, path_name)
        entries[path_name] = _with_base(
            _entry_from_stat(target.stat(), etag, data), data, text_merge_base_max_bytes
        )
        state_dirty = True
        if persist:
            persist_entries()

    def bump(mapping: dict, action_type: str) -> None:
        mapping[action_type] = mapping.get(action_type, 0) + 1

    seed_actions = [action for action in applicable_actions if action["type"] == "SEED"]
    push_actions = [action for action in applicable_actions if action["type"] == "PUSH"]
    sequential_actions = [
        action for action in applicable_actions if action["type"] not in ("SEED", "PUSH")
    ]

    for action in seed_actions:
        checkpoint(
            action["path"], action["remote"].get("etag"), action["localBytes"], persist=False
        )
        bump(result["appliedByType"], "SEED")

    prepared_pushes = []
    if push:
        for action in push_actions:
            try:
                prepared_pushes.append({
                    "action": action,
                    "key": (
                        action["remote"]["key"] if action.get("remote")
                        else _remote_key(decoder, remote_prefix, action["path"])
                    ),
                    "data": decoder.encrypt_content(action["localBytes"]),
                    "contentHash": hashlib.sha256(action["localBytes"]).digest(),
                })
            except Exception as error:
                result["ok"] = False
                result["errors"].append({"path": action["path"], "error": str(error)})
    else:
        for _ in push_actions:
            bump(result["skippedByType"], "PUSH")

    if prepared_pushes and not result["errors"]:
        push_workers = min(apply_concurrency, len(prepared_pushes))
        _emit_progress(progress, f"R2へPUSHしています(並列{push_workers}件)...")

        def apply_push(spec: dict):
            action = spec["action"]
            return put(spec["key"], spec["data"], action["localMtimeMs"])

        remote_started = time.perf_counter()
        try:
            completed_pushes = _run_parallel(
                prepared_pushes, apply_push, apply_concurrency,
                lambda done, total: _report_push_progress(progress, done, total),
                wait_on_interrupt=True,
            )
        finally:
            apply_remote_seconds += time.perf_counter() - remote_started
        for spec, future in completed_pushes:
            action = spec["action"]
            try:
                etag = future.result()
            except Exception as error:
                result["ok"] = False
                result["errors"].append({"path": action["path"], "error": str(error)})
                continue
            target = _vault_path(vault, action["path"])
            source_unchanged = (
                target.is_file()
                and hashlib.sha256(target.read_bytes()).digest() == spec["contentHash"]
            )
            result["applied"] += 1
            bump(result["appliedByType"], "PUSH")
            if not source_unchanged:
                result["ok"] = False
                result["conflicts"].append({
                    "path": action["path"], "reason": "local file changed during push"
                })
                continue
            checkpoint(action["path"], etag, action["localBytes"], persist=False)

    processed_count = len(seed_actions) + len(push_actions)
    if processed_count:
        _emit_progress(progress, f"  適用中: {processed_count}/{len(applicable_actions)}")

    should_checkpoint_batch = state_dirty and (
        bool(prepared_pushes) or bool(result["errors"]) or not sequential_actions
    )
    if should_checkpoint_batch:
        try:
            persist_entries()
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": "<state>", "error": str(error)})
    if result["errors"]:
        finish_apply_timing(apply_started)
        result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    for action in sequential_actions:
        processed_count += 1
        path_name = action["path"]
        target = _vault_path(vault, path_name)
        try:
            action_type = action["type"]
            if action_type == "PULL":
                atomic_replace(target, action["data"], action["mtimeMs"])
                checkpoint(path_name, action["etag"], action["data"])
                result["applied"] += 1
                bump(result["appliedByType"], action_type)
            elif action_type == "MERGE":
                atomic_replace(target, action["data"], action["localMtimeMs"])
                etag = timed_remote(
                    put, action["remote"]["key"], decoder.encrypt_content(action["data"]), action["localMtimeMs"]
                )
                checkpoint(path_name, etag, action["data"])
                result["applied"] += 1
                bump(result["appliedByType"], action_type)
            elif action_type == "DELETE_REMOTE":
                if allow_delete:
                    timed_remote(remove, action["remote"]["key"])
                    entries.pop(path_name, None)
                    state_dirty = True
                    persist_entries()
                    result["applied"] += 1
                    bump(result["appliedByType"], action_type)
                else:
                    bump(result["skippedByType"], action_type)
            elif action_type == "DELETE_LOCAL":
                if allow_delete:
                    target.unlink()
                    entries.pop(path_name, None)
                    state_dirty = True
                    persist_entries()
                    result["applied"] += 1
                    bump(result["appliedByType"], action_type)
                else:
                    bump(result["skippedByType"], action_type)
            elif action_type == "FORGET":
                entries.pop(path_name, None)
                state_dirty = True
                persist_entries()
                bump(result["appliedByType"], action_type)
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": path_name, "error": str(error)})
            _emit_progress(progress, f"  適用中: {processed_count}/{len(applicable_actions)}")
            break
        if processed_count % 50 == 0 or processed_count == len(applicable_actions):
            _emit_progress(progress, f"  適用中: {processed_count}/{len(applicable_actions)}")
    if state_dirty and not result["errors"]:
        try:
            persist_entries()
        except Exception as error:
            result["ok"] = False
            result["errors"].append({"path": "<state>", "error": str(error)})
    finish_apply_timing(apply_started)
    result["timingsMs"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    return result
