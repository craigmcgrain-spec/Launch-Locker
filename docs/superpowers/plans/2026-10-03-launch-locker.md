# Launch Locker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a zero-dependency Python CLI and local web UI that store named local-model launch commands and run them by name.

**Architecture:** One package where `store.py` alone owns the JSON locker file and both frontends (`cli.py`, `server.py`) are thin shells over it. `launch` `execvp`s through `sh -c`, so the model process owns the terminal. Only the UI's model-card panel touches the network, via `hf.py`, which caches to disk and degrades to a message when offline.

**Tech Stack:** Python 3.11+ standard library only (`argparse`, `http.server`, `json`, `urllib.request`, `html`, `difflib`, `unittest`). `hatchling` as build backend (build-time only). Package manager: `uv`.

**Spec:** `docs/superpowers/specs/2026-10-03-launch-locker-design.md`

## Global Constraints

- **Python `>=3.11`**; development and tests run on the 3.14 interpreter on this machine.
- **Zero third-party runtime dependencies.** Standard library only. `hatchling` is build-time only.
- **Nothing executes except `cli.launch`.** No other module may call `exec`, `system`, `Popen`, or `spawn`.
- **No network on the launch path.** Only `hf.py` performs network I/O, and only `server.py` imports it.
- **Tests never touch the network.** `hf.py` takes an injectable opener; tests pass a stub.
- **Store writes are atomic** (temp file in the same directory, `flush` + `fsync`, `os.replace`), directory `0700`, file `0600`. An existing file is re-tightened to `0600` on save.
- **A corrupt or wrongly-shaped store raises `StoreCorrupt` and is never overwritten.**
- **Commands are stored verbatim** — never normalised, re-quoted, or shell-escaped.
- **Names:** non-empty after strip, must not begin with `-`, unique case-insensitively.
- **Exit codes:** `0` success, `1` not found / bad store, `2` usage error; `launch` passes the launched command's exit code through.
- **The `→ running:` banner goes to stderr**, never stdout.
- **Server binds `127.0.0.1` only**, default port 8765, incrementing to the next free port if busy.
- **Markdown:** `html.escape` runs over the whole document before any transform; link `href`s restricted to `http`/`https`; remote images render as links, not `<img>`.
- **Cache** at `$XDG_CACHE_HOME/locker/hf/<owner>__<name>/`, default `~/.cache/...`, TTL 24h, 5s timeout, no retries.
- **Commit each task locally on `master`; never push.** The user authorised per-task local commits and will authorise the push separately. Push target when authorised is SSH: `git@github.com:craigmcgrain-spec/Launch-Locker.git` (auth verified as `craigmcgrain-spec`). Repo-local identity: `craigmcgrain-spec <craigmcgrain-spec@users.noreply.github.com>`.
- **LICENSE:** MIT.

## Review Focus

Failure modes the spec implies but no single task's happy path exercises. Each is pinned by a test in the owning task:

1. **Names containing `/`, a space, `#`, `%`, or non-ASCII characters** appear in UI URLs (`/lockers/<name>/edit`). A naive route split on `/` breaks for `a/b`, and unencoded names break percent-encoding round-trips. → Task 9.
2. **Valid JSON with the wrong shape** (`"lockers": []`, an entry that is a list, `command` missing or a number) must raise `StoreCorrupt`, not `AttributeError`, and must not be read as an empty store. → Task 1.
3. **Commands containing quotes, `$VAR`, backticks, `&&`, pipes, and newlines** must survive the JSON round-trip byte-exact and reach `sh -c` unaltered. Any re-quoting here silently changes what gets launched. → Tasks 2 and 4.
4. **Names beginning with `-`** are read by argparse as option flags and become unlaunchable. → Task 2.
5. **Hugging Face API returns 200 but the README is missing (404), empty, or CRLF-terminated** — the card must still render its metadata rather than failing whole. → Task 8.

---

### Task 1: Package skeleton and store persistence

**Files:**
- Create: `pyproject.toml`, `src/locker/__init__.py`, `src/locker/store.py`, `tests/__init__.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCHEMA_VERSION: int = 1`
  - `class StoreError(Exception)`, `class StoreCorrupt(StoreError)`, `class ReadOnlyStore(StoreError)`
  - `@dataclass(frozen=True) class Entry` with fields `name: str`, `command: str`, `hf_repo: str | None`, `created: str`, `updated: str`
  - `resolve_store_path(cli_path: str | None = None, env: Mapping[str, str] | None = None) -> Path`
  - `class Store` with `Store.open(path: Path) -> Store`, `entries(self) -> list[Entry]`, `put(self, entry: Entry) -> None`, `readonly: bool`, and internal `_save(self) -> None`
  - `utcnow() -> str` returning ISO 8601 UTC with `Z` suffix, seconds precision

- [ ] **Step 1: Create the package skeleton**

`pyproject.toml`: `[build-system]` requires `hatchling`; `[project]` name `launch-locker`, `requires-python = ">=3.11"`, no `dependencies` key; `[tool.hatch.build.targets.wheel] packages = ["src/locker"]`. Do **not** add `[project.scripts]` yet — `cli.py` does not exist until Task 3, and a broken entry point makes the installed package unusable.

