# Launch Locker — Design

**Date:** 2026-10-03
**Status:** Approved for planning
**Repo:** https://github.com/craigmcgrain-spec/Launch-Locker

## 1. Purpose

Launch Locker stores the terminal command used to launch a local AI model and pairs it
with a name, so the command can be launched later by name:

```
locker launch qwen3.8
```

Commands are managed in a small local web UI where you paste a command and give it a name.
The UI also shows the Hugging Face model card for the model the command refers to.

**Intended user:** one person on one machine. This is a personal tool, not a service.

**Success criteria:**

- `locker launch <name>` runs the stored command in the foreground; Ctrl-C and signals
  belong to the model process.
- `locker ui` opens a browser page that lists lockers, shows the model card for the
  detected Hugging Face repo, and lets you add, edit, and delete entries.
- A locker added in the UI is immediately visible to `locker list`.
- Everything works with the network unplugged, except the model card, which degrades
  to a short "unavailable" message.
- Install is one command (`uv tool install .`) and pulls in no third-party packages.

**Explicit non-goals:** no daemon or background services, no authentication, no remote or
multi-user access, no sync between machines, no runtime-specific integrations (Ollama,
vLLM, LM Studio), no arg passthrough or placeholders, no "run" button in the browser.

## 2. Architecture

A small package where one module owns the data and both frontends are thin shells over it.

```
pyproject.toml              # name=launch-locker, console script `locker`,
                            # requires-python >=3.11, zero dependencies
src/locker/__init__.py
src/locker/store.py         # THE owner of the locker file: load, save, resolve, validate
src/locker/hf.py            # detect repo from a command, fetch card, cache, offline fallback
src/locker/md.py            # markdown -> HTML, escape-first, no raw HTML passthrough
src/locker/server.py        # http.server: HTML routes + small JSON API
src/locker/cli.py           # argparse subcommands; the only module that calls exec
src/locker/static/index.html
tests/test_store.py
tests/test_hf.py
tests/test_md.py
tests/test_cli.py
tests/test_server.py
README.md
LICENSE                     # MIT
```

**Dependency direction is one-way and enforced by review:** `cli.py` and `server.py`
import `store.py`; `server.py` additionally imports `hf.py` and `md.py`. Nothing imports
`cli.py` or `server.py`. This is what makes the store and the HF client testable without a
terminal or a socket, and `store.py` is where data loss would happen, so it is the module
that most needs to be testable in isolation.

**No network on the launch path.** `locker launch` reads local JSON and `exec`s it. Only
`server.py` calls `hf.py`, and only when rendering a model card. This is a deliberate
property: the command you run most often must not depend on a network or a second process.

`pyproject.toml` requires Python 3.11+ so it installs on any modern interpreter; development
and CI run on the 3.14 interpreter present on the development machine.

## 3. Data model

One JSON file, an object keyed by locker name, so uniqueness is structural:

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

| Field | Required | Notes |
|---|---|---|
| `command` | yes | Non-empty string. Stored verbatim, never normalised or re-quoted. |
| `hf_repo` | no | `owner/name`. When absent, the UI detects it from the command at display time. When present it is authoritative. |
| `created` / `updated` | yes | ISO 8601 UTC, seconds precision, `Z` suffix. Set by `store.py`, not by callers. |

`version` exists so a future format change can be detected. A file whose `version` is
greater than the version this build supports is **read but never written**: the CLI refuses
mutating commands with a clear message rather than downgrading the user's file.

**Names** are free-form non-empty strings, trimmed of surrounding whitespace. They must not
begin with `-`, because argparse would read such a name as an option flag and the locker
would become unlaunchable. They are unique **case-insensitively**: `Qwen` and `qwen` cannot
coexist, because case-insensitive lookup would otherwise be ambiguous. Both rules are
enforced at write time.

### 3.1 Location

Precedence, highest first:

1. `--file PATH` (global flag on every subcommand)
2. `LOCKER_FILE` environment variable
3. `$XDG_CONFIG_HOME/locker/lockers.json`
4. `~/.config/locker/lockers.json`

A missing file means an empty store, not an error. An unreadable or corrupt file is an
error that stops the operation.

### 3.2 Write safety

- **Atomic:** write to a temp file in the same directory, `flush` + `fsync`, then
  `os.replace`. An interrupted write can never leave a half-written locker file.
