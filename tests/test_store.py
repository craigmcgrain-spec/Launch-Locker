"""Tests for locker.store: path resolution, persistence, CRUD, and durability."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from locker.store import (
    Entry,
    InvalidCommand,
    InvalidName,
    NameConflict,
    NotFound,
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


class StoreTestCase(unittest.TestCase):
    """A temp store path plus the fixtures every store test needs."""

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

    def write_raw(self, lockers):
        self.path.write_text(
            json.dumps({"version": 1, "lockers": lockers}), encoding="utf-8"
        )


class PersistenceTests(StoreTestCase):
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


class CrudTests(StoreTestCase):
    def test_add_and_get(self):
        store = Store.open(self.path)

        entry = store.add("qwen3.8", "llama-server -hf Qwen/Qwen3-8B")

        self.assertIsInstance(entry, Entry)
        self.assertEqual(store.get("qwen3.8"), entry)
        self.assertEqual(
            json.loads(self.path.read_text())["lockers"]["qwen3.8"]["command"],
            "llama-server -hf Qwen/Qwen3-8B",
        )

    def test_command_roundtrips_byte_exact(self):
        command = (
            "export FOO=\"double\" && echo 'single' $HOME `date` | grep x\n"
            "llama-server --model ./m.gguf"
        )
        Store.open(self.path).add("weird", command)

        reopened = Store.open(self.path)

        self.assertEqual(repr(reopened.get("weird").command), repr(command))

    def test_unicode_command_and_name(self):
        name = "qwen-3.8-日本語-🚀"
        command = "llama-server --prompt '你好'"
        Store.open(self.path).add(name, command)

        raw = self.path.read_text(encoding="utf-8")

        self.assertIn("日本語", raw)
        self.assertIn("你好", raw)
        self.assertNotIn("\\u", raw)
        self.assertEqual(Store.open(self.path).get(name).command, command)

    def test_name_uniqueness_is_case_insensitive(self):
        store = Store.open(self.path)
        store.add("Qwen", "cmd")

        with self.assertRaises(NameConflict):
            store.add("qwen", "cmd2")

        self.assertEqual(store.resolve("QWEN").name, "Qwen")

    def test_name_rejects_empty_and_whitespace(self):
        store = Store.open(self.path)

        for bad in ("", "   "):
            with self.subTest(name=bad):
                with self.assertRaises(InvalidName):
                    store.add(bad, "cmd")

    def test_name_rejects_leading_dash(self):
        store = Store.open(self.path)

        with self.assertRaises(InvalidName):
            store.add("-foo", "cmd")

        self.assertEqual(store.add(" foo ", "cmd").name, "foo")

    def test_name_is_stripped(self):
        store = Store.open(self.path)
        store.add("  x  ", "cmd")

        self.assertEqual(store.get("x").command, "cmd")

    def test_resolve_prefers_exact(self):
        now = utcnow()
        self.write_raw(
            {
                "abc": {"command": "lower", "created": now, "updated": now},
                "ABC": {"command": "upper", "created": now, "updated": now},
            }
        )

        self.assertEqual(Store.open(self.path).resolve("ABC").command, "upper")

    def test_resolve_missing_raises_notfound(self):
        store = Store.open(self.path)
        store.add("x", "cmd")

        with self.assertRaises(NotFound):
            store.resolve("nope")

    def test_suggestions(self):
        store = Store.open(self.path)
        store.add("qwen3.8", "cmd")

        self.assertIn("qwen3.8", store.suggestions("qwen38"))
        self.assertEqual(store.suggestions("completely-unrelated-thing"), [])

    def test_update_changes_command_and_updated_stamp(self):
        store = Store.open(self.path)
        with mock.patch("locker.store.utcnow", return_value="2026-01-01T00:00:00Z"):
            store.add("x", "one")

        with mock.patch("locker.store.utcnow", return_value="2026-01-02T00:00:00Z"):
            updated = store.update("x", command="two")

        self.assertEqual(updated.command, "two")
        self.assertEqual(updated.created, "2026-01-01T00:00:00Z")
        self.assertEqual(updated.updated, "2026-01-02T00:00:00Z")

    def test_update_rejects_empty_command(self):
        store = Store.open(self.path)
        store.add("x", "cmd")

        with self.assertRaises(InvalidCommand):
            store.update("x", command="")

        self.assertEqual(store.get("x").command, "cmd")

    def test_remove(self):
        store = Store.open(self.path)
        store.add("x", "cmd")

        store.remove("x")

        self.assertEqual(store.entries(), [])
        with self.assertRaises(NotFound):
            store.remove("x")

    def test_mutators_persist_immediately(self):
        Store.open(self.path).add("x", "cmd")

        self.assertEqual(Store.open(self.path).get("x").command, "cmd")


if __name__ == "__main__":
    unittest.main()