- [ ] **Step 2: Write the failing tests**

In `tests/test_store.py`, class `PathTests` and class `PersistenceTests`:

- `test_path_precedence` — `resolve_store_path("/tmp/x.json", {"LOCKER_FILE": "/tmp/y.json", "XDG_CONFIG_HOME": "/tmp/z"})` is `/tmp/x.json`; without the arg it is `/tmp/y.json`; without `LOCKER_FILE` it is `/tmp/z/locker/lockers.json`; with neither env var it ends with `.config/locker/lockers.json`.
- `test_missing_file_is_empty_store` — `Store.open(tmp/"none.json").entries() == []` and no file is created.
- `test_roundtrip` — save one entry, reopen, assert all five fields survive and `created == updated`.
- `test_corrupt_json_raises` — write `"{ not json"`, assert `StoreCorrupt` and that the message contains the path; assert the file content is **unchanged** afterwards.
- `test_wrong_shape_raises` — for each of `{"version":1,"lockers":[]}`, `{"version":1,"lockers":{"a":[]}}`, `{"version":1,"lockers":{"a":{"name":"a"}}}`, `{"version":1,"lockers":{"a":{"command":42}}}` assert `StoreCorrupt`, not `AttributeError`/`TypeError`.
- `test_no_temp_files_left` — after `Store.open(p)` + a mutation, `list(p.parent.glob("*.tmp")) == []` and `p.parent` contains only `lockers.json`.
- `test_permissions` — after save, `stat.S_IMODE(file) == 0o600` and `stat.S_IMODE(dir) == 0o700`.
- `test_existing_loose_permissions_are_tightened` — pre-create the file `0644`, save via `Store`, assert `0600`.
- `test_newer_version_is_read_only` — `{"version":99,"lockers":{}}` opens, `readonly is True`, and calling `_save()` raises `ReadOnlyStore`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locker.store'`

- [ ] **Step 4: Implement `store.py`**

Write the exception classes, `Entry`, `utcnow()`, `resolve_store_path()` implementing the four-level precedence, and `Store`:

- `Store.open(path)` reads the file if it exists, parses it, validates shape, and records `readonly` when `version > SCHEMA_VERSION`. Missing file yields an empty store.
- `_save()` creates the parent directory with `mode=0o700`, writes JSON with `ensure_ascii=False` and `indent=2` to `<name>.tmp` in the same directory opened with `os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)`, flushes, `os.fsync`s, closes, `os.replace`s onto the target, then `os.chmod`s the target `0o600`. The `chmod` is what tightens a pre-existing loose file.
- Shape validation is a dedicated `_validate(raw)` that raises `StoreCorrupt` for every wrong-shape case in Step 2, so callers never see a raw `KeyError`/`AttributeError`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_store -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Commit**

Stage `pyproject.toml src/locker/__init__.py src/locker/store.py tests/__init__.py tests/test_store.py`. Intended message: `feat: store persistence with atomic writes and corrupt-file safety`

---

### Task 2: Store entry CRUD, validation, and resolution

**Files:**
- Modify: `src/locker/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: everything from Task 1.
- Produces, on `Store`:
  - `validate_name(name: str) -> str` (module-level, raises `InvalidName`)
  - `class InvalidName(StoreError)`, `class NameConflict(StoreError)`, `class NotFound(StoreError)`
  - `get(self, name: str) -> Entry`
  - `resolve(self, name: str) -> Entry` — exact match first, then case-insensitive
  - `suggestions(self, name: str) -> list[str]`
  - `add(self, name: str, command: str, hf_repo: str | None = None) -> Entry`
  - `update(self, name: str, command: str | None = None, hf_repo: str | None = None) -> Entry`
  - `remove(self, name: str) -> None`

`add` and `update` build an `Entry` (setting `created`/`updated` via `utcnow()`) and hand it to `put()` from Task 1, which owns `_save()`. Every mutator therefore persists before returning, so a caller cannot forget to.

- [ ] **Step 1: Write the failing tests**

Add these to `tests/test_store.py`:

- `test_add_and_get` — `add("qwen3.8", "llama-server -hf Qwen/Qwen3-8B")` returns an `Entry`; `get` returns it; the file on disk contains it.
- `test_command_roundtrips_byte_exact` — the Review Focus 3 case: add a command containing `"double"`, `'single'`, `$HOME`, backticks, `&&`, a pipe, and an embedded newline; reopen the store and assert `entry.command == original` exactly, with `repr()` equality.
- `test_unicode_command_and_name` — name `qwen-3.8-日本語-🚀`, command with CJK; assert byte-exact round-trip and that the file on disk contains the raw characters (not `\uXXXX` escapes).
- `test_name_uniqueness_is_case_insensitive` — `add("Qwen", ...)` then `add("qwen", ...)` raises `NameConflict`; `resolve("QWEN")` finds the existing one.
- `test_name_rejects_empty_and_whitespace` — `add("", ...)` and `add("   ", ...)` raise `InvalidName`.
- `test_name_rejects_leading_dash` — Review Focus 4: `add("-foo", ...)` raises `InvalidName`, and the name is usable after stripping: `add(" foo ", ...)` stores `foo`.
- `test_name_is_stripped` — `add("  x  ", ...)`, then `get("x")` works.
- `test_resolve_prefers_exact` — store `{"abc": ..., "ABC": ...}` cannot coexist; instead assert exact-before-case-insensitive ordering using a store seeded directly through `_save` with two keys differing in case, then `resolve("ABC")` returns the `ABC` entry.
- `test_resolve_missing_raises_notfound`
- `test_suggestions` — with `qwen3.8` stored, `suggestions("qwen38")` contains `qwen3.8`; unrelated input suggests nothing.
- `test_update_changes_command_and_updated_stamp` — `created` unchanged, `updated` changes, `command` replaced.
- `test_update_rejects_empty_command` — `update("x", command="")` raises `InvalidCommand` (a `StoreError`, so the CLI reports it cleanly instead of tracing back) and leaves the stored entry untouched.
- `test_remove` — removes; a second `remove` raises `NotFound`.
- `test_mutators_persist_immediately` — after `add`, a fresh `Store.open` on the same path sees the entry without any explicit save.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_store -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'add'`

