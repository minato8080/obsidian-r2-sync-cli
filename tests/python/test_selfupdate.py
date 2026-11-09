import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from support import PullError, cli_module
from r2sync import __version__
from r2sync import selfupdate

_spec = importlib.util.spec_from_file_location(
    "build_python_release", Path(__file__).resolve().parents[2] / "tools" / "build_python_release.py"
)
build_release_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_release_module)

REPO = "example/r2-sync"
OLD_INIT = b'"""old"""\n__version__ = "1.0.0"\n'


def _version_files(version: str) -> dict[str, bytes]:
    return {
        "sync.py": f"# sync {version}\n".encode(),
        "r2sync/__init__.py": f'"""new"""\n__version__ = "{version}"\n'.encode(),
        "r2sync/cli.py": b"def main():\n    return 0\n",
    }


class FakeGitHub:
    def __init__(self, tag, files=None, archive=None, sums=None, asset_url_prefix=None):
        self.tag = tag
        self.archive = archive if archive is not None else build_release_module.build_zip(files or _version_files(tag[1:]))
        self.zip_name = selfupdate.asset_name(tag)
        self.sums = sums if sums is not None else f"{hashlib.sha256(self.archive).hexdigest()}  {self.zip_name}\n".encode()
        prefix = asset_url_prefix or f"https://github.com/{REPO}/releases/download/{tag}/"
        self.urls = {self.zip_name: prefix + self.zip_name, "SHA256SUMS": prefix + "SHA256SUMS"}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url == f"https://api.github.com/repos/{REPO}/releases/latest":
            assets = [{"name": name, "browser_download_url": value} for name, value in self.urls.items()]
            return json.dumps({"tag_name": self.tag, "assets": assets}).encode()
        if url == self.urls[self.zip_name]:
            return self.archive
        if url == self.urls["SHA256SUMS"]:
            return self.sums
        raise AssertionError(f"unexpected url {url}")


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.install = Path(self._tmp.name) / "scripts"
        (self.install / "r2sync").mkdir(parents=True)
        (self.install / "sync.py").write_bytes(b"# old sync\n")
        (self.install / "r2sync" / "__init__.py").write_bytes(OLD_INIT)
        (self.install / "r2sync" / "old_only.py").write_bytes(b"x = 1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self):
        return {str(path.relative_to(self.install)).replace(os.sep, "/"): path.read_bytes() for path in sorted(self.install.rglob("*")) if path.is_file()}

    def _run(self, github, **options):
        return selfupdate.self_update(REPO, install_dir=self.install, current_version="1.0.0", fetch=github, **options)

    def test_update_replaces_files_and_keeps_backup(self):
        result = self._run(FakeGitHub("v1.1.0"))
        self.assertEqual((result["ok"], result["status"], result["updated"], result["latestVersion"]), (True, "updated", True, "1.1.0"))
        self.assertEqual((self.install / "sync.py").read_bytes(), b"# sync 1.1.0\n")
        self.assertIn(b'"1.1.0"', (self.install / "r2sync" / "__init__.py").read_bytes())
        self.assertFalse((self.install / "r2sync" / "old_only.py").exists())
        backup = self.install / selfupdate.BACKUP_DIR
        self.assertEqual((backup / "sync.py").read_bytes(), b"# old sync\n")
        self.assertEqual((backup / "r2sync" / "__init__.py").read_bytes(), OLD_INIT)
        self.assertEqual([path.name for path in self.install.iterdir() if path.name.startswith(selfupdate.STAGING_PREFIX)], [])

    def test_up_to_date_and_check_only_change_nothing(self):
        before = self._snapshot()
        github = FakeGitHub("v1.0.0")
        result = self._run(github)
        self.assertEqual((result["ok"], result["status"], result["updated"]), (True, "up-to-date", False))
        self.assertEqual(len(github.calls), 1)
        result = self._run(FakeGitHub("v2.0.0"), check_only=True)
        self.assertEqual((result["ok"], result["status"], result["updated"]), (True, "available", False))
        self.assertEqual(self._snapshot(), before)

    def test_rejected_releases_change_nothing(self):
        bad_zip = io.BytesIO()
        with zipfile.ZipFile(bad_zip, "w") as archive:
            archive.writestr("../evil.py", b"x = 1\n")
        cases = {
            "SHA-256 mismatch": FakeGitHub("v1.1.0", sums=b"0" * 64 + b"  r2sync-python-v1.1.0.zip\n"),
            "unexpected file": FakeGitHub("v1.1.0", archive=bad_zip.getvalue()),
            "does not match release": FakeGitHub("v1.1.0", files=_version_files("1.2.0")),
            "does not compile": FakeGitHub("v1.1.0", files={**_version_files("1.1.0"), "r2sync/cli.py": b"def broken(:\n"}),
            "not hosted by the repository": FakeGitHub("v1.1.0", asset_url_prefix="https://example.com/"),
        }
        before = self._snapshot()
        for message, github in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(PullError, message):
                    self._run(github)
                self.assertEqual(self._snapshot(), before)

    def test_refuses_git_checkout_and_missing_install(self):
        (Path(self._tmp.name) / ".git").mkdir()
        with self.assertRaisesRegex(PullError, "Git checkout"):
            self._run(FakeGitHub("v1.1.0"))
        with self.assertRaisesRegex(PullError, "install directory"):
            selfupdate.self_update(REPO, install_dir=Path(self._tmp.name), current_version="1.0.0", fetch=FakeGitHub("v1.1.0"))

    def test_failed_swap_restores_previous_version(self):
        before = self._snapshot()
        real_replace = os.replace
        staged_moves = []

        def failing_replace(source, target):
            if selfupdate.STAGING_PREFIX in str(source):
                staged_moves.append(source)
                if len(staged_moves) == 2:
                    raise OSError("simulated replace failure")
            return real_replace(source, target)

        with mock.patch.object(selfupdate.os, "replace", side_effect=failing_replace):
            with self.assertRaisesRegex(OSError, "simulated"):
                self._run(FakeGitHub("v1.1.0"))
        self.assertEqual({name: data for name, data in self._snapshot().items() if not name.startswith(selfupdate.BACKUP_DIR)}, before)

    def test_build_release_requires_matching_tag_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(SystemExit):
                build_release_module.build_release("v0.0.1", Path(out))
            zip_path, sums_path = build_release_module.build_release(f"v{__version__}", Path(out))
            first = zip_path.read_bytes()
            build_release_module.build_release(f"v{__version__}", Path(out))
            self.assertEqual(zip_path.read_bytes(), first)
            self.assertEqual(sums_path.read_text(encoding="utf-8"), f"{hashlib.sha256(first).hexdigest()}  {zip_path.name}\n")
            files = selfupdate._read_release_zip(first, __version__)
            self.assertIn("r2sync/selfupdate.py", files)
            self.assertIn("sync.py", files)

    def test_cli_self_update_outputs_json_without_config(self):
        fake_result = {"ok": True, "mode": "self-update-check", "status": "available"}
        stdout = io.StringIO()
        with mock.patch.object(cli_module, "self_update", return_value=fake_result) as call:
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = cli_module.main(["--self-update", "--check"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), fake_result)
        self.assertTrue(call.call_args.kwargs["check_only"])
        for argv in (["--self-update", "--apply"], ["--check"], ["--apply"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli_module.main(argv)


if __name__ == "__main__":
    unittest.main()
