# Launch Locker

Store the terminal command that launches a local AI model, pair it with a name, and run it
by name later.

```console
$ locker add qwen3.8 -c "llama-server -hf Qwen/Qwen3-8B --ctx-size 8192 --port 8080"
stored 'qwen3.8'

$ locker launch qwen3.8
→ running: llama-server -hf Qwen/Qwen3-8B --ctx-size 8192 --port 8080
```

Or paste the command into the web UI, where it also shows the Hugging Face model card for
the model you are launching:

```console
$ locker ui
Launch Locker is serving at http://127.0.0.1:8765/
```

No daemon, no background service, no third-party dependencies. `locker launch` reads a local
JSON file and `exec`s your command, so Ctrl-C and signals belong to the model process and
launching works with the network unplugged.

## Requirements

Python 3.11 or newer. Nothing else — the standard library covers all of it.

## Install

```console
$ uv tool install .          # or: pipx install .
```

Run once without installing:

```console
$ uvx --from . locker list
```

## CLI

```
locker launch <name>                        run a stored command in the foreground
locker list [--json]                        list stored lockers
locker add <name> [-c CMD] [--hf REPO]      -c omitted: read the command from stdin
locker edit <name> [-c CMD] [--hf REPO]     no flags: edit the command in $EDITOR
locker rm <name> [--yes]                    delete a locker
locker ui [--port N] [--no-open] [--refresh]
locker --file PATH <command>                use a specific locker file
locker                                      with no arguments, list your lockers
```

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | No such locker, or the locker file could not be read |
| 2 | Usage error |
| *n* | For `launch`, the exit code of the launched command (e.g. 127) |

`locker launch` replaces itself with `sh -c "<your command>"`, so the command may contain
environment assignments, pipes, `cd &&`, and newlines.

Names are matched exactly first, then case-insensitively, and a near miss gets suggestions:

```console
$ locker launch qwen38
locker: no locker named 'qwen38'
  did you mean: qwen3.8
```

## Web UI

`locker ui` serves a single page on **127.0.0.1 only** and opens it in your browser. You can
add, edit, and delete lockers, copy a command to the clipboard, and read the Hugging Face
model card for whichever locker you select.

The browser never runs anything. Nothing is launched from the page — you copy the command or
run `locker launch <name>` yourself.

The card is fetched from the public Hugging Face API and cached under
`$XDG_CACHE_HOME/locker/hf/` (default `~/.cache/locker/hf/`) for 24 hours. The repo is found
in your command automatically — `-hf owner/name`, `--model owner/name`, or a bare
`owner/name`, including llama.cpp's `-hf owner/name:QUANT` form. Set the optional **hf repo**
field to override the guess. A slow or failed request never delays the page: the card loads
after the page paints and falls back to the cached copy, or to a short "unavailable" message.

## The locker file

One JSON file, an object keyed by name:

```json
{
  "version": 1,
  "lockers": {
    "qwen3.8": {
      "command": "llama-server -hf Qwen/Qwen3-8B --ctx-size 8192 --port 8080",
      "hf_repo": "Qwen/Qwen3-8B",
      "created": "2026-10-03T12:00:00Z",
      "updated": "2026-10-03T12:00:00Z"
    }
  }
}
```

`hf_repo` is optional; when present it is authoritative and skips detection.

Location, highest precedence first:

1. `--file PATH`
2. `$LOCKER_FILE`
3. `$XDG_CONFIG_HOME/locker/lockers.json`
4. `~/.config/locker/lockers.json`

A missing file is simply an empty list of lockers. Writes are atomic, and the file is created
`0600` inside a `0700` directory. A file that cannot be parsed is reported and **never
overwritten** — Launch Locker will not replace a locker file it does not understand.

## Security

The locker file is a list of shell commands that run as you. Treat it with the care you would
give your `~/.bashrc`:

- It is created `0600` and re-tightened on every save, because launch commands often embed
  credentials such as `HF_TOKEN`.
- The web UI binds to `127.0.0.1` only, and refuses requests whose `Host` is not a loopback
  name or whose `Origin`/`Referer` is another site. Without those checks any page you visited
  could quietly POST commands into your locker.
- Commands are stored exactly as you wrote them and are never normalised, re-quoted, or
  shell-escaped, so what you see in `locker list --json` is what runs.

## Not included

Deliberately out of scope, so the omissions are decisions rather than surprises:

- Background or detached launches with PID files and logs
- Per-locker `env` and working-directory fields — put them in the command
- `{placeholder}` templating or CLI argument passthrough
- Markdown tables and remote images in model cards (images render as links, so a README
  cannot beacon back to its author)
- More than one locker file, or per-project discovery
- Authentication, remote access, sync, or sharing

## Development

```console
$ uv run python -m unittest discover -s tests -v
```

No test touches the network; the Hugging Face client takes an injectable opener.

The design is written up in `docs/superpowers/specs/2026-10-03-launch-locker-design.md`.