- [ ] **Step 3: Implement the CRUD and resolution methods**

`validate_name` strips, rejects empty and leading-`-`. `add`/`update` call `validate_name`, then check `readonly`, then case-insensitive conflict, then persist. `resolve` tries an exact key lookup before a case-insensitive scan. `suggestions` uses `difflib.get_close_matches(name, keys, n=3, cutoff=0.6)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_store -v`
Expected: PASS (23 tests)

- [ ] **Step 5: Commit**

Stage `src/locker/store.py tests/test_store.py`. Intended message: `feat: store CRUD with name validation and case-insensitive resolution`

---

### Task 3: CLI skeleton, global flags, and `list`

**Files:**
- Create: `src/locker/cli.py`, `tests/test_cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `Store`, `resolve_store_path`, `StoreError` from `store.py`.
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `main(argv: list[str] | None = None) -> int`
  - `format_table(entries: list[Entry]) -> str`
  - `entries_to_json(entries: list[Entry]) -> str`

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, calling `main([...])` with `LOCKER_FILE` pointed at a temp file via `unittest.mock.patch.dict(os.environ, ...)`, capturing stdout/stderr with `contextlib.redirect_stdout`:

- `test_list_empty_store` — prints a friendly line, returns `0`.
- `test_list_shows_name_and_command` — after seeding two entries, stdout contains both names and both commands, one per line, names column-aligned.
- `test_list_json` — `main(["--file", p, "list", "--json"])` returns `0` and `json.loads(stdout)` is a list of objects with keys `name`, `command`, `hf_repo`.
- `test_list_truncates_long_commands` — a 300-character command is truncated in the table with `…`, and **not** truncated in `--json` output.
- `test_file_flag_overrides_env` — with `LOCKER_FILE` pointing elsewhere, `--file` wins.
- `test_no_args_lists_lockers` — `main([])` returns `0` and prints the locker list.
- `test_unknown_subcommand_is_usage_error` — `main(["bogus"])` returns `2`.
- `test_corrupt_store_exits_one_with_message` — `StoreCorrupt` is reported on stderr with the file path, return `1`, no traceback.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locker.cli'`

- [ ] **Step 3: Implement the parser and `list`**

Add `dest="file"` global `--file` on the main parser so it may appear before the subcommand. Add subparsers `launch`, `list`, `add`, `edit`, `rm`, `ui`; Tasks 4, 5, and 9 fill in all but `list`, which uses `--json`. `main` wraps each command in `try/except StoreError`, printing `locker: <message>` to stderr and returning `1`. `format_table` computes column widths from the data and truncates the command column at 60 characters with a trailing `…`.

Add to `pyproject.toml`:

