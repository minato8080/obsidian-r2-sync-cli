"""Pure synchronization decisions and three-way text merging."""

from __future__ import annotations

import difflib


def _diff_hunks(base_lines: list[str], variant_lines: list[str]) -> list[dict]:
    return [
        {"start": start, "end": end, "replacement": variant_lines[new_start:new_end]}
        for tag, start, end, new_start, new_end in difflib.SequenceMatcher(None, base_lines, variant_lines, autojunk=False).get_opcodes()
        if tag != "equal"
    ]


def _hunks_overlap(left: dict, right: dict) -> bool:
    if left["start"] == left["end"] == right["start"] == right["end"]:
        return True
    if left["start"] == left["end"]:
        return right["start"] <= left["start"] <= right["end"]
    if right["start"] == right["end"]:
        return left["start"] <= right["start"] <= left["end"]
    return max(left["start"], right["start"]) < min(left["end"], right["end"])


def _render_hunks(base_lines: list[str], start: int, end: int, hunks: list[dict]) -> list[str]:
    output = []
    cursor = start
    for hunk in sorted(hunks, key=lambda value: (value["start"], value["end"])):
        output.extend(base_lines[cursor:hunk["start"]])
        output.extend(hunk["replacement"])
        cursor = hunk["end"]
    output.extend(base_lines[cursor:end])
    return output


def three_way_merge(base: bytes, local: bytes, remote: bytes) -> tuple[bytes | None, bool]:
    """Merge UTF-8 line changes; return ``(None, True)`` on a real conflict."""
    try:
        base_lines = base.decode("utf-8").splitlines(keepends=True)
        local_lines = local.decode("utf-8").splitlines(keepends=True)
        remote_lines = remote.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None, True
    local_hunks = _diff_hunks(base_lines, local_lines)
    remote_hunks = _diff_hunks(base_lines, remote_lines)
    all_hunks = [("local", hunk) for hunk in local_hunks] + [("remote", hunk) for hunk in remote_hunks]
    groups = []
    ungrouped = list(all_hunks)
    while ungrouped:
        group = [ungrouped.pop(0)]
        expanded = True
        while expanded:
            expanded = False
            remaining = []
            for candidate in ungrouped:
                if any(_hunks_overlap(candidate[1], existing[1]) for existing in group):
                    group.append(candidate)
                    expanded = True
                else:
                    remaining.append(candidate)
            ungrouped = remaining
        groups.append(group)

    output = []
    cursor = 0
    for group in sorted(groups, key=lambda value: (min(item[1]["start"] for item in value), min(item[1]["end"] for item in value))):
        start = min(item[1]["start"] for item in group)
        end = max(item[1]["end"] for item in group)
        output.extend(base_lines[cursor:start])
        local_text = _render_hunks(base_lines, start, end, [hunk for side, hunk in group if side == "local"])
        remote_text = _render_hunks(base_lines, start, end, [hunk for side, hunk in group if side == "remote"])
        if local_text == remote_text:
            output.extend(local_text)
        elif not any(side == "local" for side, _ in group):
            output.extend(remote_text)
        elif not any(side == "remote" for side, _ in group):
            output.extend(local_text)
        else:
            def marked(label: str, lines: list[str]) -> list[str]:
                text = "".join(lines)
                if text and not text.endswith("\n"):
                    text += "\n"
                return [f"{label}\n", text, "=======\n"] if label == "<<<<<<< LOCAL" else [text, f"{label}\n"]

            output.extend(marked("<<<<<<< LOCAL", local_text))
            output.extend(marked(">>>>>>> REMOTE", remote_text))
            return "".join(output + base_lines[end:]).encode("utf-8"), True
        cursor = end
    output.extend(base_lines[cursor:])
    return "".join(output).encode("utf-8"), False
def _action_counts(actions: list[dict]) -> dict[str, int]:
    counts = {}
    for action in actions:
        counts[action["type"]] = counts.get(action["type"], 0) + 1
    return counts

def classify_sync_path(rel_path: str, local: dict | None, remote: dict | None, previous: dict | None) -> dict:
    """Classify one path using only scan metadata and checkpoint state.

    Content-dependent cases are returned as candidates for the executor to
    resolve after reading or fetching bytes.
    """
    has_local = local is not None
    if not previous:
        if has_local and remote:
            return {"type": "BOOTSTRAP", "path": rel_path}
        if has_local:
            return {"type": "NEW_LOCAL", "path": rel_path}
        return {"type": "NEW_REMOTE", "path": rel_path}

    local_gone = not has_local and previous.get("localMtimeMs") is not None
    remote_gone = remote is None and previous.get("remoteETag") is not None
    local_changed = False
    if has_local:
        size_changed = previous.get("localSize") is not None and local["size"] != previous["localSize"]
        mtime_changed = (
            previous.get("localMtimeMs") is not None
            and abs(local["mtimeMs"] - float(previous["localMtimeMs"])) > 1.0
        )
        local_changed = size_changed or mtime_changed
    remote_changed = bool(remote and remote.get("etag") != previous.get("remoteETag"))

    if local_gone and remote_gone:
        action_type = "FORGET"
    elif local_gone:
        action_type = "RESTORE_REMOTE_CHANGED" if remote_changed else "DELETE_REMOTE"
    elif remote_gone:
        action_type = "RESTORE_LOCAL_CHANGED" if local_changed else "DELETE_LOCAL"
    elif local_changed and remote_changed:
        action_type = "BOTH_CHANGED"
    elif local_changed:
        action_type = "LOCAL_CHANGED"
    elif remote_changed:
        action_type = "REMOTE_CHANGED"
    else:
        action_type = "NOOP"
    return {"type": action_type, "path": rel_path}