- **Permissions:** directory `0700`, file `0600`. Launch commands routinely embed
  credentials such as `HF_TOKEN`; a world-readable file of shell commands is a credential
  leak.
- **Never clobber a corrupt file.** `load()` raises `StoreCorrupt` with the path and the
  JSON error. No mutating command proceeds after a failed load, so a parse error cannot
  result in an empty store overwriting real data. This is the single most important
  behaviour in the store.
- **Shape is validated, not assumed.** A file that parses as JSON but does not match the
  schema — `lockers` is a list rather than an object, an entry is not an object, or
  `command` is missing or not a string — raises `StoreCorrupt` like any other bad file. It
  must never surface as an `AttributeError` or `TypeError` traceback, and it must never be
  silently coerced into an empty store.
- **Concurrency:** single user, last write wins, atomic replace. No locking. A documented
  limitation, not an oversight.

## 4. CLI

```
locker launch <name>                  # resolve, then exec
locker list [--json]                  # name, command, repo
locker add <name> [-c CMD] [--hf REPO]     # -c omitted: read command from stdin
locker edit <name> [-c CMD] [--hf REPO]    # no flags: open command in $EDITOR
locker rm <name> [--yes]
locker ui [--port N] [--no-open] [--refresh]
locker --file PATH <subcommand>       # global
locker                                # no args: print the locker list
```

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Locker not found, bad or corrupt store |
| 2 | Usage error |
| *n* | For `launch`, the exit code of the launched command passes through (e.g. 127) |

### 4.1 `launch`

1. Resolve the name. Exact match, then case-insensitive match. If nothing matches, exit 1
   with `difflib.get_close_matches` suggestions. Ambiguity cannot occur because of the
   case-insensitive uniqueness rule.
2. Print `→ running: <command>` **to stderr**. Sending it to stdout would corrupt
   `locker launch x | jq`.
3. `os.execvp("/bin/sh", ["sh", "-c", command])`.

`execvp` rather than `subprocess`: the CLI replaces itself with the model server, so
Ctrl-C, signals, and stdio belong to the model process outright, with no wrapper process
lingering and no signal-forwarding code to get wrong. The command runs through `sh -c`
because pasted launch commands contain env assignments, `cd &&`, and pipes; splitting them
into argv is lossy and a source of quoting bugs.

`launch` is the only place in the codebase that executes anything.

### 4.2 `edit` without flags

Opens the command in `$EDITOR` (falling back to `vi`) via a temp file, and reads the result
back on save. An unchanged file is a no-op.

## 5. Web UI

`locker ui` binds `127.0.0.1` — **loopback only, never `0.0.0.0`** — default port 8765,
and if that port is in use it tries the next available port rather than failing. It prints
the URL and opens the browser with `webbrowser.open` unless `--no-open` is given.

### 5.1 Pages and routes

| Method | Path | Behavior |
|---|---|---|
| GET | `/` | Locker table + add form |
| POST | `/lockers` | Create, then 303 redirect to `/` |
| GET | `/lockers/<name>/edit` | Edit form |
| POST | `/lockers/<name>` | Update, then 303 redirect to `/` |
| POST | `/lockers/<name>/delete` | Delete, then 303 redirect to `/` |
| GET | `/api/card?repo=owner/name` | JSON: model card metadata + rendered HTML |
| GET | `/static/<file>` | Static assets |

Forms are plain HTML `POST` with a 303 redirect, so the UI works with JavaScript disabled.
Vanilla JS adds exactly three things: the copy-to-clipboard button, lazy model-card
loading, and a live "detected repo" hint while pasting a command.

Names appearing in URLs are percent-encoded. Unknown routes return 404, wrong methods 405,
malformed form data 400.

