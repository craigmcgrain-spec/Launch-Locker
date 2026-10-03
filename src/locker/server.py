"""The local web UI: a loopback-only HTTP server over the same store.

The browser never executes anything. It lists lockers, adds and edits them, and
shows a model card fetched by this process. Commands leave the page only through
the copy button or through `locker launch` in your terminal.

Two guards protect the store from the web at large, because any page you visit
can POST to http://127.0.0.1:<port>/:

1. the ``Host`` header must be a loopback name on the bound port (also blunts
   DNS rebinding), and
2. a ``POST`` carrying ``Origin`` or ``Referer`` must match this server.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as _resource_files
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .hf import (
    Card,
    CardClient,
    CardUnavailable,
    NoCandidateDetected,
    RepoNotFound,
    cache_dir_for,
    is_repo_id,
    lookup,
)
from .md import render
from .store import Entry, NameConflict, NotFound, Store, StoreError

_HOSTS = ("127.0.0.1", "localhost")
_SAFE_STATIC = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

# Ordered: `edit` and `delete` must be tried before the greedy `update`.
_ROUTES = (
    ("index", re.compile(r"/"), {"GET"}),
    ("static", re.compile(r"/static/(?P<file>[^/]+)"), {"GET"}),
    ("api_card", re.compile(r"/api/card"), {"GET"}),
    ("create", re.compile(r"/lockers"), {"POST"}),
    # Names are matched in their percent-encoded form, so a '/' inside a name can
    # never be mistaken for a route separator and an internal newline stays %0A.
    # Each handler decodes the name itself. Do NOT unquote the path before
    # matching: that turns /lockers/x%2Fdelete into the delete route for "x".
    ("edit", re.compile(r"/lockers/(?P<name>[^/]+)/edit"), {"GET"}),
    ("delete", re.compile(r"/lockers/(?P<name>[^/]+)/delete"), {"POST"}),
    ("update", re.compile(r"/lockers/(?P<name>[^/]+)"), {"POST"}),
)

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

_TEMPLATES: dict[str, str] = {}


def host_is_loopback(host_header: str, port: int) -> bool:
    """True when ``Host`` names this machine's loopback interface and our port."""
    if not host_header:
        return False

    host = host_header.strip()
    if host.startswith("["):  # IPv6 literal
        name, _, rest = host.partition("]")
        if rest.startswith(":") and rest[1:] != str(port):
            return False
        return name == "[::1]"

    name, separator, header_port = host.partition(":")
    if separator and header_port and header_port != str(port):
        return False
    return name in _HOSTS


