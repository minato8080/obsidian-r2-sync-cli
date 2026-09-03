import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

try:
    from . import sync as pull_module
    from .sync import (
        MAGIC,
        PlainContent,
        RemoteObject,
        R2Client,
        RcloneBase64,
        execute_full_sync,
        _aes_block,
        _config_relative_path,
        _ignore_base_rel_path,
        _ignore_matcher,
        _report_result,
        _poly1305,
        _xsalsa_stream,
        _scan_vault,
        execute_full,
        execute_probe,
        load_state,
    )
except ImportError:
    import sync as pull_module
    from sync import (
    MAGIC,
    PlainContent,
    RemoteObject,
    R2Client,
    RcloneBase64,
    execute_full_sync,
    _aes_block,
    _config_relative_path,
    _ignore_base_rel_path,
    _ignore_matcher,
    _report_result,
    _poly1305,
    _xsalsa_stream,
    _scan_vault,
    execute_full,
    execute_probe,
    load_state,
    )


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


class PullProbeTests(unittest.TestCase):
    def test_full_sync_reports_node_style_plan_and_progress(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({"note.md": RemoteObject(b"remote", "e1", {})})
            messages = []

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, progress=messages.append,
            )
            _report_result(messages.append, result)
            rendered = "\n".join(messages)

            self.assertTrue(result["ok"], result)
            self.assertIn("local files: 0件 / remote objects: 1件 / 前回状態: 0件", rendered)
            self.assertIn("取得・検証中: 1/1", rendered)
            self.assertIn("[PULL] 1件", rendered)
            self.assertIn("  note.md", rendered)
            self.assertIn("R2 snapshotを再確認しています...", rendered)
            self.assertIn("適用中: 1/1", rendered)
            self.assertIn("=== 実行結果 ===", rendered)
            self.assertIn("PULL: 1", rendered)

    def test_full_sync_does_not_apply_or_checkpoint_noops(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({"note.md": RemoteObject(b"same", "e1", {})})
            first = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )
            self.assertTrue(first["ok"], first)
            original_state = state.read_bytes()
            list_remote = mock.Mock(side_effect=remote.list)
            messages = []

            result = execute_full_sync(
                vault, state, list_remote, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, progress=messages.append,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["unchanged"], 1)
            self.assertEqual(result["planned"], 0)
            self.assertEqual(result["applied"], 0)
            self.assertEqual(result["plannedByType"], {})
            self.assertEqual(result["appliedByType"], {})
            self.assertEqual(list_remote.call_count, 1)
            self.assertEqual(state.read_bytes(), original_state)
            rendered = "\n".join(messages)
            self.assertIn("[NOOP] 1件", rendered)
            self.assertIn("適用対象の変更はありません。", rendered)
            self.assertNotIn("変更を適用しています", rendered)
            self.assertNotIn("適用中:", rendered)

    def test_main_keeps_json_on_stdout_and_progress_on_stderr(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            vault = root_path / "vault"
            vault.mkdir()
            config = root_path / "config.json"
            config.write_text(json.dumps({
                "vaultPath": str(vault),
                "statePath": "state.json",
                "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                "bucket": "<bucket-name>",
                "accessKeyId": "key",
                "secretAccessKey": "secret",
                "encryption": "plain",
                "mode": "full",
            }), encoding="utf-8")
            remote = FakeRemote({})
            client = mock.Mock(
                list_all=remote.list,
                get_object=remote.get,
                put_object=remote.put,
                delete_object=remote.delete,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(pull_module, "R2Client", return_value=client):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = pull_module.main(["--config", str(config)])

            output_lines = stdout.getvalue().splitlines()
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(output_lines), 1)
            self.assertTrue(json.loads(output_lines[0])["ok"])
            self.assertIn("vault:", stderr.getvalue())
            self.assertIn("mode: DRY-RUN", stderr.getvalue())
            self.assertIn("dry-runのため実際の変更は行っていません", stderr.getvalue())
            self.assertIn("=== 実行結果 ===", stderr.getvalue())

    def test_local_deleted_remote_changed_pull_has_payload(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state.json"
            state.write_text(json.dumps({"version": 1, "entries": {
                "note.md": {"localMtimeMs": 1, "localSize": 3, "remoteETag": "old"}
            }}), encoding="utf-8")
            remote = FakeRemote({"note.md": RemoteObject(b"remote-new", "new", {})})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual((vault / "note.md").read_bytes(), b"remote-new")

    def test_new_remote_aborts_if_target_appears_during_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state.json"
            remote = FakeRemote({"note.md": RemoteObject(b"remote", "e1", {})})

            def fetch(key):
                (vault / "note.md").write_bytes(b"concurrent-local")
                return remote.get(key)

            result = execute_full_sync(
                vault, state, remote.list, fetch, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )

            self.assertFalse(result["ok"], result)
            self.assertEqual((vault / "note.md").read_bytes(), b"concurrent-local")

    def test_vault_root_config_uses_empty_ignore_base(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            config = vault / "config.json"
            config.write_text("{}", encoding="utf-8")
            (vault / "keep.md").write_text("keep", encoding="utf-8")
            base = _ignore_base_rel_path(vault, config)
            self.assertEqual(base, "")
            self.assertEqual(_scan_vault(vault, ["/**"], base), {})

    def test_both_gone_path_is_forgotten_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state.json"
            state.write_text(json.dumps({"version": 1, "entries": {
                "gone.md": {"localMtimeMs": 1, "localSize": 4, "remoteETag": "old"}
            }}), encoding="utf-8")
            remote = FakeRemote({})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plannedByType"].get("FORGET"), 1)
            self.assertNotIn("gone.md", load_state(state))

    def test_state_and_checkpoint_temp_are_protected(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = vault / "tools" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
            orphan = state.parent / ".state.json.interrupted.tmp"
            orphan.write_text("checkpoint fragment", encoding="utf-8")
            remote = FakeRemote({"tools/state.json": RemoteObject(b"remote-state", "state-etag", {})})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, allow_delete=True,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["planned"], 0)
            self.assertEqual(set(remote.objects), {"tools/state.json"})

    def test_config_is_protected_without_ignore_pattern(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            config = vault / "tools" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text("local config", encoding="utf-8")
            state = Path(root) / "state.json"
            remote = FakeRemote({"tools/config.json": RemoteObject(b"remote config", "config-etag", {})})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, allow_delete=True,
                extra_protected_paths=["tools/config.json"],
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["planned"], 0)
            self.assertEqual(config.read_text(encoding="utf-8"), "local config")
            self.assertEqual(remote.objects["tools/config.json"].data, b"remote config")

    def test_state_path_is_config_relative_with_legacy_existing_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            config = root_path / "tools" / "config.json"
            expected = config.parent / "state.json"
            self.assertEqual(_config_relative_path("state.json", config), expected.resolve())
            absolute = (root_path / "absolute-state.json").resolve()
            self.assertEqual(_config_relative_path(absolute, config), absolute)
            previous_cwd = Path.cwd()
            try:
                os.chdir(root_path)
                legacy = root_path / "legacy-state.json"
                legacy.write_text("{}", encoding="utf-8")
                self.assertEqual(_config_relative_path("legacy-state.json", config), legacy.resolve())
            finally:
                os.chdir(previous_cwd)

    def test_gitignore_negated_character_class(self):
        _, ignored_file = _ignore_matcher(["**/secret[!0].md"], "tools")
        self.assertFalse(ignored_file("tools/secret0.md"))
        self.assertTrue(ignored_file("tools/secret1.md"))
        self.assertTrue(ignored_file("tools/nested/secret2.md"))

    def test_remote_change_after_initial_list_aborts_before_push(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state.json"
            target = vault / "note.md"
            target.write_bytes(b"base")
            remote = FakeRemote({"note.md": RemoteObject(b"base", "base-etag", {})})
            self.assertTrue(execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )["ok"])
            target.write_bytes(b"local-new")

            def stale_list(prefix=""):
                snapshot = remote.list(prefix)
                remote.objects["note.md"] = RemoteObject(b"concurrent-remote", "concurrent-etag", {})
                return snapshot

            result = execute_full_sync(
                vault, state, stale_list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True,
            )

            self.assertFalse(result["ok"], result)
            self.assertEqual(remote.objects["note.md"].data, b"concurrent-remote")

    def test_config_relative_ignore_globs(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            (vault / "tools").mkdir(parents=True)
            for rel_path in (
                "tools/sync.py", "tools/r2-sync-config.json", "tools/nested/sync.py", "tools/nested/r2-sync-config.json",
                "tools/generated/note.md", "tools/generated/config.json", "tools/generated/deep/config.json",
                "tools/nested/secret1.md", "notes/secret1.md", "keep.md",
            ):
                target = vault / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rel_path, encoding="utf-8")
            files = _scan_vault(vault, ["/sync.py", "/r2-sync-config.json", "/generated/"], "tools")
            self.assertEqual(sorted(files), [
                "keep.md", "notes/secret1.md", "tools/nested/r2-sync-config.json", "tools/nested/secret1.md", "tools/nested/sync.py",
            ])
            any_depth = _scan_vault(vault, ["sync.py"], "tools")
            self.assertNotIn("tools/sync.py", any_depth)
            self.assertNotIn("tools/nested/sync.py", any_depth)

            all_tool_files = _scan_vault(vault, ["/**"], "tools")
            self.assertEqual(sorted(all_tool_files), ["keep.md", "notes/secret1.md"])

            globbed = _scan_vault(vault, ["/generated/*.json", "**/secret?.md"], "tools")
            self.assertNotIn("tools/generated/config.json", globbed)
            self.assertIn("tools/generated/deep/config.json", globbed)

    def test_external_config_keeps_legacy_vault_root_base(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            (vault / "nested").mkdir(parents=True)
            (vault / "sync.py").write_text("root", encoding="utf-8")
            (vault / "nested/sync.py").write_text("nested", encoding="utf-8")
            files = _scan_vault(vault, ["./sync.py"])
            self.assertNotIn("sync.py", files)
            self.assertIn("nested/sync.py", files)

    def test_ignored_file_in_old_state_never_reenters_full_sync(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            tool_dir = vault / "ios"
            tool_dir.mkdir(parents=True)
            (tool_dir / "config.json").write_text("local config", encoding="utf-8")
            state = Path(root) / "state.json"
            state.write_text(json.dumps({"version": 1, "entries": {
                "ios/config.json": {"localMtimeMs": 1, "localSize": 12, "remoteETag": "old"}
            }}), encoding="utf-8")
            remote = FakeRemote({})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(),
                extra_patterns=["/**"], ignore_base_rel_path="ios", apply=True, push=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["planned"], 0)
            self.assertEqual(remote.objects, {})
            self.assertEqual((tool_dir / "config.json").read_text(encoding="utf-8"), "local config")

    def test_state_inside_vault_is_protected_without_ignore_pattern(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            tool_dir = vault / "ios"
            tool_dir.mkdir(parents=True)
            state = tool_dir / "r2-sync-state.json"
            state.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
            remote = FakeRemote({})

            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(),
                apply=True, push=True,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["planned"], 0)
            self.assertEqual(remote.objects, {})

    def test_r2_list_all_parses_and_pages_s3_xml(self):
        client = R2Client("https://<account-id>.r2.cloudflarestorage.com", "<bucket-name>", "key", "secret")
        calls = []
        pages = [
            b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Contents><Key>one</Key><ETag>&quot;e1&quot;</ETag><Size>3</Size><LastModified>2023-11-14T22:13:20.000Z</LastModified></Contents><IsTruncated>true</IsTruncated><NextContinuationToken>next-token</NextContinuationToken></ListBucketResult>''',
            b'''<ListBucketResult><Contents><Key>two</Key><ETag>e2</ETag><Size>4</Size></Contents><IsTruncated>false</IsTruncated></ListBucketResult>''',
        ]

        def fake_request(method, key, query):
            calls.append((method, key, query))
            return pages[len(calls) - 1], None

        client._request = fake_request
        self.assertEqual(client.list_all("prefix/"), [
            {"key": "one", "etag": '"e1"', "size": 3, "lastModified": "2023-11-14T22:13:20.000Z"},
            {"key": "two", "etag": "e2", "size": 4, "lastModified": None},
        ])
        self.assertEqual(calls[0][2], {"list-type": "2", "prefix": "prefix/"})
        self.assertEqual(calls[1][2], {"list-type": "2", "prefix": "prefix/", "continuation-token": "next-token"})

    def test_aes_and_filename_roundtrip(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(_aes_block(plain, key).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(_aes_block(_aes_block(plain, key), key, True), plain)

        cipher = RcloneBase64("test-password")
        name = "深い階層/note 日本語.md"
        self.assertEqual(cipher.decrypt_path(cipher.encrypt_path(name)), name)

    def test_rclone_content_roundtrip_and_authentication(self):
        cipher = RcloneBase64("test-password")
        node_vector = bytes.fromhex(
            "52434c4f4e450000000102030405060708090a0b0c0d0e0f"
            "1011121314151617"
            "1d840608f1e4c1ee2dc861e09e08b19eca3219482673c21456f82d579c4e62e60c6f452aff0b"
        )
        self.assertEqual(cipher.decrypt_content(node_vector), bytes.fromhex("e697a5e69cace8aa9ee381ae4d61726b646f776e5c6e"))
        clear = ("日本語のMarkdown\n" * 20).encode("utf-8")
        encrypted = encrypt_content_for_test(clear, cipher, bytes(range(24)))
        self.assertEqual(cipher.decrypt_content(encrypted), clear)
        self.assertEqual(cipher.decrypt_content(cipher.encrypt_content(clear)), clear)
        tampered = bytearray(encrypted)
        tampered[-1] ^= 1
        with self.assertRaises(Exception):
            cipher.decrypt_content(bytes(tampered))

    def test_filename_matches_node_vector(self):
        cipher = RcloneBase64("test-password")
        self.assertEqual(cipher.encrypt_path("深い階層/note 日本語.md"), "-onHdzFTWqw_CLt0KW16rw/GcYc3DaSITf6jaTJoLooEyruftqJY7KDGYnUwQy3MrA")

    def test_apply_is_atomic_and_restores_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "app-data" / "state.json"
            remote = FakeRemote({"note": RemoteObject("新しい内容\n".encode(), '"etag-1"', {"mtime": "1700000000"})})
            result = execute_probe(vault, state, [{"path": "深い/note.md", "key": "note"}], remote.get, PlainContent(), apply=True)
            self.assertTrue(result["ok"])
            target = vault / "深い" / "note.md"
            self.assertEqual(target.read_text(encoding="utf-8"), "新しい内容\n")
            self.assertAlmostEqual(target.stat().st_mtime, 1700000000, delta=0.01)
            entries = load_state(state)
            self.assertEqual(entries["深い/note.md"]["remoteETag"], '"etag-1"')
            self.assertEqual(entries["深い/note.md"]["localContentHash"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertFalse(any(path.name.startswith(".r2-sync-") for path in target.parent.iterdir()))

    def test_fetch_or_decode_error_does_not_change_any_file(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            target = vault / "old.md"
            target.write_text("old", encoding="utf-8")
            old_stat = target.stat()
            remote = FakeRemote({
                "good": RemoteObject(b"good", "good", {"mtime": "1700000000"}),
                "bad": ValueError("network down"),
            })
            result = execute_probe(vault, Path(root) / "state.json", [{"path": "new.md", "key": "good"}, {"path": "other.md", "key": "bad"}], remote.get, PlainContent(), apply=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["applied"], 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(target.stat().st_mtime_ns, old_stat.st_mtime_ns)
            self.assertFalse((vault / "new.md").exists())

    def test_conflict_aborts_before_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            target = vault / "note.md"
            target.write_text("edited", encoding="utf-8")
            state = Path(root) / "state.json"
            state.write_text(json.dumps({"version": 1, "entries": {"note.md": {"localMtimeMs": 1, "localSize": 3}}}), encoding="utf-8")
            remote = FakeRemote({"note": RemoteObject(b"remote", "etag", {"mtime": "1700000000"})})
            result = execute_probe(vault, state, [{"path": "note.md", "key": "note"}], remote.get, PlainContent(), apply=True)
            self.assertFalse(result["ok"])
            self.assertEqual(remote.calls, [])
            self.assertEqual(target.read_text(encoding="utf-8"), "edited")

    def test_full_scan_pulls_all_objects_and_ignores_generic_files(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({
                "notes/a.md": RemoteObject(b"A\n", "a", {"mtime": "1700000000"}),
                "deep/b.bin": RemoteObject(b"\x00\x01", "b", {"mtime": "1700000001"}),
                ".git/config": RemoteObject(b"should not be pulled", "ignored", {}),
            })
            result = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["scannedRemote"], 3)
            self.assertEqual(result["applied"], 2)
            self.assertEqual((vault / "notes/a.md").read_bytes(), b"A\n")
            self.assertEqual((vault / "deep/b.bin").read_bytes(), b"\x00\x01")
            self.assertFalse((vault / ".git/config").exists())
            self.assertAlmostEqual((vault / "notes/a.md").stat().st_mtime, 1700000000, delta=0.01)
            self.assertEqual(load_state(state)["notes/a.md"]["remoteETag"], "a")

    def test_full_fetch_failure_leaves_everything_unmodified(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            old = vault / "old.md"
            old.write_text("old", encoding="utf-8")
            remote = FakeRemote({
                "new.md": RemoteObject(b"new", "new", {}),
                "broken.md": RuntimeError("network down"),
            })
            result = execute_full(vault, Path(root) / "state.json", remote.list, remote.get, PlainContent(), apply=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["applied"], 0)
            self.assertEqual(old.read_text(encoding="utf-8"), "old")
            self.assertFalse((vault / "new.md").exists())

    def test_full_conflict_aborts_before_any_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            target = vault / "note.md"
            target.write_text("edited", encoding="utf-8")
            stat = target.stat()
            state = Path(root) / "state.json"
            state.write_text(json.dumps({"version": 1, "entries": {"note.md": {
                "localMtimeMs": (stat.st_mtime_ns / 1_000_000) - 1000,
                "localSize": 3,
                "remoteETag": "old",
            }}}), encoding="utf-8")
            remote = FakeRemote({"note.md": RemoteObject(b"remote", "new", {})})
            result = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertFalse(result["ok"])
            self.assertEqual(remote.calls, [])
            self.assertEqual(target.read_text(encoding="utf-8"), "edited")

    def test_full_bootstrap_seeds_equal_content_and_rejects_different_content(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            equal = vault / "equal.md"
            equal.write_text("same", encoding="utf-8")
            different = vault / "different.md"
            different.write_text("local", encoding="utf-8")
            remote = FakeRemote({
                "equal.md": RemoteObject(b"same", "equal", {}),
                "different.md": RemoteObject(b"remote", "different", {}),
            })
            result = execute_full(vault, Path(root) / "state.json", remote.list, remote.get, PlainContent(), apply=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["applied"], 0)
            self.assertEqual(result["seeded"], 0)
            self.assertEqual(equal.read_text(encoding="utf-8"), "same")
            self.assertEqual(different.read_text(encoding="utf-8"), "local")

    def test_full_pull_does_not_delete_local_file_when_remote_object_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({"keep.md": RemoteObject(b"keep", "keep", {})})
            first = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertTrue(first["ok"])
            remote.objects = {}
            second = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertTrue(second["ok"])
            self.assertEqual(second["ignoredRemoteDeletes"], 1)
            self.assertEqual((vault / "keep.md").read_text(encoding="utf-8"), "keep")

    def test_full_apply_checkpoints_success_before_later_write_failure(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({
                "first.md": RemoteObject(b"first", "first", {}),
                "second.md": RemoteObject(b"second", "second", {}),
            })
            original = pull_module.atomic_replace

            def fail_second(target, data, mtime_ms):
                if target.name == "second.md":
                    raise OSError("simulated stop")
                return original(target, data, mtime_ms)

            with mock.patch.object(pull_module, "atomic_replace", side_effect=fail_second):
                failed = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["applied"], 1)
            self.assertEqual((vault / "first.md").read_text(encoding="utf-8"), "first")
            self.assertFalse((vault / "second.md").exists())
            self.assertIn("first.md", load_state(state))
            self.assertNotIn("second.md", load_state(state))

            resumed = execute_full(vault, state, remote.list, remote.get, PlainContent(), apply=True)
            self.assertTrue(resumed["ok"])
            self.assertEqual(resumed["applied"], 1)
            self.assertEqual((vault / "second.md").read_text(encoding="utf-8"), "second")

    def test_full_sync_pushes_new_local_file(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            (vault / "local.md").write_text("from local", encoding="utf-8")
            remote = FakeRemote({})
            cipher = RcloneBase64("test-password")
            result = execute_full_sync(vault, Path(root) / "state.json", remote.list, remote.get, remote.put, remote.delete, cipher, apply=True, push=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["plannedByType"].get("PUSH"), 1)
            remote_key = cipher.encrypt_path("local.md")
            self.assertIn(remote_key, remote.objects)
            self.assertEqual(cipher.decrypt_content(remote.objects[remote_key].data), b"from local")
            self.assertIn("local.md", load_state(Path(root) / "state.json"))

    def test_full_sync_pulls_new_remote_file_with_payload(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            remote = FakeRemote({"remote.md": RemoteObject(b"from remote", "remote", {})})

            result = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True)

            self.assertTrue(result["ok"])
            self.assertEqual(result["plannedByType"].get("PULL"), 1)
            self.assertEqual((vault / "remote.md").read_bytes(), b"from remote")
            self.assertIn("remote.md", load_state(state))

    def test_full_sync_applies_remote_and_local_deletes_only_when_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            (vault / "gone-remote.md").parent.mkdir(parents=True)
            (vault / "gone-remote.md").write_text("remote delete", encoding="utf-8")
            (vault / "gone-local.md").write_text("local delete", encoding="utf-8")
            remote = FakeRemote({})
            self.assertTrue(execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True)["ok"])

            (vault / "gone-remote.md").unlink()
            first = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, allow_delete=True, push=True)
            self.assertTrue(first["ok"])
            self.assertNotIn("gone-remote.md", remote.objects)

            remote.objects.pop("gone-local.md")
            second = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, allow_delete=True, push=True)
            self.assertTrue(second["ok"])
            self.assertFalse((vault / "gone-local.md").exists())

    def test_full_sync_three_way_merge_combines_non_overlapping_text_changes(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            vault.mkdir()
            target = vault / "note.md"
            target.write_bytes(b"one\nbase\nthree\n")
            remote = FakeRemote({})
            self.assertTrue(execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True)["ok"])

            target.write_bytes(b"ONE\nbase\nthree\n")
            remote.objects["note.md"] = RemoteObject(b"one\nbase\nTHREE\n", "remote-new", {})
            result = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True, merge=True)
            self.assertTrue(result["ok"])
            expected = "ONE\nbase\nTHREE\n"
            self.assertEqual(target.read_text(encoding="utf-8"), expected)
            self.assertEqual(remote.objects["note.md"].data, expected.encode())

    def test_full_sync_merge_conflict_aborts_without_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            state = Path(root) / "state.json"
            vault.mkdir()
            target = vault / "note.md"
            target.write_text("base\n", encoding="utf-8")
            remote = FakeRemote({})
            self.assertTrue(execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True)["ok"])

            target.write_text("local\n", encoding="utf-8")
            remote.objects["note.md"] = RemoteObject(b"remote\n", "remote-new", {})
            before_remote = remote.objects["note.md"].data
            result = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), apply=True, push=True, merge=True)
            self.assertFalse(result["ok"])
            self.assertTrue(result["conflicts"])
            self.assertEqual(target.read_text(encoding="utf-8"), "local\n")
            self.assertEqual(remote.objects["note.md"].data, before_remote)


if __name__ == "__main__":
    unittest.main()
