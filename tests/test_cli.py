"""Tests for the locker CLI: argument parsing, list output, and exit codes."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from locker.cli import main
from locker.store import Store


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "lockers.json"

    def seed(self, *pairs):
        store = Store.open(self.path)
        for name, command in pairs:
            store.add(name, command)
        return store

    def run_cli(self, argv, env=None):
        """Run main(), capturing stdout and stderr."""
        environ = {"LOCKER_FILE": str(self.path)}
        if env is not None:
            environ.update(env)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv)
        return code, out.getvalue(), err.getvalue()


class ListTests(CliTestCase):
    def test_list_empty_store(self):
        code, out, err = self.run_cli(["list"])

        self.assertEqual(code, 0)
        self.assertIn("No lockers yet", out)
        self.assertEqual(err, "")

    def test_list_shows_name_and_command(self):
        self.seed(
            ("qwen3.8", "llama-server -hf Qwen/Qwen3-8B"),
            ("llama70b", "llama-server -hf meta-llama/Llama-3-70B"),
        )

        code, out, err = self.run_cli(["list"])

        self.assertEqual(code, 0)
        self.assertIn("qwen3.8", out)
        self.assertIn("llama70b", out)
        self.assertIn("llama-server -hf Qwen/Qwen3-8B", out)

        rows = [
            line
            for line in out.splitlines()
            if line.startswith(("qwen3.8", "llama70b"))
        ]
        self.assertEqual(len(rows), 2)
        # Commands are column-aligned, so the command starts at the same offset.
        self.assertEqual(rows[0].index("llama-server"), rows[1].index("llama-server"))

    def test_list_json(self):
        self.seed(("a", "cmd-a"), ("b", "cmd-b"))

        code, out, err = self.run_cli(["--file", str(self.path), "list", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([item["name"] for item in data], ["a", "b"])
        for key in ("name", "command", "hf_repo"):
            self.assertIn(key, data[0])

    def test_list_truncates_long_commands(self):
        long_command = "x" * 300
        self.seed(("long", long_command))

        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("…", out)
        self.assertNotIn(long_command, out)

        code, out, _ = self.run_cli(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertIn(long_command, out)
        self.assertNotIn("…", out)

    def test_file_flag_overrides_env(self):
        other = self.dir / "other.json"
        Store.open(other).add("from-other-file", "cmd")
        self.seed(("from-env", "cmd"))

        code, out, _ = self.run_cli(["--file", str(other), "list"])

        self.assertEqual(code, 0)
        self.assertIn("from-other-file", out)
        self.assertNotIn("from-env", out)

    def test_no_args_lists_lockers(self):
        self.seed(("only", "cmd"))

        code, out, _ = self.run_cli([])

        self.assertEqual(code, 0)
        self.assertIn("only", out)

    def test_unknown_subcommand_is_usage_error(self):
        code, out, err = self.run_cli(["bogus"])

        self.assertEqual(code, 2)
        self.assertIn("bogus", err)

    def test_corrupt_store_exits_one_with_message(self):
        self.path.write_text("{ not json")

        code, out, err = self.run_cli(["list"])

        self.assertEqual(code, 1)
        self.assertIn(str(self.path), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
