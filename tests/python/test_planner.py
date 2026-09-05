import sys
from pathlib import Path
import unittest


PYTHON_SOURCE = Path(__file__).resolve().parents[2] / "py"
sys.path.insert(0, str(PYTHON_SOURCE))

from r2sync.planner import classify_sync_path, three_way_merge


class PlannerTests(unittest.TestCase):
    def test_new_paths_are_classified_without_io(self):
        local = {"mtimeMs": 1000, "size": 4}
        remote = {"etag": "remote"}

        self.assertEqual(classify_sync_path("local.md", local, None, None)["type"], "NEW_LOCAL")
        self.assertEqual(classify_sync_path("remote.md", None, remote, None)["type"], "NEW_REMOTE")
        self.assertEqual(classify_sync_path("both.md", local, remote, None)["type"], "BOOTSTRAP")

    def test_unchanged_path_is_noop(self):
        local = {"mtimeMs": 1000, "size": 4}
        remote = {"etag": "remote"}
        previous = {"localMtimeMs": 1000, "localSize": 4, "remoteETag": "remote"}

        self.assertEqual(classify_sync_path("note.md", local, remote, previous)["type"], "NOOP")

    def test_one_sided_changes_become_content_candidates(self):
        previous = {"localMtimeMs": 1000, "localSize": 4, "remoteETag": "old"}

        local_change = classify_sync_path(
            "local.md", {"mtimeMs": 2000, "size": 4}, {"etag": "old"}, previous
        )
        remote_change = classify_sync_path(
            "remote.md", {"mtimeMs": 1000, "size": 4}, {"etag": "new"}, previous
        )

        self.assertEqual(local_change["type"], "LOCAL_CHANGED")
        self.assertEqual(remote_change["type"], "REMOTE_CHANGED")

    def test_both_changed_becomes_merge_candidate(self):
        previous = {"localMtimeMs": 1000, "localSize": 4, "remoteETag": "old"}
        decision = classify_sync_path(
            "note.md", {"mtimeMs": 2000, "size": 5}, {"etag": "new"}, previous
        )

        self.assertEqual(decision["type"], "BOTH_CHANGED")

    def test_deletes_and_delete_change_races_are_distinct(self):
        previous = {"localMtimeMs": 1000, "localSize": 4, "remoteETag": "old"}

        self.assertEqual(
            classify_sync_path("local-gone.md", None, {"etag": "old"}, previous)["type"],
            "DELETE_REMOTE",
        )
        self.assertEqual(
            classify_sync_path("remote-gone.md", {"mtimeMs": 1000, "size": 4}, None, previous)["type"],
            "DELETE_LOCAL",
        )
        self.assertEqual(
            classify_sync_path("remote-edited.md", None, {"etag": "new"}, previous)["type"],
            "RESTORE_REMOTE_CHANGED",
        )
        self.assertEqual(
            classify_sync_path("local-edited.md", {"mtimeMs": 2000, "size": 4}, None, previous)["type"],
            "RESTORE_LOCAL_CHANGED",
        )
        self.assertEqual(classify_sync_path("gone.md", None, None, previous)["type"], "FORGET")

    def test_three_way_merge_keeps_non_overlapping_changes(self):
        merged, conflicted = three_way_merge(
            b"one\ntwo\nthree\n",
            b"ONE\ntwo\nthree\n",
            b"one\ntwo\nTHREE\n",
        )

        self.assertFalse(conflicted)
        self.assertEqual(merged, b"ONE\ntwo\nTHREE\n")


if __name__ == "__main__":
    unittest.main()