```toml
[project.scripts]
locker = "locker.cli:main"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Verify the console script end to end**

Run: `uv run locker --file /tmp/ll-smoke.json list`
Expected: prints the empty-store line, exit code 0. Then `rm -f /tmp/ll-smoke.json`.

- [ ] **Step 6: Commit**

Stage `src/locker/cli.py tests/test_cli.py pyproject.toml`. Intended message: `feat: CLI skeleton with list, --json, and global --file`

---

### Task 4: `launch`

**Files:**
- Modify: `src/locker/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store.resolve`, `Store.suggestions`, `NotFound` from Task 2.
- Produces: `cmd_launch(store: Store, name: str, exec_fn: Callable[[str, list[str]], NoReturn] = os.execvp) -> int`

`exec_fn` is injectable so tests never replace a process. Production passes `os.execvp`.

- [ ] **Step 1: Write the failing tests**

- `test_launch_execs_sh_c_with_exact_command` — Review Focus 3: seed a command containing quotes, `$HOME`, and a newline; call `cmd_launch(store, "x", exec_fn=stub)`; assert the stub was called exactly once with `("/bin/sh", ["sh", "-c", command])` and `command` is byte-identical to what was stored.
- `test_launch_banner_goes_to_stderr` — capture both streams; `→ running:` appears in stderr and stdout is empty.
- `test_launch_missing_name_exits_one_with_suggestions` — `main(["launch","qwen38"])` with `qwen3.8` stored returns `1`, stderr contains `qwen3.8`, and `exec_fn` was never called.
- `test_launch_resolves_case_insensitively` — `launch QWEN3.8` execs the entry stored as `qwen3.8`.
- `test_launch_does_not_import_or_call_the_network` — assert `locker.hf` is absent from `sys.modules` after `import locker.cli` and a `launch`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: FAIL — `AttributeError: module 'locker.cli' has no attribute 'cmd_launch'`

- [ ] **Step 3: Implement `cmd_launch`**

Resolve the name; on `NotFound` print `locker: no locker named '<name>'` plus up to three `Did you mean:` suggestions to stderr and return `1`. Print `→ running: <command>` to **stderr**. Then `exec_fn("/bin/sh", ["sh", "-c", command])`. Annotate the return type as `NoReturn` since `execvp` never returns; the final `return 0` after the call exists only to satisfy the type when a stub is injected.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Verify by hand, including the signal path**

Run: `uv run locker --file /tmp/ll-smoke.json add demo -c 'echo launched; sleep 30'` then
`uv run locker --file /tmp/ll-smoke.json launch demo`, press Ctrl-C, and confirm the shell reports the interrupt and returns to the prompt immediately (no lingering wrapper process). Then `rm -f /tmp/ll-smoke.json`.

- [ ] **Step 6: Commit**

Stage `src/locker/cli.py tests/test_cli.py`. Intended message: `feat: launch via execvp through sh -c`

---

### Task 5: `add`, `edit`, and `rm`

**Files:**
- Modify: `src/locker/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store.add`, `Store.update`, `Store.remove`, `InvalidName`, `NameConflict`, `NotFound`.
- Produces: `cmd_add`, `cmd_edit`, `cmd_rm`, and `read_command_from_stdin() -> str`

- [ ] **Step 1: Write the failing tests**

- `test_add_with_c_flag` — returns `0`, entry persisted, stdout confirms the name.
- `test_add_reads_stdin_when_c_omitted` — patch `sys.stdin` with a `StringIO` command containing a trailing newline; assert the stored command has trailing whitespace stripped but internal newlines preserved.
- `test_add_rejects_empty_stdin` — returns `1`, nothing written.
- `test_add_duplicate_name_exits_one` — second `add` of the same name returns `1`, stderr names the conflict, store unchanged.
- `test_add_leading_dash_name_exits_one` — `add -- -foo -c 'x'` returns `1` with the `InvalidName` message.
- `test_edit_replaces_command` — `edit x -c 'new'` returns `0`; stored command is `new`; `created` unchanged, `updated` differs.
- `test_edit_hf_flag_pins_repo` — `edit x --hf Qwen/Qwen3-8B` sets `hf_repo`.
- `test_edit_uses_editor_when_no_flags` — patch `EDITOR` to a script that appends ` # edited`; assert the stored command reflects the edit and that the temp file is deleted afterwards.
- `test_edit_unchanged_editor_is_noop` — editor that exits without writing leaves `updated` unchanged.
- `test_edit_missing_editor_falls_back_to_vi` — with `EDITOR` unset, assert the subprocess argv begins with `vi`.
- `test_edit_missing_name_exits_one`
- `test_rm_requires_confirmation_when_not_yes` — patch `stdin` with `n`; entry survives, returns `0`.
- `test_rm_yes_deletes` — `rm x --yes` returns `0`, `get` raises `NotFound`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: FAIL — `AttributeError: module 'locker.cli' has no attribute 'cmd_add'`

- [ ] **Step 3: Implement the three commands**

`cmd_add` uses `-c` when given, otherwise reads stdin and strips outer whitespace. `cmd_edit` with no `-c` and no `--hf` writes the command to a `NamedTemporaryFile` in `tempfile.gettempdir()`, runs `os.environ.get("EDITOR", "vi")` on it via `subprocess.call`, reads it back, and deletes it in a `finally`. `subprocess` is used here for the editor only — it never executes a stored command, so the Global Constraint on execution still holds. `cmd_rm` prompts `Remove '<name>'? [y/N]` unless `--yes`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: PASS (26 tests)

- [ ] **Step 5: Run the whole suite**

Run: `uv run python -m unittest discover -s tests -v`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

Stage `src/locker/cli.py tests/test_cli.py`. Intended message: `feat: add, edit, and rm subcommands`

---

### Task 6: Markdown renderer

**Files:**
- Create: `src/locker/md.py`, `tests/test_md.py`

**Interfaces:**
- Consumes: nothing (pure module, no imports from the rest of the package).
- Produces:
  - `MAX_README_CHARS: int = 200_000`
  - `render(text: str) -> str` — returns HTML-safe markup, or `""` for empty input

- [ ] **Step 1: Write the failing tests**

