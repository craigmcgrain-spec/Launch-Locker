"""Tests for the locker CLI: parsing, list, launch, add/edit/rm, exit codes."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from locker.cli import cmd_launch, main
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

    def write_editor(self, body: str) -> Path:
        script = self.dir / "editor.sh"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        return script

    def run_cli(self, argv, env=None, stdin=None):
        """Run main(), capturing output. An env value of None removes that key."""
        environ = {"LOCKER_FILE": str(self.path)}
        if env is not None:
            environ.update(env)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {k: v for k, v in environ.items() if v is not None},
                    clear=False,
                )
            )
            for key, value in environ.items():
                if value is None:
                    os.environ.pop(key, None)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
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


class LaunchTests(CliTestCase):
    def test_launch_execs_sh_c_with_exact_command(self):
        command = "export FOO=\"a\" && echo $HOME 'x'\nllama-server --port 8080"
        self.seed(("x", command))
        stub = mock.Mock()

        store = Store.open(self.path)
        with contextlib.redirect_stderr(io.StringIO()):
            code = cmd_launch(store, "x", exec_fn=stub)

        self.assertEqual(code, 0)
        stub.assert_called_once_with("/bin/sh", ["sh", "-c", command])
        self.assertEqual(store.get("x").command, command)

    def test_launch_banner_goes_to_stderr(self):
        self.seed(("x", "echo hi"))
        stub = mock.Mock()
        out, err = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cmd_launch(Store.open(self.path), "x", exec_fn=stub)

        self.assertEqual(out.getvalue(), "")
        self.assertIn("→ running: echo hi", err.getvalue())

    def test_launch_missing_name_exits_one_with_suggestions(self):
        self.seed(("qwen3.8", "cmd"))
        stub = mock.Mock()

        with contextlib.redirect_stderr(io.StringIO()):
            code = cmd_launch(Store.open(self.path), "qwen38", exec_fn=stub)

        self.assertEqual(code, 1)
        self.assertEqual(stub.call_count, 0)

        code, out, err = self.run_cli(["launch", "qwen38"])
        self.assertEqual(code, 1)
        self.assertIn("qwen3.8", err)
        self.assertIn("did you mean", err)

    def test_launch_resolves_case_insensitively(self):
        self.seed(("qwen3.8", "echo hi"))
        stub = mock.Mock()

        with contextlib.redirect_stderr(io.StringIO()):
            code = cmd_launch(Store.open(self.path), "QWEN3.8", exec_fn=stub)

        self.assertEqual(code, 0)
        stub.assert_called_once_with("/bin/sh", ["sh", "-c", "echo hi"])

    def test_launch_does_not_touch_the_network(self):
        self.seed(("x", "echo hi"))
        stub = mock.Mock()

        with mock.patch("urllib.request.urlopen") as urlopen:
            with contextlib.redirect_stderr(io.StringIO()):
                cmd_launch(Store.open(self.path), "x", exec_fn=stub)

        self.assertEqual(stub.call_count, 1)
        self.assertEqual(urlopen.call_count, 0)


class AddTests(CliTestCase):
    def test_add_with_c_flag(self):
        code, out, err = self.run_cli(["add", "qwen3.8", "-c", "echo hi"])

        self.assertEqual(code, 0)
        self.assertIn("qwen3.8", out)
        self.assertEqual(Store.open(self.path).get("qwen3.8").command, "echo hi")

    def test_add_reads_stdin_when_c_omitted(self):
        code, out, err = self.run_cli(["add", "x"], stdin="echo one\necho two\n")

        self.assertEqual(code, 0)
        self.assertEqual(Store.open(self.path).get("x").command, "echo one\necho two")

    def test_add_rejects_empty_stdin(self):
        code, out, err = self.run_cli(["add", "x"], stdin="")

        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())
        self.assertNotIn("Traceback", err)

    def test_add_duplicate_name_exits_one(self):
        self.seed(("x", "cmd"))

        code, out, err = self.run_cli(["add", "x", "-c", "other"])

        self.assertEqual(code, 1)
        self.assertIn("x", err)
        self.assertEqual(Store.open(self.path).get("x").command, "cmd")

    def test_add_leading_dash_name_exits_one(self):
        code, out, err = self.run_cli(["add", "-c", "echo", "--", "-foo"])

        self.assertEqual(code, 1)
        self.assertIn("cannot begin with", err)
        self.assertEqual(Store.open(self.path).entries(), [])


class EditTests(CliTestCase):
    def test_edit_replaces_command(self):
        store = Store.open(self.path)
        with mock.patch("locker.store.utcnow", return_value="2026-01-01T00:00:00Z"):
            store.add("x", "old")

        with mock.patch("locker.store.utcnow", return_value="2026-01-02T00:00:00Z"):
            code, out, err = self.run_cli(["edit", "x", "-c", "new"])

        self.assertEqual(code, 0)
        entry = Store.open(self.path).get("x")
        self.assertEqual(entry.command, "new")
        self.assertEqual(entry.created, "2026-01-01T00:00:00Z")
        self.assertEqual(entry.updated, "2026-01-02T00:00:00Z")

    def test_edit_hf_flag_pins_repo(self):
        self.seed(("x", "cmd"))

        code, out, err = self.run_cli(["edit", "x", "--hf", "Qwen/Qwen3-8B"])

        self.assertEqual(code, 0)
        self.assertEqual(Store.open(self.path).get("x").hf_repo, "Qwen/Qwen3-8B")

    def test_edit_uses_editor_when_no_flags(self):
        self.seed(("x", "echo hi"))
        log = self.dir / "editor-path.txt"
        editor = self.write_editor(
            'printf "%s" "$1" > "$EDITOR_LOG"\nprintf " # edited" >> "$1"\n'
        )

        code, out, err = self.run_cli(
            ["edit", "x"], env={"EDITOR": str(editor), "EDITOR_LOG": str(log)}
        )

        self.assertEqual(code, 0)
        self.assertEqual(Store.open(self.path).get("x").command, "echo hi # edited")
        temp_path = Path(log.read_text())
        self.assertFalse(temp_path.exists(), "temp editor file should be deleted")

    def test_edit_unchanged_editor_is_noop(self):
        self.seed(("x", "echo hi"))
        before = self.path.read_bytes()

        code, out, err = self.run_cli(["edit", "x"], env={"EDITOR": "/bin/true"})

        self.assertEqual(code, 0)
        # Distinguishes a real no-op from the subcommand being lost and `list`
        # running instead, which also leaves the file untouched and returns 0.
        self.assertIn("unchanged", out)
        self.assertEqual(self.path.read_bytes(), before)

    def test_edit_missing_editor_falls_back_to_vi(self):
        self.seed(("x", "echo hi"))

        with mock.patch("locker.cli.subprocess.call") as call:
            code, out, err = self.run_cli(["edit", "x"], env={"EDITOR": None})

        self.assertEqual(code, 0)
        argv = call.call_args.args[0]
        self.assertEqual(argv[0], "vi")

    def test_edit_missing_name_exits_one(self):
        code, out, err = self.run_cli(["edit", "nope", "-c", "cmd"])

        self.assertEqual(code, 1)
        self.assertIn("nope", err)


class RemoveTests(CliTestCase):
    def test_rm_requires_confirmation_when_not_yes(self):
        self.seed(("x", "cmd"))

        code, out, err = self.run_cli(["rm", "x"], stdin="n\n")

        self.assertEqual(code, 0)
        self.assertEqual(Store.open(self.path).get("x").command, "cmd")

    def test_rm_yes_deletes(self):
        self.seed(("x", "cmd"))

        code, out, err = self.run_cli(["rm", "x", "--yes"])

        self.assertEqual(code, 0)
        self.assertEqual(Store.open(self.path).entries(), [])


if __name__ == "__main__":
    unittest.main()
