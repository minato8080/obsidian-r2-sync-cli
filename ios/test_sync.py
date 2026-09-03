import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
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
        _poly1305,
        _xsalsa_stream,
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
    _poly1305,
    _xsalsa_stream,
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
