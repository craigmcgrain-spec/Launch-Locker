"""Tests for locker.store: path resolution, persistence, and durability."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from locker.store import (
    Entry,
    ReadOnlyStore,
    Store,
    StoreCorrupt,
    resolve_store_path,
    utcnow,
)


class PathTests(unittest.TestCase):
    def test_path_precedence(self):
        env = {"LOCKER_FILE": "/tmp/y.json", "XDG_CONFIG_HOME": "/tmp/z"}

        self.assertEqual(resolve_store_path("/tmp/x.json", env), Path("/tmp/x.json"))
        self.assertEqual(resolve_store_path(None, env), Path("/tmp/y.json"))
        self.assertEqual(
            resolve_store_path(None, {"XDG_CONFIG_HOME": "/tmp/z"}),
            Path("/tmp/z/locker/lockers.json"),
        )
        self.assertTrue(
            str(resolve_store_path(None, {})).endswith(".config/locker/lockers.json")
        )


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "lockers.json"

    def entry(self, name="qwen3.8", command="llama-server -hf Qwen/Qwen3-8B"):
        now = utcnow()
        return Entry(
            name=name, command=command, hf_repo=None, created=now, updated=now
        )

    def test_missing_file_is_empty_store(self):
        store = Store.open(self.path)

        self.assertEqual(store.entries(), [])
        self.assertFalse(self.path.exists())

    def test_roundtrip(self):
        entry = self.entry()
        Store.open(self.path).put(entry)

        reopened = Store.open(self.path)

        self.assertEqual(reopened.entries(), [entry])
        self.assertEqual(entry.created, entry.updated)

    def test_corrupt_json_raises(self):
        self.path.write_text("{ not json")
        before = self.path.read_text()

        with self.assertRaises(StoreCorrupt) as caught:
            Store.open(self.path)

        self.assertIn(str(self.path), str(caught.exception))
        self.assertEqual(self.path.read_text(), before)

    def test_wrong_shape_raises(self):
        shapes = [
            {"version": 1, "lockers": []},
            {"version": 1, "lockers": {"a": []}},
            {"version": 1, "lockers": {"a": {"name": "a"}}},
            {"version": 1, "lockers": {"a": {"command": 42}}},
        ]

        for shape in shapes:
            with self.subTest(shape=shape):
                self.path.write_text(json.dumps(shape))
                with self.assertRaises(StoreCorrupt):
                    Store.open(self.path)

    def test_no_temp_files_left(self):
        store = Store.open(self.path)
        store.put(self.entry())

        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        self.assertEqual([p.name for p in self.dir.iterdir()], ["lockers.json"])

    def test_permissions(self):
        nested = self.dir / "nested" / "lockers.json"

        Store.open(nested).put(self.entry())

        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(nested.parent.stat().st_mode), 0o700)

    def test_existing_loose_permissions_are_tightened(self):
        self.path.write_text(json.dumps({"version": 1, "lockers": {}}))
        os.chmod(self.path, 0o644)

        Store.open(self.path).put(self.entry())

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_newer_version_is_read_only(self):
        self.path.write_text(json.dumps({"version": 99, "lockers": {}}))
        store = Store.open(self.path)

        self.assertTrue(store.readonly)
        with self.assertRaises(ReadOnlyStore):
            store._save()


if __name__ == "__main__":
    unittest.main()