### 5.2 Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Launch Locker                                          [127.0.0.1]   │
├───────────────────────────────────┬──────────────────────────────────┤
│ LOCKERS                           │ MODEL CARD                       │
│ ┌───────────────────────────────┐ │ Qwen/Qwen3-8B                    │
│ │ qwen3.8            [copy] [×] │ │ Qwen · text-generation           │
│ │ llama-server -hf Qwen/Qwen3…  │ │ ↓ 10,703,016   ♥ 2,069           │
│ │ Qwen/Qwen3-8B      [edit]     │ │ apache-2.0 · updated 3d ago      │
│ ├───────────────────────────────┤ │ [qwen3] [safetensors] [convers…] │
│ │ llama3-70b         [copy] [×] │ │ ──────────────────────────────── │
│ │ llama-server -hf meta-llam…   │ │ ▾ Model card                     │
│ │ meta-llama/Llama-3-70B [edit] │ │   # Qwen3-8B                     │
│ └───────────────────────────────┘ │   …rendered README…              │
│                                   │                                  │
│ ADD A LOCKER                      │                                  │
│ name    [___________________]     │                                  │
│ command [___________________]     │                                  │
│ hf repo [auto] (optional)         │                                  │
│                    [save]         │                                  │
└───────────────────────────────────┴──────────────────────────────────┘
```

The model card panel shows the card for the selected locker, defaulting to the first
locker in the list. It loads via `/api/card` after page paint, so a slow or failed
Hugging Face request never delays the page. With an empty store the table is replaced by a
one-line empty state and the card panel is hidden, leaving the add form as the only action
on the page.

### 5.3 Local security

A page on the internet can `POST` to `http://127.0.0.1:8765/` from your browser, which
would let a hostile site silently drop shell commands into the locker. Two cheap
mitigations, both required:

- **Host header check.** Reject any request whose `Host` is not `127.0.0.1:<port>` or
  `localhost:<port>` with 403. This also blunts DNS-rebinding.
- **Origin check on mutating methods.** If `Origin` or `Referer` is present on a `POST`,
  it must match the server's own origin. Absent headers are allowed, since the plain HTML
  form flow from our own page may omit them.

No authentication. The server is loopback-only and single-user, so a token would add a
secret to manage without changing the threat model.

## 6. Model card

### 6.1 Detecting the repo

`hf.py` scans the command for a Hugging Face repo id, in this order:

1. Explicit flags: `-hf <repo>`, `--hf <repo>`, `--hf-repo <repo>`, `--repo-id <repo>`.
2. `--model <repo>` / `-m <repo>` / `--model-id <repo>`, taking that flag's value directly.
3. Any bare `owner/name` token elsewhere in the command.

A **repo id** is exactly two non-empty segments separated by one `/`, where each segment
matches `[A-Za-z0-9._-]+`. A candidate is rejected when it is a URL, an absolute or
home-relative path (`/models/...`, `~/...`, `./...`), a path with a known model-file
extension (`.gguf`, `.safetensors`, `.bin`, `.pt`), or when either segment is missing or
contains a second `/`.

For step 2 the value is taken as a candidate only if it satisfies the repo-id shape; a
local path passed to `--model` is therefore skipped, which is the common case for
`llama-server --model ./models/qwen.gguf`.

Detection returns an ordered list of candidates, not a single guess. The UI tries them in
order and uses the first that the API resolves successfully, labelling the card with the
repo id it settled on so a wrong guess is visible rather than silent.

A stored `hf_repo` field, when present, skips detection entirely. It is authoritative and
may be edited in the UI.

### 6.2 Fetching and caching

- `GET https://huggingface.co/api/models/<repo>` for metadata.
- `GET https://huggingface.co/<repo>/raw/main/README.md` for card text.
- Cache under `$XDG_CACHE_HOME/locker/hf/<owner>__<name>/`, defaulting to
  `~/.cache/locker/hf/<owner>__<name>/` (metadata JSON and README stored together), TTL 24
  hours, overridable per request (`?refresh=1`) and by `locker ui --refresh`, which
  bypasses the cache for the session. Cache writes are atomic, same as the store.
- 5 second timeout, no retries. A request that fails does not make the UI hang.
- An identifiable `User-Agent` naming Launch Locker and its version.
- README content is truncated at 200 KB before caching to bound memory and disk.

### 6.3 Failure and offline behavior

The card panel degrades in this order, and never blocks the page:

1. Fresh cache or successful fetch: full card.
2. Stale cache: full card with `cached, updated <age>`.
3. No cache, request fails: a one-line `Model card unavailable (offline?)` plus a plain
   link to `https://huggingface.co/<repo>` for manual checking.
4. Detection found no candidate at all: `No Hugging Face repo detected in this command`,
   with the optional repo field pointed out as the fix.