def _template(name: str) -> str:
    if name not in _TEMPLATES:
        _TEMPLATES[name] = (
            _resource_files("locker") / "static" / name
        ).read_text(encoding="utf-8")
    return _TEMPLATES[name]


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute ``{{key}}`` in a single pass over the template only.

    A single pass matters. Substituting sequentially re-scans text that was
    already inserted, so a command containing a literal ``{{hf_repo}}`` would be
    rewritten — the store's "stored verbatim" invariant would break from the UI.
    `re.sub` never rescans its own replacements.
    """
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def _locker_row(entry: Entry) -> str:
    quoted = quote(entry.name, safe="")
    return (
        f'<tr class="locker" data-name="{escape(entry.name, quote=True)}" '
        f'data-hf="{escape(entry.hf_repo or "", quote=True)}">'
        f'<td class="name"><a href="/?selected={quoted}">{escape(entry.name)}</a></td>'
        f'<td class="command"><code>{escape(entry.command)}</code></td>'
        f'<td class="repo">{escape(entry.hf_repo or "")}</td>'
        f'<td class="actions">'
        f'<button type="button" class="copy" '
        f'data-command="{escape(entry.command, quote=True)}">copy</button> '
        f'<a href="/lockers/{quoted}/edit">edit</a> '
        f'<form method="post" action="/lockers/{quoted}/delete" class="inline">'
        f'<button type="submit">delete</button></form>'
        f"</td></tr>"
    )


def render_page(entries: list[Entry], *, selected: str | None = None) -> str:
    """Render the locker list. Everything interpolated is HTML-escaped."""
    if selected is None and entries:
        selected = entries[0].name

    if entries:
        rows = "".join(_locker_row(entry) for entry in entries)
        lockers = (
            "<table><thead><tr><th>Name</th><th>Command</th><th>Repo</th>"
            f"<th></th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        lockers = '<p class="empty">No lockers yet. Add one below.</p>'

    return _fill(
        _template("index.html"),
        {"lockers": lockers, "selected": escape(selected or "", quote=True)},
    )


def render_edit_page(entry: Entry) -> str:
    return _fill(
        _template("edit.html"),
        {
            "name": escape(entry.name),
            "quoted": quote(entry.name, safe=""),
            "command": escape(entry.command),
            "hf_repo": escape(entry.hf_repo or "", quote=True),
        },
    )


def _summarize(card: Card, from_cache: bool) -> dict:
    """Shape a Card for the panel, deriving what the card view needs."""
    api = card.api
    tags = [tag for tag in (api.get("tags") or []) if isinstance(tag, str)]
    license_name = next(
        (tag.split(":", 1)[1] for tag in tags if tag.startswith("license:")), None
    )
    downloads = api.get("downloads")
    likes = api.get("likes")

    return {
        "repo": card.repo,
        "author": card.repo.split("/", 1)[0],
        "pipeline_tag": api.get("pipeline_tag"),
        "downloads": downloads if isinstance(downloads, int) else 0,
        "likes": likes if isinstance(likes, int) else 0,
        "license": license_name,
        "last_modified": api.get("lastModified"),
        "tags": tags[:20],
        "cached": from_cache,
        "readme_html": render(card.readme),
    }


class LockerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        *,
        store_path: Path,
        refresh: bool = False,
        card_client=None,
    ):
        super().__init__(address, handler)
        self.store_path = Path(store_path)
        self.refresh = refresh
        # None means "build a real CardClient per request"; tests inject a
        # factory so no test ever reaches the network.
        self.card_client = card_client


class LockerHandler(BaseHTTPRequestHandler):
    server_version = "LaunchLocker/0.1"

    def log_message(self, *args) -> None:
        """Silence per-request logging; it would pollute the CLI's stderr."""

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    # -- dispatch ---------------------------------------------------------

    def _handle(self, method: str) -> None:
        port = self.server.server_address[1]

        if not host_is_loopback(self.headers.get("Host", ""), port):
            self._respond(403, "Requests must be addressed to localhost.")
            return

        if method == "POST" and not self._origin_ok(port):
            self._respond(403, "Cross-origin requests are refused.")
            return

        path = urlparse(self.path).path  # keep percent-encoding; see _ROUTES
        for action, pattern, methods in _ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            if method not in methods:
                self._respond(405, "Method not allowed.")
                return
            try:
                getattr(self, f"_do_{action}")(match)
            except StoreError as exc:
                self._respond(500, str(exc))
            return

        self._respond(404, "Not found.")

    def _origin_ok(self, port: int) -> bool:
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            # A plain form post from our own page may omit both.
            return True
        parsed = urlparse(origin)
        return host_is_loopback(parsed.netloc or parsed.path, port)

    # -- routes -----------------------------------------------------------

    def _do_index(self, match) -> None:
        query = parse_qs(urlparse(self.path).query)
        selected = (query.get("selected") or [""])[-1] or None
        entries = Store.open(self.server.store_path).entries()
        self._send_html(200, render_page(entries, selected=selected))

    def _do_static(self, match) -> None:
        name = unquote(match.group("file"))
        if not _SAFE_STATIC.match(name):
            self._respond(404, "Not found.")
            return

        resource = _resource_files("locker") / "static" / name
        if not resource.is_file():
            self._respond(404, "Not found.")
            return

        body = resource.read_bytes()
        content_type = _CONTENT_TYPES.get(
            Path(name).suffix, "application/octet-stream"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _do_create(self, match) -> None:
        fields = self._read_form()
        if fields is None:
            return

        name = fields.get("name", "").strip()
        command = fields.get("command", "").strip()
        if not name or not command:
            self._respond(400, "A locker needs a name and a command.")
            return

        hf_repo = fields.get("hf_repo", "").strip() or None
        try:
            Store.open(self.server.store_path).add(name, command, hf_repo=hf_repo)
        except NameConflict as exc:
            self._respond(409, str(exc))
            return
        except StoreError as exc:
            self._respond(400, str(exc))
            return

        self._redirect("/")

    def _do_edit(self, match) -> None:
        name = unquote(match.group("name"))
        try:
            entry = Store.open(self.server.store_path).resolve(name)
        except StoreError:
            self._respond(404, f"No locker named {name!r}.")
            return

        self._send_html(200, render_edit_page(entry))

    def _do_update(self, match) -> None:
        fields = self._read_form()
        if fields is None:
            return

        command = fields.get("command", "").strip()
        if not command:
            self._respond(400, "A locker needs a non-empty command.")
            return

        hf_repo = fields.get("hf_repo", "").strip() or None
        name = unquote(match.group("name"))
        try:
            store = Store.open(self.server.store_path)
            entry = store.resolve(name)
            store.update(entry.name, command=command, hf_repo=hf_repo)
        except NotFound as exc:
            self._respond(404, str(exc))
            return
        except StoreError as exc:
            self._respond(400, str(exc))
            return

        self._redirect("/")

    def _do_delete(self, match) -> None:
        name = unquote(match.group("name"))
        try:
            store = Store.open(self.server.store_path)
            store.remove(store.resolve(name).name)
        except NotFound as exc:
            self._respond(404, str(exc))
            return

        self._redirect("/")

    def _do_api_card(self, match) -> None:
        query = parse_qs(urlparse(self.path).query)
        repo = (query.get("repo") or [""])[-1].strip()
        command = (query.get("command") or [""])[-1]
        refresh = bool(query.get("refresh")) or self.server.refresh

        if repo and not is_repo_id(repo):
            # Validated before any client is built, so a hostile value can never
            # reach the network layer.
            self._send_json(
                200,
                {
                    "error": "no_candidate",
                    "message": "That is not a Hugging Face repo id.",
                    "repo": None,
                },
            )
            return

        client = self._card_client(refresh)

        try:
            card, from_cache = lookup(command, repo or None, client)
        except NoCandidateDetected:
            self._send_json(
                200,
                {
                    "error": "no_candidate",
                    "message": "No Hugging Face repo detected in this command.",
                    "repo": None,
                },
            )
        except RepoNotFound as exc:
            self._send_json(
                200,
                {"error": "repo_not_found", "message": str(exc), "repo": repo or None},
            )
        except CardUnavailable as exc:
            self._send_json(
                200,
                {
                    "error": "unavailable",
                    "message": f"Model card unavailable ({exc}).",
                    "repo": repo or None,
                },
            )
        else:
            self._send_json(200, _summarize(card, from_cache))

    def _card_client(self, refresh: bool) -> CardClient:
        injected = self.server.card_client
        if injected is None:
            return CardClient(cache_dir_for(), refresh=refresh)
        return injected(refresh=refresh)

    # -- helpers ----------------------------------------------------------

    def _read_form(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, "Bad Content-Length.")
            return None

        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {
            key: values[-1]
            for key, values in parse_qs(raw, keep_blank_values=True).items()
        }

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, status: int, message: str) -> None:
        self._send_html(
            status,
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            f"<title>{status}</title><p>{escape(message)}</p></html>",
        )


def make_server(
    store_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh: bool = False,
    card_client=None,
) -> LockerHTTPServer:
    return LockerHTTPServer(
        (host, port),
        LockerHandler,
        store_path=store_path,
        refresh=refresh,
        card_client=card_client,
    )


def bind_server(
    store_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh: bool = False,
    attempts: int = 20,
) -> LockerHTTPServer:
    """Bind the first free port at or above ``port``."""
    last_error: OSError | None = None
    for candidate in range(port, port + attempts):
        try:
            return make_server(store_path, host=host, port=candidate, refresh=refresh)
        except OSError as exc:
            last_error = exc

    raise OSError(
        f"no free port in {port}..{port + attempts - 1}"
    ) from last_error


def serve(
    store_path: Path,
    *,
    port: int = 8765,
    open_browser: bool = True,
    refresh: bool = False,
) -> int:
    try:
        server = bind_server(store_path, port=port, refresh=refresh)
    except OSError as exc:
        print(f"locker: {exc}", file=sys.stderr)
        return 1

    host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    # flush: with stdout redirected to a file the URL would otherwise sit in the
    # buffer until the server exits, leaving the user with no address to open.
    print(f"Launch Locker is serving at {url}", flush=True)

    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0
