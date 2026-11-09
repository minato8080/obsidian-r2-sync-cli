from support import *

from r2sync.checkpoint import save_state
from r2sync.manifest import build_manifest, content_fingerprint


class ManifestTests(unittest.TestCase):
    def test_matching_manifest_seeds_without_remote_body_get(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            content = b"same"
            (vault / "note.md").write_bytes(content)
            manifest_key = "separate-key"
            remote = FakeRemote({
                "note.md": RemoteObject(content, "e1", {}),
                ".r2-sync-manifest.json": RemoteObject(build_manifest({"note.md": {"etag": "e1", "size": len(content), "fingerprint": content_fingerprint(content, manifest_key)}}), "manifest-1", {}),
            })
            result = execute_full_sync(
                vault, Path(root) / "state.json", remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=False, push=True, manifest_object_key=".r2-sync-manifest.json", manifest_key=manifest_key,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plannedByType"], {"SEED": 1})
            self.assertEqual(remote.calls, [".r2-sync-manifest.json"])
            self.assertFalse(result["manifestUpdated"])

    def test_etag_mismatch_falls_back_to_body_comparison(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            content = b"same"
            (vault / "note.md").write_bytes(content)
            remote = FakeRemote({
                "note.md": RemoteObject(content, "new-etag", {}),
                ".r2-sync-manifest.json": RemoteObject(build_manifest({"note.md": {"etag": "old-etag", "size": len(content), "fingerprint": content_fingerprint(content, "separate-key")}}), "manifest-1", {}),
            })
            result = execute_full_sync(
                vault, Path(root) / "state.json", remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=False, push=True, manifest_object_key=".r2-sync-manifest.json", manifest_key="separate-key",
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(remote.calls, [".r2-sync-manifest.json", "note.md"])

    def test_apply_writes_manifest_but_dry_run_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            content = b"same"
            (vault / "note.md").write_bytes(content)
            remote = FakeRemote({"note.md": RemoteObject(content, "e1", {})})
            state = Path(root) / "state.json"
            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=False, push=True, manifest_object_key=".r2-sync-manifest.json", manifest_key="separate-key",
            )
            self.assertTrue(result["ok"], result)
            self.assertNotIn(".r2-sync-manifest.json", remote.objects)
            self.assertFalse(state.exists())
            remote.calls.clear()
            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, manifest_object_key=".r2-sync-manifest.json", manifest_key="separate-key",
            )
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["manifestUpdated"], result)
            self.assertIn(".r2-sync-manifest.json", remote.objects)
            parsed = json.loads(remote.objects[".r2-sync-manifest.json"].data)
            self.assertEqual(parsed["entries"]["note.md"]["fingerprint"], content_fingerprint(content, "separate-key"))
            self.assertNotIn(hashlib.sha256(content).hexdigest(), remote.objects[".r2-sync-manifest.json"].data.decode())

    def test_conflict_copy_is_recorded_in_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            state = Path(root) / "state.json"
            key = "separate-key"
            options = dict(apply=True, push=True, merge=True, manifest_object_key=".r2-sync-manifest.json", manifest_key=key)
            (vault / "note.md").write_bytes(b"base\n")
            remote = FakeRemote({"note.md": RemoteObject(b"base\n", "e1", {})})
            first = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), **options)
            self.assertTrue(first["ok"], first)

            (vault / "note.md").write_bytes(b"local\n")
            remote.objects["note.md"] = RemoteObject(b"remote\n", "e2", {"mtime": "1700000000"})
            result = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), **options)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["appliedByType"], {"CONFLICT_COPY": 1})
            self.assertTrue(result["manifestUpdated"], result)

            entries = json.loads(remote.objects[".r2-sync-manifest.json"].data)["entries"]
            copies = [name for name in entries if name.startswith("note (conflict remote ")]
            self.assertEqual(len(copies), 1)
            self.assertEqual(set(entries), {"note.md", copies[0]})
            self.assertEqual(entries["note.md"]["etag"], remote.objects["note.md"].etag)
            self.assertEqual(entries["note.md"]["fingerprint"], content_fingerprint(b"local\n", key))
            self.assertEqual(entries[copies[0]]["fingerprint"], content_fingerprint(b"remote\n", key))

            # The next run on a fresh state is answered from the manifest without body GETs.
            state.unlink()
            remote.calls.clear()
            again = execute_full_sync(vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(), **{**options, "apply": False})
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["plannedByType"], {"SEED": 2})
            self.assertEqual(remote.calls, [".r2-sync-manifest.json"])

    def test_valid_manifest_and_noop_apply_does_not_rewrite_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            content = b"same"
            target = vault / "note.md"
            target.write_bytes(content)
            state = Path(root) / "state.json"
            stat = target.stat()
            save_state(state, {"note.md": {"localMtimeMs": stat.st_mtime_ns / 1_000_000, "localSize": len(content), "remoteETag": "e1", "localContentHash": hashlib.sha256(content).hexdigest()}})
            key = "separate-key"
            remote = FakeRemote({
                "note.md": RemoteObject(content, "e1", {}),
                ".r2-sync-manifest.json": RemoteObject(build_manifest({"note.md": {"etag": "e1", "size": len(content), "fingerprint": content_fingerprint(content, key)}}), "manifest-1", {}),
            })
            result = execute_full_sync(
                vault, state, remote.list, remote.get, remote.put, remote.delete,
                PlainContent(), apply=True, push=True, manifest_object_key=".r2-sync-manifest.json", manifest_key=key,
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["planned"], 0)
            self.assertFalse(result["manifestUpdated"])
            self.assertEqual(remote.calls, [".r2-sync-manifest.json"])
