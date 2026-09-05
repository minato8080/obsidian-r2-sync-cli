from support import *


class LocalAndCheckpointTests(unittest.TestCase):
    def test_nfd_local_path_matches_nfc_remote_and_state(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "state.json"
                nfc_path = "inbox/アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                self.assertNotEqual(nfc_path, nfd_path)
                target = vault / Path(*nfd_path.split("/"))
                target.parent.mkdir(parents=True)
                target.write_bytes(b"same")
                remote = FakeRemote({nfc_path: RemoteObject(b"same", "e1", {})})

                result = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["scannedLocal"], 1)
                self.assertEqual(result["reconciledLocalFiles"], 0)
                self.assertEqual(result["appliedByType"], {"SEED": 1})
                self.assertEqual(set(load_state(state)), {nfc_path})
                self.assertEqual(set(remote.objects), {nfc_path})

    def test_existing_nfd_remote_key_is_reused_for_push(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "state.json"
                nfc_path = "inbox/アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                target = vault / Path(*nfd_path.split("/"))
                target.parent.mkdir(parents=True)
                target.write_bytes(b"local-new")
                remote = FakeRemote({nfd_path: RemoteObject(b"remote-old", "e1", {})})

                result = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["appliedByType"], {"PUSH": 1})
                self.assertEqual(set(remote.objects), {nfd_path})
                self.assertEqual(remote.objects[nfd_path].data, b"local-new")
                self.assertEqual(set(load_state(state)), {nfc_path})

    def test_remote_path_recovers_file_omitted_by_initial_scan(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                vault.mkdir()
                target = vault / "late.md"
                target.write_bytes(b"same")
                state = Path(root) / "state.json"
                remote = FakeRemote({"late.md": RemoteObject(b"same", "e1", {})})
                messages = []

                with mock.patch.object(executor_module, "_scan_vault", return_value={}):
                    result = execute_full_sync(
                        vault, state, remote.list, remote.get, remote.put, remote.delete,
                        PlainContent(), apply=True, push=True, progress=messages.append,
                    )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["scannedLocal"], 1)
                self.assertEqual(result["reconciledLocalFiles"], 1)
                self.assertEqual(result["appliedByType"], {"SEED": 1})
                self.assertIn("local paths reconciled: 1件", messages)
                self.assertFalse(any(
                    item.get("reason") == "target appeared during fetch"
                    for item in result["conflicts"]
                ))

    def test_state_rejects_paths_that_collide_after_nfc_normalization(self):
            with tempfile.TemporaryDirectory() as root:
                state = Path(root) / "state.json"
                nfc_path = "アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                state.write_text(json.dumps({"version": 1, "entries": {
                    nfc_path: {"remoteETag": "one"},
                    nfd_path: {"remoteETag": "two"},
                }}, ensure_ascii=False), encoding="utf-8")

                with self.assertRaisesRegex(PullError, "multiple state paths normalize"):
                    load_state(state)

    def test_remote_paths_that_collide_after_nfc_normalization_stop_sync(self):
            with tempfile.TemporaryDirectory() as root:
                nfc_path = "アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                remote = FakeRemote({
                    nfc_path: RemoteObject(b"one", "one", {}),
                    nfd_path: RemoteObject(b"two", "two", {}),
                })

                result = execute_full_sync(
                    Path(root) / "vault", Path(root) / "state.json",
                    remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True,
                )

                self.assertFalse(result["ok"], result)
                self.assertEqual(result["applied"], 0)
                self.assertTrue(any(
                    item.get("reason") == "multiple remote objects decode to the same path"
                    for item in result["conflicts"]
                ))

    def test_local_paths_that_collide_after_nfc_normalization_stop_scan(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                vault.mkdir()
                nfc_name = "アクアパッツア.md"
                nfd_name = unicodedata.normalize("NFD", nfc_name)
                (vault / nfc_name).write_bytes(b"one")
                (vault / nfd_name).write_bytes(b"two")
                if len(list(vault.iterdir())) < 2:
                    self.skipTest("filesystem does not permit distinct NFC and NFD names")

                with self.assertRaisesRegex(PullError, "multiple local paths normalize"):
                    _scan_vault(vault)

    def test_prefer_nfc_uses_exact_local_name_without_deleting_alias(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                vault.mkdir()
                nfc_name = "アクアパッツア.md"
                nfd_name = unicodedata.normalize("NFD", nfc_name)
                (vault / nfc_name).write_bytes(b"nfc")
                (vault / nfd_name).write_bytes(b"nfd-alias")
                if len(list(vault.iterdir())) < 2:
                    self.skipTest("filesystem does not permit distinct NFC and NFD names")
                stats = {"ignored": 0}

                files = _scan_vault(
                    vault, unicode_collision_policy="prefer-nfc", collision_stats=stats
                )

                self.assertEqual(set(files), {nfc_name})
                self.assertEqual(files[nfc_name]["size"], len(b"nfc"))
                self.assertEqual(stats["ignored"], 1)
                self.assertTrue((vault / nfd_name).exists())

    def test_prefer_nfc_uses_exact_remote_name_and_reports_alias(self):
            with tempfile.TemporaryDirectory() as root:
                nfc_path = "inbox/アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                remote = FakeRemote({
                    nfd_path: RemoteObject(b"nfd-alias", "nfd", {}),
                    nfc_path: RemoteObject(b"nfc", "nfc", {}),
                })

                result = execute_full_sync(
                    Path(root) / "vault", Path(root) / "state.json",
                    remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=False, push=True,
                    unicode_collision_policy="prefer-nfc",
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["unicodeAliasesIgnored"], 1)
                self.assertEqual(result["plannedByType"], {"PULL": 1})
                self.assertEqual(remote.calls, [nfc_path])
                self.assertTrue(any(
                    item.get("key") == nfd_path and "Unicode alias" in item.get("reason", "")
                    for item in result["ignoredRemoteObjects"]
                ))

    def test_prefer_nfc_remote_selection_is_independent_of_alias_order(self):
            with tempfile.TemporaryDirectory() as root:
                nfc_path = "inbox/é.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                other_alias = "inbox/e\u0341.md"
                remote = FakeRemote({
                    nfd_path: RemoteObject(b"nfd", "nfd", {}),
                    other_alias: RemoteObject(b"other", "other", {}),
                    nfc_path: RemoteObject(b"nfc", "nfc", {}),
                })

                result = execute_full_sync(
                    Path(root) / "vault", Path(root) / "state.json",
                    remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=False, push=True,
                    unicode_collision_policy="prefer-nfc",
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["unicodeAliasesIgnored"], 2)
                self.assertEqual(remote.calls, [nfc_path])

    def test_prefer_nfc_uses_exact_state_key(self):
            with tempfile.TemporaryDirectory() as root:
                state = Path(root) / "state.json"
                nfc_path = "アクアパッツア.md"
                nfd_path = unicodedata.normalize("NFD", nfc_path)
                state.write_text(json.dumps({"version": 1, "entries": {
                    nfd_path: {"remoteETag": "nfd"},
                    nfc_path: {"remoteETag": "nfc"},
                }}, ensure_ascii=False), encoding="utf-8")
                stats = {"ignored": 0}

                entries = load_state(state, "prefer-nfc", stats)

                self.assertEqual(entries, {nfc_path: {"remoteETag": "nfc"}})
                self.assertEqual(stats["ignored"], 1)

    def test_full_sync_prunes_large_and_binary_merge_bases_when_enabled(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "state.json"
                remote = FakeRemote({
                    "small.txt": RemoteObject(b"ok", "small", {}),
                    "large.txt": RemoteObject(b"12345", "large", {}),
                    "image.bin": RemoteObject(b"\xff\x00", "binary", {}),
                })
                first = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True,
                )
                self.assertTrue(first["ok"], first)
                original_state = state.read_bytes()
                original_size = state.stat().st_size

                dry_run = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=False, push=True, text_merge_base_max_bytes=4,
                )
                self.assertTrue(dry_run["ok"], dry_run)
                self.assertEqual(dry_run["mergeBasesPruned"], 0)
                self.assertEqual(dry_run["mergeBasesPendingPrune"], 2)
                self.assertEqual(state.read_bytes(), original_state)

                with mock.patch.object(executor_module, "save_state", wraps=checkpoint_module.save_state) as save:
                    second = execute_full_sync(
                        vault, state, remote.list, remote.get, remote.put, remote.delete,
                        PlainContent(), apply=True, push=True, text_merge_base_max_bytes=4,
                    )

                self.assertTrue(second["ok"], second)
                self.assertEqual(second["planned"], 0)
                self.assertEqual(second["mergeBasesPruned"], 2)
                self.assertEqual(second["mergeBasesPendingPrune"], 0)
                self.assertEqual(save.call_count, 1)
                self.assertLess(state.stat().st_size, original_size)
                entries = load_state(state)
                self.assertIn("baseContentBase64", entries["small.txt"])
                self.assertNotIn("baseContentBase64", entries["large.txt"])
                self.assertNotIn("baseContentBase64", entries["image.bin"])

                (vault / "large.txt").write_bytes(b"local-change")
                remote.objects["large.txt"] = RemoteObject(b"remote-change", "large-new", {})
                conflict = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True, merge=True, text_merge_base_max_bytes=4,
                )
                self.assertFalse(conflict["ok"], conflict)
                self.assertEqual(conflict["applied"], 0)
                self.assertTrue(any("merge base is unavailable" in item["reason"] for item in conflict["conflicts"]))
                self.assertEqual((vault / "large.txt").read_bytes(), b"local-change")
                self.assertEqual(remote.objects["large.txt"].data, b"remote-change")

    def test_vault_root_glob_matches_directory_and_descendants(self):
            ignored_dir, ignored_file = _ignore_matcher(["tools/r2-sync/**"])
            self.assertTrue(ignored_dir("tools/r2-sync"))
            self.assertTrue(ignored_file("tools/r2-sync/sync.py"))
            self.assertFalse(ignored_file("other/tools/r2-sync/sync.py"))

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

    def test_state_json_basename_is_always_ignored_locally_and_remotely(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                for rel_path in ("state.json", "nested/state.json", "state.json.bak", "local.txt"):
                    target = vault / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(rel_path, encoding="utf-8")
                remote = FakeRemote({
                    "state.json": RemoteObject(b"ignored", "root-state", {}),
                    "nested/state.json": RemoteObject(b"ignored", "nested-state", {}),
                    "remote.txt": RemoteObject(b"keep", "remote", {}),
                })

                result = execute_full_sync(
                    vault, Path(root) / "checkpoint.json",
                    remote.list, remote.get, remote.put, remote.delete, PlainContent(),
                    apply=False, push=True,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["scannedLocal"], 2)
                self.assertEqual(
                    {item["path"] for item in result["ignoredRemoteObjects"]},
                    {"state.json", "nested/state.json"},
                )
                self.assertEqual(result["plannedByType"], {"PUSH": 2, "PULL": 1})

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

    def test_state_path_is_always_config_relative(self):
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
                    cwd_state = root_path / "state.json"
                    cwd_state.write_text("{}", encoding="utf-8")
                    self.assertEqual(_config_relative_path("state.json", config), expected.resolve())
                finally:
                    os.chdir(previous_cwd)

    def test_ignore_glob_character_class(self):
            _, ignored_file = _ignore_matcher(["tools/secret[1-9].md"])
            self.assertFalse(ignored_file("tools/secret0.md"))
            self.assertTrue(ignored_file("tools/secret1.md"))
            self.assertFalse(ignored_file("tools/nested/secret2.md"))

    def test_vault_relative_ignore_globs(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                for rel_path in (
                    "scripts/python/r2-sync/sync.py", "scripts/js/r2-sync/app.js", "scripts/python/keep.py",
                    ".env", "project/.env", "project/.env.example", ".obsidian/plugins/remotely-save/data.json",
                    ".obsidian/plugins.json", ".git/config", "justfile", "notes/keep.md",
                ):
                    target = vault / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(rel_path, encoding="utf-8")
                files = _scan_vault(vault, [
                    "scripts/python/r2-sync/**",
                    "scripts/js/r2-sync/**",
                    ".obsidian/plugins/remotely-save/data.json",
                    "**/.env",
                    ".git",
                ])
                self.assertEqual(sorted(files), [
                    ".obsidian/plugins.json", "justfile", "notes/keep.md", "project/.env.example", "scripts/python/keep.py",
                ])

    def test_vault_relative_ignore_glob_filters_remote_objects(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "state.json"
                remote = FakeRemote({
                    "tools/js/r2-sync/app.js": RemoteObject(b"ignored", "ignored", {}),
                    "notes/keep.md": RemoteObject(b"keep", "keep", {}),
                })

                result = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(),
                    extra_patterns=["tools/js/r2-sync/**"], apply=False, push=True,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["plannedByType"], {"PULL": 1})
                self.assertEqual(result["ignoredRemoteObjects"][0]["path"], "tools/js/r2-sync/app.js")

    def test_ignored_file_in_old_state_never_reenters_full_sync(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                tool_dir = vault / "tools"
                tool_dir.mkdir(parents=True)
                (tool_dir / "config.json").write_text("local config", encoding="utf-8")
                state = Path(root) / "state.json"
                state.write_text(json.dumps({"version": 1, "entries": {
                "tools/config.json": {"localMtimeMs": 1, "localSize": 12, "remoteETag": "old"}
                }}), encoding="utf-8")
                remote = FakeRemote({})

                result = execute_full_sync(
                    vault, state, remote.list, remote.get, remote.put, remote.delete, PlainContent(),
                extra_patterns=["tools/**"], apply=True, push=True,
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["planned"], 0)
                self.assertEqual(remote.objects, {})
                self.assertEqual((tool_dir / "config.json").read_text(encoding="utf-8"), "local config")

    def test_state_inside_vault_is_protected_without_ignore_pattern(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                tool_dir = vault / "tools"
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


if __name__ == "__main__":
    unittest.main()
