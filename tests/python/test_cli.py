from support import *


class CliTests(unittest.TestCase):
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
                self.assertIn("snapshotCheck", result["timingsMs"])
                self.assertIn("applyRemote", result["timingsMs"])
                self.assertIn("applyCheckpoint", result["timingsMs"])
                self.assertTrue(result["remoteSnapshotRecheckEnabled"])
                self.assertTrue(result["remoteSnapshotRechecked"])

    def test_action_plan_lists_at_most_twenty_paths_per_type(self):
            messages = []
            actions = [
                {"type": "NOOP", "path": f"note-{index:02d}.md"}
                for index in range(25)
            ]

            executor_module._report_action_plan(messages.append, actions)

            rendered = "\n".join(messages)
            self.assertIn("[NOOP] 25件", rendered)
            self.assertEqual(sum(message.startswith("  note-") for message in messages), 20)
            self.assertIn("  note-19.md", rendered)
            self.assertNotIn("  note-20.md", rendered)
            self.assertIn("  ...ほか5件", rendered)

    def test_result_json_summarizes_routine_ignored_remote_objects(self):
            ignored = [
                {"key": f"encoded-{index}", "path": f"ignored/{index}.md", "reason": "ignored"}
                for index in range(25)
            ]
            ignored.extend({
                "key": f"broken-{index}", "reason": "cannot decode object path: invalid name"
            } for index in range(22))
            result = {"ok": True, "ignoredRemoteObjects": ignored, "errors": []}
            messages = []

            output = cli_module._result_for_json(result)
            _report_result(messages.append, result)

            self.assertIsNot(output, result)
            self.assertEqual(len(result["ignoredRemoteObjects"]), 47)
            self.assertEqual(output["ignoredRemoteObjectsTotal"], 47)
            self.assertEqual(output["ignoredRemoteObjectsOmitted"], 27)
            self.assertEqual(len(output["ignoredRemoteObjects"]), 20)
            self.assertTrue(all(item["reason"].startswith("cannot decode") for item in output["ignoredRemoteObjects"]))
            self.assertIn("ignoredRemoteObjects: 47", messages)

    def test_full_sync_can_explicitly_skip_remote_snapshot_recheck(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                state = Path(root) / "state.json"
                remote = FakeRemote({"note.txt": RemoteObject(b"remote", "e1", {})})
                list_remote = mock.Mock(side_effect=remote.list)
                messages = []

                result = execute_full_sync(
                    vault, state, list_remote, remote.get, remote.put, remote.delete,
                    PlainContent(), apply=True, push=True, recheck_remote_before_apply=False,
                    progress=messages.append,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(list_remote.call_count, 1)
                self.assertFalse(result["remoteSnapshotRecheckEnabled"])
                self.assertFalse(result["remoteSnapshotRechecked"])
                self.assertEqual(result["timingsMs"]["snapshotCheck"], 0)
                self.assertIn("snapshot再確認をスキップ", "\n".join(messages))

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
                    "fetchConcurrency": 1,
                    "requestTimeoutSeconds": 7.5,
                    "textMergeBaseMaxBytes": 4,
                    "recheckRemoteBeforeApply": False,
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

                with mock.patch.object(cli_module, "R2Client", return_value=client) as client_type:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = cli_module.main(["--config", str(config)])

                output = stdout.getvalue()
                self.assertEqual(exit_code, 0)
                self.assertGreater(output.count("\n"), 1)
                payload = json.loads(output)
                self.assertTrue(payload["ok"])
                self.assertFalse(payload["remoteSnapshotRecheckEnabled"])
                self.assertEqual(payload["fetchConcurrency"], 1)
                client_type.assert_called_once_with(
                    "https://<account-id>.r2.cloudflarestorage.com", "<bucket-name>", "key", "secret",
                    timeout=7.5,
                )
                self.assertIn("vault:", stderr.getvalue())
                self.assertIn("mode: DRY-RUN", stderr.getvalue())
                self.assertIn("fetch concurrency: 1 / request timeout: 7.5秒", stderr.getvalue())
                self.assertIn("dry-runのため実際の変更は行っていません", stderr.getvalue())
                self.assertIn("=== 実行結果 ===", stderr.getvalue())

    def test_nested_config_ignore_glob_still_uses_vault_root(self):
            with tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                vault = root_path / "vault"
                config = vault / "tools" / "python" / "r2-sync" / "config.json"
                config.parent.mkdir(parents=True)
                (vault / "elsewhere").mkdir()
                (vault / "elsewhere" / "ignored.txt").write_text("ignored", encoding="utf-8")
                (vault / "keep.txt").write_text("keep", encoding="utf-8")
                config.write_text(json.dumps({
                    "vaultPath": str(vault),
                    "statePath": "state.json",
                    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                    "bucket": "<bucket-name>",
                    "accessKeyId": "key",
                    "secretAccessKey": "secret",
                    "encryption": "plain",
                    "mode": "full",
                    "ignoreExtra": ["elsewhere/**", "tools/python/r2-sync/**"],
                }), encoding="utf-8")
                remote = FakeRemote({})
                client = mock.Mock(
                    list_all=remote.list,
                    get_object=remote.get,
                    put_object=remote.put,
                    delete_object=remote.delete,
                )
                stdout = io.StringIO()

                with mock.patch.object(cli_module, "R2Client", return_value=client):
                    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                        exit_code = cli_module.main(["--config", str(config)])

                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["scannedLocal"], 1)
                self.assertEqual(payload["plannedByType"], {"PUSH": 1})

    def test_main_check_ignore_never_constructs_r2_client(self):
            with tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                vault = root_path / "vault"
                (vault / "ignored").mkdir(parents=True)
                (vault / "ignored" / "file.txt").write_text("ignored", encoding="utf-8")
                (vault / ".env").write_text("ignored", encoding="utf-8")
                (vault / "state.json").write_text("ignored", encoding="utf-8")
                (vault / "keep.txt").write_text("keep", encoding="utf-8")
                (vault / ".agents" / "nested").mkdir(parents=True)
                (vault / ".agents" / "one.md").write_text("one", encoding="utf-8")
                (vault / ".agents" / "nested" / "two.md").write_text("two", encoding="utf-8")
                (vault / "mixed" / "pure" / "deep").mkdir(parents=True)
                (vault / "mixed" / ".env").write_text("ignored", encoding="utf-8")
                (vault / "mixed" / "keep.txt").write_text("keep", encoding="utf-8")
                (vault / "mixed" / "pure" / "a.txt").write_text("a", encoding="utf-8")
                (vault / "mixed" / "pure" / "deep" / "b.txt").write_text("b", encoding="utf-8")
                config = root_path / "config.json"
                state = root_path / "checkpoint.json"
                config.write_text(json.dumps({
                    "vaultPath": str(vault),
                    "statePath": str(state),
                    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                    "bucket": "<bucket-name>",
                    "accessKeyId": "key",
                    "secretAccessKey": "secret",
                    "mode": "full",
                    "ignoreExtra": ["ignored/**", "**/.env"],
                }), encoding="utf-8")
                listed_stdout = io.StringIO()
                verbose_stdout = io.StringIO()
                checked_stdout = io.StringIO()

                held_lock = local_module._SyncRunLock(vault)
                held_lock.acquire()
                try:
                    with mock.patch.object(cli_module, "R2Client", side_effect=AssertionError("R2 must not be used")):
                        with redirect_stdout(listed_stdout), redirect_stderr(io.StringIO()):
                            list_exit = cli_module.main(["--config", str(config), "--check-ignore"])
                        with redirect_stdout(verbose_stdout), redirect_stderr(io.StringIO()):
                            verbose_exit = cli_module.main([
                                "--config", str(config), "--verbose", "--check-ignore",
                            ])
                        with redirect_stdout(checked_stdout), redirect_stderr(io.StringIO()):
                            check_exit = cli_module.main([
                                "--config", str(config), "--check-ignore", "ignored/file.txt", "keep.txt",
                            ])
                finally:
                    held_lock.release()

                self.assertEqual(list_exit, 0)
                self.assertIn("ignoreExtra: 2 patterns", listed_stdout.getvalue())
                self.assertIn("[IGNORE] 4 entries", listed_stdout.getvalue())
                self.assertIn("  ignored/ (all files)", listed_stdout.getvalue())
                self.assertIn("  .env", listed_stdout.getvalue())
                self.assertIn("  mixed/.env", listed_stdout.getvalue())
                self.assertIn("  state.json", listed_stdout.getvalue())
                self.assertIn("[INCLUDE] 6 files", listed_stdout.getvalue())
                self.assertIn("  .agents/ (2 files)", listed_stdout.getvalue())
                self.assertIn("  keep.txt", listed_stdout.getvalue())
                self.assertIn("  mixed/keep.txt", listed_stdout.getvalue())
                self.assertIn("  mixed/pure/ (2 files)", listed_stdout.getvalue())
                self.assertNotIn("  .agents/one.md", listed_stdout.getvalue())
                self.assertNotIn("  mixed/pure/deep/", listed_stdout.getvalue())
                self.assertEqual(verbose_exit, 0)
                self.assertIn("[IGNORE] 5 entries", verbose_stdout.getvalue())
                self.assertIn("  ignored/file.txt", verbose_stdout.getvalue())
                self.assertIn("[INCLUDE] 6 files", verbose_stdout.getvalue())
                self.assertIn("  keep.txt", verbose_stdout.getvalue())
                self.assertIn("  .agents/one.md", verbose_stdout.getvalue())
                self.assertIn("  mixed/pure/deep/b.txt", verbose_stdout.getvalue())
                self.assertEqual(check_exit, 1)
                self.assertIn("[IGNORE] ignored/file.txt", checked_stdout.getvalue())
                self.assertIn("[INCLUDE] keep.txt", checked_stdout.getvalue())
                self.assertFalse(state.exists())

    def test_main_rejects_concurrent_sync_before_r2_and_releases_lock_after_error(self):
            with tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                vault = root_path / "vault"
                vault.mkdir()
                state = root_path / "state.json"
                config = root_path / "config.json"
                config.write_text(json.dumps({
                    "vaultPath": str(vault),
                    "statePath": str(state),
                    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                    "bucket": "<bucket-name>",
                    "accessKeyId": "key",
                    "secretAccessKey": "secret",
                    "mode": "full",
                }), encoding="utf-8")
                held_lock = local_module._SyncRunLock(vault)
                held_lock.acquire()
                try:
                    stdout = io.StringIO()
                    with mock.patch.object(
                        cli_module, "R2Client", side_effect=AssertionError("R2 must not be used")
                    ):
                        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                            exit_code = cli_module.main(["--config", str(config)])
                finally:
                    held_lock.release()

                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 1)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["errors"], [{
                    "error": "another sync is already running for this vault",
                }])
                self.assertFalse(state.exists())

                with mock.patch.object(cli_module, "R2Client", side_effect=PullError("client failed")):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(cli_module.main(["--config", str(config)]), 1)

                next_lock = local_module._SyncRunLock(vault)
                next_lock.acquire()
                next_lock.release()

                with mock.patch.object(cli_module, "R2Client", side_effect=KeyboardInterrupt):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        with self.assertRaises(KeyboardInterrupt):
                            cli_module.main(["--config", str(config)])

                after_interrupt_lock = local_module._SyncRunLock(vault)
                after_interrupt_lock.acquire()
                after_interrupt_lock.release()

    def test_config_rejects_unsafe_performance_option_types(self):
            with tempfile.TemporaryDirectory() as root:
                config = Path(root) / "config.json"
                base = {
                    "vaultPath": str(Path(root) / "vault"),
                    "statePath": "state.json",
                    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                    "bucket": "<bucket-name>",
                    "accessKeyId": "key",
                    "secretAccessKey": "secret",
                    "mode": "full",
                }
                for field, value in (
                    ("textMergeBaseMaxBytes", -1),
                    ("textMergeBaseMaxBytes", True),
                    ("recheckRemoteBeforeApply", "false"),
                    ("fetchConcurrency", 0),
                    ("fetchConcurrency", 17),
                    ("fetchConcurrency", True),
                    ("fetchConcurrency", 1.5),
                    ("requestTimeoutSeconds", 0),
                    ("requestTimeoutSeconds", 301),
                    ("requestTimeoutSeconds", True),
                    ("requestTimeoutSeconds", "10"),
                    ("unicodeCollisionPolicy", "prefer-newest"),
                    ("unicodeCollisionPolicy", []),
                ):
                    config.write_text(json.dumps({**base, field: value}), encoding="utf-8")
                    with self.assertRaises(Exception):
                        config_module._load_config(config)

    def test_config_requires_explicit_mode(self):
            with tempfile.TemporaryDirectory() as root:
                config = Path(root) / "config.json"
                config.write_text(json.dumps({
                    "vaultPath": str(Path(root) / "vault"),
                    "statePath": "state.json",
                    "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
                    "bucket": "<bucket-name>",
                    "accessKeyId": "key",
                    "secretAccessKey": "secret",
                }), encoding="utf-8")

                with self.assertRaisesRegex(PullError, "config is missing: mode"):
                    config_module._load_config(config)

    def test_removed_hidden_cli_flags_are_rejected(self):
            for option in ("--full", "--push", "--merge"):
                with self.subTest(option=option):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            cli_module.main(["--config", "config.json", option])

    def test_fetch_concurrency_one_avoids_executor_and_reports_each_completion(self):
            completed = []
            with mock.patch.object(remote_module.concurrent.futures, "ThreadPoolExecutor") as pool_type:
                results = remote_module._run_parallel(
                    [1, 2, 3], lambda value: value * 10, 1,
                    lambda done, total: completed.append((done, total)),
                )

            pool_type.assert_not_called()
            self.assertEqual([future.result() for _, future in results], [10, 20, 30])
            self.assertEqual(completed, [(1, 3), (2, 3), (3, 3)])

    def test_parallel_interrupt_cancels_pending_work_without_waiting(self):
            pool = mock.Mock()
            futures = [mock.Mock(), mock.Mock()]
            pool.submit.side_effect = futures

            with mock.patch.object(remote_module.concurrent.futures, "ThreadPoolExecutor", return_value=pool):
                with mock.patch.object(remote_module.concurrent.futures, "as_completed", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        remote_module._run_parallel(["one", "two"], lambda value: value, 2)

            for future in futures:
                future.cancel.assert_called_once_with()
            pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_full_sync_uses_configured_fetch_concurrency(self):
            with tempfile.TemporaryDirectory() as root:
                vault = Path(root) / "vault"
                remote = FakeRemote({
                    "one.md": RemoteObject(b"one", "e1", {}),
                    "two.md": RemoteObject(b"two", "e2", {}),
                    "three.md": RemoteObject(b"three", "e3", {}),
                })
                messages = []

                result = execute_full_sync(
                    vault, Path(root) / "state.json", remote.list, remote.get, remote.put, remote.delete,
                    PlainContent(), fetch_concurrency=1, progress=messages.append,
                )

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["fetchConcurrency"], 1)
                rendered = "\n".join(messages)
                self.assertIn("R2から取得・検証しています(並列1件)", rendered)
                self.assertIn("取得・検証中: 1/3", rendered)
                self.assertIn("取得・検証中: 3/3", rendered)


if __name__ == "__main__":
    unittest.main()