Because step 1 tries candidates in order, a 404 on one candidate advances to the next. Only
when every candidate has been rejected as nonexistent is the result reported as
`Hugging Face has no repo named <first candidate>`. A 404 is kept distinct from a network
failure throughout, because the cause and the fix differ.

## 7. Markdown rendering

The standard library has no markdown parser, and vendoring one would break the zero
dependency rule. `md.py` is a deliberately minimal renderer (~120 lines) whose security
property matters more than its fidelity:

**`html.escape` runs first, over the whole document.** Every subsequent transform operates
on already-escaped text. Raw HTML in a README therefore cannot reach the page as markup —
`<script>alert(1)</script>` becomes visible literal text. README content is untrusted
external input and is treated as such.

Supported on top of the escaped text: ATX headings, fenced code blocks, inline code, bold,
italic, bullet and numbered lists, blockquotes, horizontal rules, and links.

Link `href`s are restricted to `http` and `https`; anything else (`javascript:`, `data:`)
renders as plain text. Images are **not** loaded from remote URLs — `![alt](url)` renders
as a link, to avoid turning a README into a tracking beacon against the reader.

Unsupported constructs render as literal escaped text, never as broken HTML. Tables are
left as preformatted text in this version.

## 8. Error handling

| Situation | Behavior |
|---|---|
| Store file missing | Empty store, exit 0 on reads |
| Store file corrupt | `StoreCorrupt` with path and parse error; mutating commands refuse to run; file is never overwritten |
| `version` newer than supported | Read-only mode with an explanatory message |
| Locker not found | Exit 1, with `difflib` suggestions |
| Name collides case-insensitively | Rejected at write time, naming the existing entry |
| Port 8765 in use | Try the next free port; if none, clear error |
| `$EDITOR` unset | Fall back to `vi` |
| HF request times out / offline | Cached card, else "unavailable" message (see 6.3) |
| HF repo 404 | Distinct "no repo named X" message |
| Form data invalid | 400, no partial write |
| Request from a non-loopback Host | 403 |

## 9. Testing

Standard library `unittest` only, run with `python -m unittest discover -s tests`. No test
touches the real network: `hf.py` accepts an injectable opener, and tests pass a stub.

| File | Covers |
|---|---|
| `test_store.py` | Round-trip; atomic write leaves no temp files; case-insensitive uniqueness rejected; resolution order (exact before case-insensitive); suggestions; **corrupt file raises and is not clobbered**; missing file = empty store; `0600`/`0700` permissions; `--file` > `LOCKER_FILE` > XDG precedence; newer `version` refuses writes |
| `test_md.py` | XSS vectors (`<script>`, `<img onerror>`, event-handler attributes, `javascript:` and `data:` hrefs); code-fence escaping; unclosed fences; 200 KB truncation |
| `test_hf.py` | Detection positive and negative cases (`-hf`, `--model`, bare token, local paths, URLs, `.gguf` paths); candidate ordering and fall-through on 404; no-candidate-detected result; cache hit, miss and TTL expiry; offline fallback; 404 versus timeout |
| `test_cli.py` | Argparse parsing per subcommand; `launch` builds `["sh", "-c", command]` with `execvp` mocked, and asserts nothing else executed; `list`/`add`/`edit`/`rm` against a temp `LOCKER_FILE`; exit codes |
| `test_server.py` | Bound to port 0 (ephemeral) in a thread: `GET /` 200; empty store renders the empty state; `POST /lockers` 303; non-loopback `Host` 403; `/api/card` with a stubbed fetcher; unknown route 404; wrong method 405 |

## 10. Repository setup

- Wire `origin` to `https://github.com/craigmcgrain-spec/Launch-Locker`.
- `README.md` covering install, the CLI reference, the UI, and the file format.
- `LICENSE`: MIT.
- Set a repository-local git author identity, since the machine-wide config is currently
  `GitHub Copilot <copilot@github.com>`.

## 11. Deferred

Named so the omissions are decisions rather than oversights. Revisit only on real
annoyance:

- Background/detached launches with PID files and logs (`locker up` / `down` / `logs`).
- Per-locker `env` and `cwd` fields; today they are written into the command string.
- `{placeholder}` templating and CLI arg passthrough.
- Rendering markdown tables, or swapping `md.py` for a vendored renderer.
- More than one locker file, or per-project `.locker.json` discovery.
- Import/export and shell-completion generation.