In `tests/test_md.py`, class `SecurityTests` first, because these are the point of the module:

- `test_script_tag_is_escaped` — `render("<script>alert(1)</script>")` contains `&lt;script&gt;` and does not contain `<script`.
- `test_img_onerror_is_escaped` — `render('<img src=x onerror=alert(1)>')` contains no `<img`.
- `test_event_handler_attribute_is_escaped` — `render('<a href="#" onclick="x()">y</a>')` contains no `onclick=`.
- `test_javascript_href_is_not_a_link` — `render("[x](javascript:alert(1))")` output contains no `href="javascript:`.
- `test_data_href_is_not_a_link` — same for `data:text/html;base64,...`.
- `test_remote_image_renders_as_link` — `render("![alt](https://example.com/a.png)")` contains no `<img` and does contain an `<a href="https://example.com/a.png"`.
- `test_html_in_fenced_code_is_escaped` — a fence containing `<script>` yields escaped text inside `<pre><code>`.
- `test_safe_link_is_rendered` — `[Qwen](https://huggingface.co/Qwen)` yields an `<a href="https://huggingface.co/Qwen">`.

Class `RenderTests`:

- `test_headings` — `#` through `######` produce `h1`..`h6`.
- `test_bold_italic_inline_code`
- `test_bullet_and_numbered_lists`
- `test_blockquote_and_hr`
- `test_unclosed_fence_does_not_swallow_the_page` — an unterminated ``` fence still returns well-formed output with balanced `<pre>` tags.
- `test_unsupported_construct_is_literal_text` — a table renders as escaped text, producing no `<table>`.
- `test_unicode_passes_through_unmangled` — CJK and emoji survive; no mojibake, no double-escaping of `&` (`&amp;` stays `&amp;`, not `&amp;amp;`).
- `test_truncates_at_max_chars` — input of `MAX_README_CHARS + 5000` returns output derived from at most `MAX_README_CHARS` of input.
- `test_empty_input` — `render("") == ""`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_md -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locker.md'`

- [ ] **Step 3: Implement `md.py`**

Order of operations is the security property: truncate to `MAX_README_CHARS`, then `html.escape(text, quote=True)` over the whole document, then apply line-based transforms to the escaped text, then inline transforms within non-code lines. Links are matched as `\[([^\]]+)\]\(([^)]+)\)` and emitted only when the target starts with `http://` or `https://`; otherwise the original text is left as-is. A `render` that encounters an unsupported block emits the escaped line unchanged, so unknown syntax is always literally visible and never markup.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_md -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

Stage `src/locker/md.py tests/test_md.py`. Intended message: `feat: escape-first minimal markdown renderer for model cards`

---

### Task 7: Hugging Face repo detection

**Files:**
- Create: `src/locker/hf.py`, `tests/test_hf.py`

**Interfaces:**
- Consumes: `MAX_README_CHARS` from `md.py`.
- Produces:
  - `REPO_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")`
  - `is_repo_id(candidate: str) -> bool`
  - `detect_repos(command: str) -> list[str]` — ordered, de-duplicated candidates

This task is pure and synchronous: no network, no cache.

- [ ] **Step 1: Write the failing tests**

- `test_detects_hf_flag` — `llama-server -hf Qwen/Qwen3-8B` → `["Qwen/Qwen3-8B"]`.
- `test_detects_hf_repo_and_repo_id_flags` — `--hf-repo`, `--repo-id`, `--hf`.
- `test_detects_model_flag_value` — `--model unsloth/Qwen3-8B-GGUF` → that repo.
- `test_model_flag_with_local_path_is_skipped` — `llama-server --model ./models/qwen.gguf` → `[]`.
- `test_model_flag_with_absolute_path_is_skipped` — `/models/x.gguf` → `[]`.
- `test_gguf_extension_is_skipped` — `-hf Qwen/Qwen3-8B-GGUF/./x.gguf` and any candidate ending in `.gguf`, `.safetensors`, `.bin`, `.pt` are rejected.
- `test_url_is_skipped` — `https://huggingface.co/Qwen/Qwen3-8B` yields no candidate that contains `://`.
- `test_bare_token_is_detected` — `some-tool Qwen/Qwen3-8B --port 8080` → `["Qwen/Qwen3-8B"]`.
- `test_home_relative_path_is_skipped` — `~/models/qwen` → `[]`.
- `test_three_segment_path_is_skipped` — `a/b/c` → `[]`.
- `test_flags_come_before_bare_tokens` — a command with both `--model X/Y` and a bare `Z/W` returns `["X/Y", "Z/W"]` in that order.
- `test_duplicates_are_collapsed` — the same repo appearing twice returns one candidate.
- `test_no_candidates_returns_empty_list` — `llama-server --port 8080` → `[]`.
- `test_is_repo_id_rejects_bad_shapes` — `""`, `"a"`, `"a/"`, `"/b"`, `"a/b/c"`, `"a b/c"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_hf -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locker.hf'`

- [ ] **Step 3: Implement detection**

