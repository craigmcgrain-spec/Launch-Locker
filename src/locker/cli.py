"""The command-line interface.

This is the only module in the package that executes a user's command; every
other module either reads data or renders it.
"""

from __future__ import annotations

import argparse
import json
import sys

from .store import Entry, Store, StoreError, resolve_store_path

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

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

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


COMMANDS = {
    "list": cmd_list,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits for --help and for usage errors; turn both into a code.
        return exc.code if isinstance(exc.code, int) else 2

    if args.command is None:
        args.command = "list"

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")

    try:
        store = Store.open(resolve_store_path(args.file))
        return handler(store, args)
    except StoreError as exc:
        print(f"locker: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
