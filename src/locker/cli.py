"""The command-line interface.

This is the only module in the package that executes a user's command; every
other module either reads data or renders it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Callable, NoReturn

from .store import Entry, NotFound, Store, StoreError, resolve_store_path

COMMAND_WIDTH = 60


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locker",
        description="Store local AI model launch commands by name and run them.",
    )
    parser.add_argument(
        "--file",
        dest="file",
        metavar="PATH",
        help="locker file to use (default: $LOCKER_FILE, then the XDG config dir)",
    )

    # dest must not be "command": -c/--command on the add/edit subparsers would
    # otherwise overwrite the subcommand name in the namespace, and argparse
    # applies that option's default even when the flag is absent, silently
    # turning `locker edit x` into `locker list`.
    sub = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    p_launch = sub.add_parser("launch", help="run a stored command in the foreground")
    p_launch.add_argument("name", help="name of the locker to launch")

    p_list = sub.add_parser("list", help="list stored lockers")
    p_list.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of a table"
    )

    p_add = sub.add_parser("add", help="store a new locker")
    p_add.add_argument("name", help="name to store the command under")
    p_add.add_argument(
        "-c", "--command", help="the command to run; omit to read it from stdin"
    )
    p_add.add_argument("--hf", dest="hf_repo", metavar="REPO", help="pin a Hugging Face repo")

    p_edit = sub.add_parser("edit", help="change a stored locker")
    p_edit.add_argument("name", help="name of the locker to change")
    p_edit.add_argument("-c", "--command", help="replacement command")
    p_edit.add_argument("--hf", dest="hf_repo", metavar="REPO", help="pin a Hugging Face repo")

    p_rm = sub.add_parser("rm", help="delete a locker")
    p_rm.add_argument("name", help="name of the locker to delete")
    p_rm.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p_ui = sub.add_parser("ui", help="open the local web UI")
    p_ui.add_argument("--port", type=int, default=8765, help="port to bind (default: 8765)")
    p_ui.add_argument("--no-open", action="store_true", dest="no_open", help="do not open a browser")
    p_ui.add_argument("--refresh", action="store_true", help="ignore the model-card cache")

    return parser


def _truncate(text: str, width: int) -> str:
    """Collapse to one line and shorten with an ellipsis if needed."""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def format_table(entries: list[Entry]) -> str:
    """Render entries as an aligned table, truncating long commands."""
    rows = [
        (entry.name, _truncate(entry.command, COMMAND_WIDTH), entry.hf_repo or "")
        for entry in entries
    ]
    headers = ("NAME", "COMMAND", "REPO")
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(3)]

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [render(headers), render(tuple("-" * width for width in widths))]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def entries_to_json(entries: list[Entry]) -> str:
    """Render entries as JSON, with commands intact rather than truncated."""
    return json.dumps(
        [
            {
                "name": entry.name,
                "command": entry.command,
                "hf_repo": entry.hf_repo,
                "created": entry.created,
                "updated": entry.updated,
            }
            for entry in entries
        ],
        ensure_ascii=False,
        indent=2,
    )


def cmd_list(store: Store, args: argparse.Namespace) -> int:
    entries = store.entries()

    if getattr(args, "as_json", False):
        print(entries_to_json(entries))
    elif not entries:
        print("No lockers yet. Add one with: locker add <name> -c '<command>'")
    else:
        print(format_table(entries))

    return 0


def cmd_launch(
    store: Store,
    name: str,
    exec_fn: Callable[[str, list[str]], NoReturn] = os.execvp,
) -> int:
    """Replace this process with the stored command.

    ``execvp`` rather than a subprocess, so Ctrl-C, signals, and stdio belong to
    the model process outright with no wrapper lingering in between. The command
    goes through ``sh -c`` because pasted launch commands contain env
    assignments, ``cd &&``, and pipes. ``exec_fn`` is injectable so tests never
    replace a process.
    """
    try:
        entry = store.resolve(name)
    except NotFound:
        print(f"locker: no locker named {name!r}", file=sys.stderr)
        for guess in store.suggestions(name):
            print(f"  did you mean: {guess}", file=sys.stderr)
        return 1

    # stderr, so a pipe on stdout stays clean.
    print(f"→ running: {entry.command}", file=sys.stderr)
    exec_fn("/bin/sh", ["sh", "-c", entry.command])
    return 0


def _run_launch(store: Store, args: argparse.Namespace) -> int:
    return cmd_launch(store, args.name)


def read_command_from_stdin() -> str:
    """Read a command from stdin, stripping only the outer whitespace.

    Internal newlines are preserved: multi-line launch commands are normal.
    """
    return sys.stdin.read().strip()


def cmd_add(store: Store, args: argparse.Namespace) -> int:
    command = args.command if args.command is not None else read_command_from_stdin()

    entry = store.add(args.name, command, hf_repo=args.hf_repo)
    print(f"stored {entry.name!r}")
    return 0


def _edit_in_editor(current: str) -> str | None:
    """Open the command in ``$EDITOR`` and return what was saved.

    ``subprocess`` is used here for the editor only. It never executes a stored
    command, so the rule that only ``cmd_launch`` executes anything still holds.

    ``$EDITOR`` is split like a shell word, because the documented forms carry
    arguments (``code -w``). Returns None when the editor cannot be started,
    having reported why, so the caller can exit cleanly instead of tracing back.
    """
    editor = os.environ.get("EDITOR") or "vi"
    try:
        argv = shlex.split(editor)
    except ValueError:
        argv = [editor]
    if not argv:
        argv = ["vi"]

    fd, path = tempfile.mkstemp(prefix="locker-", suffix=".cmd")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(current)

        try:
            subprocess.call([*argv, path])
        except OSError as exc:
            print(f"locker: cannot run editor {argv[0]!r}: {exc}", file=sys.stderr)
            return None

        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def cmd_edit(store: Store, args: argparse.Namespace) -> int:
    entry = store.resolve(args.name)

    command = args.command
    if command is None and args.hf_repo is None:
        command = _edit_in_editor(entry.command)
        if command is None:
            return 1

    # No change means no write: an untouched editor must not churn `updated` or
    # rewrite the file.
    if command == entry.command and args.hf_repo is None:
        print(f"unchanged {entry.name!r}")
        return 0

    updated = store.update(args.name, command=command, hf_repo=args.hf_repo)
    print(f"updated {updated.name!r}")
    return 0


def cmd_rm(store: Store, args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"Remove {args.name!r}? [y/N] ", end="")
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0

    store.remove(args.name)
    print(f"removed {args.name!r}")
    return 0


def cmd_ui(store: Store, args: argparse.Namespace) -> int:
    # Imported here so that importing the CLI pulls in neither the HTTP server
    # nor the model-card client, keeping the launch path offline and minimal.
    from .server import serve

    return serve(
        store.path,
        port=args.port,
        open_browser=not args.no_open,
        refresh=args.refresh,
    )


COMMANDS = {
    "list": cmd_list,
    "launch": _run_launch,
    "add": cmd_add,
    "edit": cmd_edit,
    "rm": cmd_rm,
    "ui": cmd_ui,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits for --help and for usage errors; turn both into a code.
        return exc.code if isinstance(exc.code, int) else 2

    if args.subcommand is None:
        args.subcommand = "list"

    handler = COMMANDS.get(args.subcommand)
    if handler is None:
        parser.error(f"unknown command: {args.subcommand}")

    try:
        store = Store.open(resolve_store_path(args.file))
        return handler(store, args)
    except StoreError as exc:
        print(f"locker: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