Tokenise with `shlex.split(command, posix=True)` inside a `try`, falling back to `command.split()` on `ValueError` (unbalanced quotes are common in half-typed commands). Walk the tokens for the explicit flags in the spec's order, then a second pass for bare `owner/name` tokens. Reject anything failing `is_repo_id`, containing `://`, starting with `/`, `~`, or `.`, or ending in a model-file extension.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_hf -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

Stage `src/locker/hf.py tests/test_hf.py`. Intended message: `feat: detect Hugging Face repo ids in launch commands`

---

### Task 8: Model card fetch, cache, and fallback

**Files:**
- Modify: `src/locker/hf.py`, `tests/test_hf.py`

**Interfaces:**
- Consumes: `detect_repos` from Task 7.
- Produces:
  - `class CardError(Exception)`, `class RepoNotFound(CardError)`, `class CardUnavailable(CardError)`, `class NoCandidateDetected(CardError)`
  - `@dataclass(frozen=True) class Card` with `repo: str`, `api: dict`, `readme: str`, `fetched_at: float`
  - `cache_dir_for(env: Mapping[str, str] | None = None) -> Path`
  - `class CardClient` with `__init__(self, cache_dir: Path, *, opener=None, ttl_seconds: int = 86400, refresh: bool = False, now: Callable[[], float] = time.time)`
  - `CardClient.card_for(self, repo: str) -> tuple[Card, bool]` — returns `(card, from_cache)`, raises `RepoNotFound` on HTTP 404 and `CardUnavailable` on any other failure with no usable cache
  - `lookup(command: str, pinned: str | None, client: CardClient) -> tuple[Card, bool]` — pin wins; otherwise candidates in order, advancing on `RepoNotFound`; raises `NoCandidateDetected` when detection is empty

`opener` defaults to `urllib.request.urlopen` and is the seam that keeps tests off the network.

- [ ] **Step 1: Write the failing tests**

Use a `FakeOpener` returning canned bytes for the API and README URLs and recording every URL requested.

- `test_fetches_api_and_readme_and_caches` — one call yields a `Card` with `api["downloads"]` preserved; a second `CardClient` on the same cache dir performs **zero** HTTP requests and returns `from_cache is True`.
- `test_cache_expiry` — with `now` advanced past `ttl_seconds`, the fetch happens again.
- `test_refresh_bypasses_fresh_cache` — `refresh=True` refetches despite a fresh cache.
- `test_cache_is_written_atomically` — no `.tmp` files remain in the cache directory.
- `test_repo_404_raises_repo_not_found` — distinct from `CardUnavailable`.
- `test_timeout_raises_card_unavailable`
- `test_offline_with_stale_cache_returns_stale_card` — opener raises, a stale cache exists; returns the cached card with `from_cache is True` rather than raising.
- `test_offline_without_cache_raises_card_unavailable`
- `test_missing_readme_still_returns_card` — Review Focus 5: API 200, README 404; `card.readme == ""` and the card is still returned.
- `test_empty_readme_still_returns_card` — README 200 with `b""`.
- `test_crlf_readme_is_normalised` — `b"# Title\r\n\r\nBody\r\n"` yields no `\r`.
- `test_readme_is_truncated_before_caching` — a README of `MAX_README_CHARS + 5000` is stored truncated on disk; assert the cached file size is bounded.
- `test_pinned_repo_skips_detection` — `pinned="X/Y"` with a command containing `A/B`; only `X/Y` is requested.
- `test_candidates_advance_on_404` — candidates `["A/B","C/D"]`, `A/B` 404s, `C/D` succeeds; the returned card's `repo == "C/D"`.
- `test_all_candidates_404_raises_repo_not_found` — message names the first candidate.
- `test_no_candidate_raises_no_candidate_detected`
- `test_user_agent_is_set` — the recorded request headers contain `Launch-Locker`.
- `test_readme_read_failure_does_not_lose_metadata` — README raises a non-404 error; the card still returns with `readme == ""`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_hf -v`
Expected: FAIL — `AttributeError: module 'locker.hf' has no attribute 'CardClient'`

- [ ] **Step 3: Implement the client**

Metadata and README live together in `<cache_dir>/<owner>__<name>/meta.json` and `README.md`, written with the same temp-file-then-`os.replace` pattern as the store. The README is normalised (`\r\n` → `\n`) and truncated to `MAX_README_CHARS` **before** caching. Staleness is `now() - fetched_at > ttl_seconds`. HTTP 404 maps to `RepoNotFound`; `socket.timeout`, `URLError`, `HTTPError` with any other code, and malformed JSON all map to `CardUnavailable`. Both requests use `timeout=5` and a `User-Agent` of `Launch-Locker/<version>`. A README failure after a successful API call logs nothing and yields `readme=""`, because partial data beats no card.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_hf -v`
Expected: PASS (32 tests)

- [ ] **Step 5: Verify against the real API once, by hand**

Run: `uv run python -c "from pathlib import Path; from locker.hf import *; c=CardClient(Path('/tmp/ll-cache')); card,_=c.card_for('Qwen/Qwen3-8B'); print(card.repo, card.api['downloads'], len(card.readme))"`
Expected: prints the repo, a download count, and a non-zero README length. Then `rm -rf /tmp/ll-cache`.

