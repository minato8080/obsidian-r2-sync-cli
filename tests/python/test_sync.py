from support import *


class SyncWorkflowTests(unittest.TestCase):
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

                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("NOOP content was read")):
                    with mock.patch.object(executor_module, "_vault_path", side_effect=AssertionError("NOOP path was resolved")):
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
                self.assertIn("scanLocal", result["timingsMs"])
                self.assertIn("listRemote", result["timingsMs"])
                self.assertIn("decodeRemote", result["timingsMs"])
                rendered = "\n".join(messages)
                self.assertIn("[NOOP] 1件", rendered)
                self.assertIn("適用対象の変更はありません。", rendered)
                self.assertNotIn("変更を適用しています", rendered)
                self.assertNotIn("適用中:", rendered)

    def test_full_sync_batches_seed_only_state_checkpoint(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                vault.mkdir()
                (vault / "one.txt").write_bytes(b"one")
                (vault / "two.json").write_bytes(b"two")
                state = Path(root) / "state.json"
                remote = FakeRemote({
                    "one.txt": RemoteObject(b"one", "e1", {}),
                    "two.json": RemoteObject(b"two", "e2", {}),
                })

                with mock.patch.object(executor_module, "save_state", wraps=checkpoint_module.save_state) as save:
                    result = execute_full_sync(
                        vault, state, remote.list, remote.get, remote.put, remote.delete,
                        PlainContent(), apply=True, push=True,
                    )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["appliedByType"], {"SEED": 2})
                self.assertEqual(result["applied"], 0)
                self.assertEqual(save.call_count, 1)
                self.assertGreaterEqual(result["timingsMs"]["applyCheckpoint"], 0)

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
                original = executor_module.atomic_replace

                def fail_second(target, data, mtime_ms):
                    if target.name == "second.md":
                        raise OSError("simulated stop")
                    return original(target, data, mtime_ms)

                with mock.patch.object(executor_module, "atomic_replace", side_effect=fail_second):
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