- [ ] **Step 6: Commit**

Stage `src/locker/hf.py tests/test_hf.py`. Intended message: `feat: Hugging Face model card fetch with disk cache and offline fallback`

---

### Task 9: HTTP server, routes, and local security

**Files:**
- Create: `src/locker/server.py`, `src/locker/static/index.html`, `tests/test_server.py`
- Modify: `src/locker/cli.py`

**Interfaces:**
- Consumes: `Store`, `resolve_store_path`, `StoreError`, `Entry`.
- Produces:
  - `ALLOWED_HOSTS` check helper `host_is_loopback(host_header: str, port: int) -> bool`
  - `make_server(store_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer`
  - `serve(store_path: Path, *, port: int, open_browser: bool, refresh: bool = False) -> int`
  - `render_page(entries: list[Entry], *, selected: str | None = None) -> str`
  - `cmd_ui(args) -> int` in `cli.py`

The HTML lives in `src/locker/static/index.html` and is loaded via `importlib.resources`, so it ships inside the wheel.

- [ ] **Step 1: Write the failing tests**

Start the server in a daemon thread on **port 0** (ephemeral) with a temp store, and use `urllib.request` against the bound address.

- `test_get_index_renders_lockers` — 200, body contains the seeded names and commands.
- `test_empty_store_renders_empty_state` — 200, body contains the empty-state text and no table.
- `test_post_creates_locker` — `POST /lockers` with `name`/`command`; response is 303 to `/`; the entry exists in the store.
- `test_post_duplicate_returns_409_and_does_not_write`
- `test_get_edit_form` — 200, contains the current command.
- `test_post_update_changes_entry` — 303, stored command updated.
- `test_post_delete_removes_entry` — 303, entry gone.
- `test_unknown_route_is_404`
- `test_wrong_method_is_405` — `GET /lockers/a` where only POST is registered.
- `test_malformed_form_data_is_400` — `POST /lockers` with no `name`.
- `test_non_loopback_host_is_403` — send `Host: evil.example.com:PORT`; assert 403 and that no write occurred.
- `test_cross_origin_post_is_403` — `Origin: http://evil.example.com`; assert 403 and no write.
- **Review Focus 1:** `test_names_with_special_characters_round_trip` — for each of `a/b`, `qwen 3.8`, `a#b`, `a%b`, `qwen-日本語` create via POST, then GET the edit page and POST an update, then delete; assert each returns 200/303 and never 404, and that the store holds the exact original name. This is the test that fails if routes split naively on `/`.
- `test_names_are_html_escaped_in_the_page` — a locker named `<script>x</script>` is stored but rendered escaped, with no raw `<script>x</script>` in the body.
- `test_commands_are_html_escaped_in_the_page` — a command containing `<b>` renders escaped.
- `test_port_fallback_when_busy` — occupy 8765, start the server, assert it binds a higher port and reports it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_server -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'locker.server'`

- [ ] **Step 3: Implement the server**

`ThreadingHTTPServer` subclassed to hold `store_path`, `refresh`, and the bound port. A `BaseHTTPRequestHandler` subclass routes with `re.fullmatch` against `urllib.parse.unquote`d paths, so `/lockers/(?P<name>.+)/edit` is matched with a greedy group **after** unquoting — never by splitting on `/`.

Order of checks in `do_GET`/`do_POST`, before any store access:

1. `host_is_loopback(self.headers.get("Host", ""), self.server.server_address[1])`, else 403.
2. On POST, if `Origin` or `Referer` is present, its netloc must equal the loopback host, else 403.
3. Route match, else 404. Registered path with the wrong method, 405.
4. Parse the form with `urllib.parse.parse_qs`; a missing or empty required field is 400.

All interpolated values pass through `html.escape`. `serve` binds the configured port, retrying the next port up to 20 times on `OSError`, prints the URL, opens the browser unless `--no-open`, and serves until interrupted. Wire `cmd_ui` into `cli.py` with `--port`, `--no-open`, `--refresh`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_server -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Verify by hand in a browser**

Run: `uv run locker --file /tmp/ll-smoke.json ui`. Add a locker via the form, confirm it appears, edit it, delete it, and confirm `uv run locker --file /tmp/ll-smoke.json list` agrees. Then `rm -f /tmp/ll-smoke.json`.

- [ ] **Step 6: Commit**

Stage `src/locker/server.py src/locker/static/index.html tests/test_server.py src/locker/cli.py`. Intended message: `feat: local web UI with loopback-only server and CRUD routes`

---

### Task 10: Model card panel and UI behaviour

**Files:**
- Modify: `src/locker/server.py`, `src/locker/static/index.html`, `tests/test_server.py`

**Interfaces:**
- Consumes: `lookup`, `CardClient`, `CardError` from Task 8; `render` from Task 6.
- Produces: route `GET /api/card?repo=<owner/name>` returning
  `{"repo": str, "author": str, "pipeline_tag": str|None, "downloads": int, "likes": int, "license": str|None, "last_modified": str|None, "tags": list[str], "cached": bool, "readme_html": str}`
  on success (200) and `{"error": "<code>", "message": str, "repo": str|None}` on failure, with `error` in `{"repo_not_found","unavailable","no_candidate"}`. Failures return 200 with an error body, so the panel renders a message instead of a broken fetch.

- [ ] **Step 1: Write the failing tests**

- `test_api_card_returns_metadata` — with a stubbed client injected, assert the JSON keys and that `readme_html` contains rendered HTML.
- `test_api_card_404_repo_returns_error_body` — `error == "repo_not_found"`, HTTP status 200.
- `test_api_card_offline_returns_unavailable` — `error == "unavailable"`.
- `test_api_card_no_candidate` — `error == "no_candidate"`.
- `test_api_card_rejects_a_malicious_repo_parameter` — `?repo=../../etc/passwd` and `?repo=javascript:alert(1)` are rejected with `no_candidate`/400 and never fetched.
- `test_api_card_refresh_parameter_bypasses_cache` — `?refresh=1` forces a refetch even with a fresh cache, per spec 6.2; the stubbed client records that it was called with `refresh=True`.
- `test_index_includes_card_panel_and_script` — the page contains the panel container and the script that fetches `/api/card`.
- `test_index_includes_copy_button` — a copy control exists per row.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_server -v`
Expected: FAIL — 404 on `/api/card`

- [ ] **Step 3: Implement the endpoint and the panel**

Validate the `repo` parameter with `is_repo_id` from `hf.py` **before** constructing a request, so a hostile value never reaches the opener. Build the JSON per the interface above; derive `license` from the `license:` tag and `author` from the part before `/`. Call `render(card.readme)` for `readme_html`. In `index.html`, add the panel, a `fetch('/api/card?repo=...')` that fills it after paint, the copy-to-clipboard button using `navigator.clipboard.writeText` with a `document.execCommand('copy')` fallback, and a paste-time hint on the command field that shows the detected repo.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_server -v`
Expected: PASS (24 tests)

- [ ] **Step 5: Verify by hand, online and offline**

Run: `uv run locker ui`, add a locker whose command contains `-hf Qwen/Qwen3-8B`, and confirm the card renders with metadata and the README. Then disable networking and reload: the card must fall back to the cached copy or the "unavailable" message, and the page must still be fully usable.

- [ ] **Step 6: Commit**

Stage `src/locker/server.py src/locker/static/index.html tests/test_server.py`. Intended message: `feat: model card panel with lazy loading and offline fallback`

---

### Task 11: Documentation, license, and end-to-end verification

**Files:**
- Create: `README.md`, `LICENSE`
- Modify: none

**Interfaces:**
- Consumes: the finished CLI and UI.
- Produces: nothing importable.

- [ ] **Step 1: Write `LICENSE`**

MIT, copyright holder to be confirmed with the user before the first commit.

- [ ] **Step 2: Write `README.md`**

Sections: what it is (one paragraph, with a `locker launch qwen3.8` example); install (`uv tool install .` and `uvx --from . locker`, plus pipx); a CLI reference matching the spec's table; the UI (`locker ui`, loopback-only, port behaviour); the file format with a JSON example and the `LOCKER_FILE` / `--file` / XDG precedence; a **Security** section stating that the lockers file is a list of shell commands executed on your behalf, that it is kept `0600`, and that commands may embed credentials such as `HF_TOKEN`; and a short *Not included* list mirroring the spec's Deferred section.

- [ ] **Step 3: Run the full suite from a clean environment**

Run: `rm -rf .venv && uv run python -m unittest discover -s tests -v`
Expected: PASS, all tasks' tests, no skips, no network access.

- [ ] **Step 4: Verify the packaged install path**

Run: `uv build` then `uv tool install --force .` and `locker list`.
Expected: builds a wheel, installs, and `locker list` runs the empty-store path with exit code 0. Confirm the static HTML is inside the wheel with `unzip -l dist/*.whl | grep index.html`.

- [ ] **Step 5: Walk the five Review Focus cases by hand**

Confirm each behaves as its test asserts, against the installed `locker`: a locker named `a/b` survives create → edit → delete in the UI; a store containing `{"version":1,"lockers":[]}` is refused rather than emptied; `locker launch` of a command containing `$HOME` and a newline runs the same string; `locker add -- -x -c y` is rejected with the `InvalidName` message; and a locker pinned to a nonexistent repo shows `repo_not_found` rather than a stack trace.

- [ ] **Step 6: Report and await authorization to commit and push**

Before any commit, settle the git author identity with the user. The machine-wide config is
`GitHub Copilot <copilot@github.com>`, and spec §10 calls for a repository-local override;
neither has been agreed yet.

Run: `git status --short` and `git remote -v`. Report the file list and the full command list for the reviewer. Wire the remote only when authorised:

```bash
git remote add origin git@github.com:craigmcgrain-spec/Launch-Locker.git
git push -u origin master
```

Expected: `origin` absent until authorised; all task commits present locally on `master`, and nothing pushed. The `.superpowers/` ledger directory is ignored and never committed.
