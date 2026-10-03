"""Tests for the local web UI: routes, guards, escaping, and port fallback."""

import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode

from locker.hf import Card, CardUnavailable, RepoNotFound
from locker.server import bind_server, host_is_loopback, make_server, render_page
from locker.store import Store


class HostCheckTests(unittest.TestCase):
    def test_accepts_loopback_hosts(self):
        for header in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1"):
            with self.subTest(host=header):
                self.assertTrue(host_is_loopback(header, 8765))

    def test_rejects_other_hosts(self):
        for header in ("evil.example.com:8765", "192.168.1.5:8765", "", "localhost:9"):
            with self.subTest(host=header):
                self.assertFalse(host_is_loopback(header, 8765))


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "lockers.json"

    def start(self, **kwargs):
        server = make_server(self.path, port=0, **kwargs)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        # A short poll interval keeps shutdown (and therefore the suite) quick.
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return server

    def request(self, server, method, path, *, body=None, headers=None):
        """Issue a request without following redirects, so 303 is observable."""
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            payload = urlencode(body) if body is not None else None
            sent = {"Content-Type": "application/x-www-form-urlencoded"}
            sent.update(headers or {})
            conn.request(method, path, body=payload, headers=sent)
            response = conn.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            conn.close()


class RouteTests(ServerTestCase):
    def test_get_index_renders_lockers(self):
        Store.open(self.path).add("qwen3.8", "llama-server -hf Qwen/Qwen3-8B")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("qwen3.8", body)
        self.assertIn("llama-server -hf Qwen/Qwen3-8B", body)

    def test_empty_store_renders_empty_state(self):
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("No lockers yet", body)
        self.assertNotIn("<table", body)

    def test_post_creates_locker(self):
        server = self.start()

        status, body, headers = self.request(
            server, "POST", "/lockers", body={"name": "qwen3.8", "command": "echo hi"}
        )

        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/")
        self.assertEqual(Store.open(self.path).get("qwen3.8").command, "echo hi")

    def test_post_duplicate_returns_409_and_does_not_write(self):
        Store.open(self.path).add("x", "cmd")
        server = self.start()

        status, body, _ = self.request(
            server, "POST", "/lockers", body={"name": "x", "command": "other"}
        )

        self.assertEqual(status, 409)
        self.assertEqual(Store.open(self.path).get("x").command, "cmd")

    def test_get_edit_form(self):
        Store.open(self.path).add("x", "echo hi")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/lockers/x/edit")

        self.assertEqual(status, 200)
        self.assertIn("echo hi", body)

    def test_post_update_changes_entry(self):
        Store.open(self.path).add("x", "old")
        server = self.start()

        status, body, _ = self.request(
            server, "POST", "/lockers/x", body={"name": "x", "command": "new"}
        )

        self.assertEqual(status, 303)
        self.assertEqual(Store.open(self.path).get("x").command, "new")

    def test_post_delete_removes_entry(self):
        Store.open(self.path).add("x", "cmd")
        server = self.start()

        status, body, _ = self.request(server, "POST", "/lockers/x/delete")

        self.assertEqual(status, 303)
        self.assertEqual(Store.open(self.path).entries(), [])

    def test_unknown_route_is_404(self):
        server = self.start()

        status, _, _ = self.request(server, "GET", "/nope")

        self.assertEqual(status, 404)

    def test_wrong_method_is_405(self):
        Store.open(self.path).add("x", "cmd")
        server = self.start()

        status, _, _ = self.request(server, "GET", "/lockers/x")

        self.assertEqual(status, 405)

    def test_malformed_form_data_is_400(self):
        server = self.start()

        status, _, _ = self.request(
            server, "POST", "/lockers", body={"command": "echo hi"}
        )

        self.assertEqual(status, 400)
        self.assertFalse(self.path.exists())


class SecurityTests(ServerTestCase):
    def test_non_loopback_host_is_403(self):
        server = self.start()
        port = server.server_address[1]

        status, _, _ = self.request(
            server,
            "POST",
            "/lockers",
            body={"name": "x", "command": "c"},
            headers={"Host": f"evil.example.com:{port}"},
        )

        self.assertEqual(status, 403)
        self.assertFalse(self.path.exists())

    def test_cross_origin_post_is_403(self):
        server = self.start()

        status, _, _ = self.request(
            server,
            "POST",
            "/lockers",
            body={"name": "x", "command": "c"},
            headers={"Origin": "http://evil.example.com"},
        )

        self.assertEqual(status, 403)
        self.assertFalse(self.path.exists())

    def test_names_are_html_escaped_in_the_page(self):
        Store.open(self.path).add("<script>x</script>", "cmd")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertNotIn("<script>x</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_commands_are_html_escaped_in_the_page(self):
        Store.open(self.path).add("x", "echo <b>bold</b>")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertNotIn("<b>bold</b>", body)
        self.assertIn("&lt;b&gt;", body)


class SpecialNameTests(ServerTestCase):
    """Review Focus 1: names with /, spaces, #, %, and non-ASCII in URLs."""

    NAMES = ["a/b", "qwen 3.8", "a#b", "a%b", "qwen-日本語"]

    def test_names_with_special_characters_round_trip(self):
        server = self.start()

        for name in self.NAMES:
            with self.subTest(name=name):
                quoted = quote(name, safe="")

                status, _, _ = self.request(
                    server, "POST", "/lockers", body={"name": name, "command": "echo hi"}
                )
                self.assertEqual(status, 303)

                status, body, _ = self.request(server, "GET", f"/lockers/{quoted}/edit")
                self.assertEqual(status, 200)
                self.assertIn("echo hi", body)

                status, _, _ = self.request(
                    server,
                    "POST",
                    f"/lockers/{quoted}",
                    body={"name": name, "command": "echo edited"},
                )
                self.assertEqual(status, 303)
                self.assertEqual(Store.open(self.path).get(name).command, "echo edited")

                status, _, _ = self.request(server, "POST", f"/lockers/{quoted}/delete")
                self.assertEqual(status, 303)
                self.assertNotIn(
                    name, [entry.name for entry in Store.open(self.path).entries()]
                )

    def test_special_names_survive_the_rendered_page(self):
        Store.open(self.path).add("a/b", "echo hi")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("a/b", body)

    def test_colliding_suffix_names_do_not_misroute(self):
        """A name ending in /delete or /edit must not be read as a route."""
        server = self.start()
        for name in ("x", "x/delete", "x/edit"):
            status, _, _ = self.request(
                server, "POST", "/lockers", body={"name": name, "command": "echo hi"}
            )
            self.assertEqual(status, 303)

        # Updating "x/delete" used to match the delete route for "x".
        status, _, _ = self.request(
            server,
            "POST",
            "/lockers/" + quote("x/delete", safe=""),
            body={"name": "x/delete", "command": "echo edited"},
        )
        self.assertEqual(status, 303)

        entries = {entry.name: entry.command for entry in Store.open(self.path).entries()}
        self.assertEqual(
            entries.get("x"), "echo hi", "the locker named 'x' must be untouched"
        )
        self.assertEqual(entries.get("x/edit"), "echo hi")
        self.assertEqual(entries.get("x/delete"), "echo edited")

    def test_name_with_newline_round_trips(self):
        server = self.start()
        name = "nl\nname"
        quoted = quote(name, safe="")

        status, _, _ = self.request(
            server, "POST", "/lockers", body={"name": name, "command": "echo hi"}
        )
        self.assertEqual(status, 303)

        status, body, _ = self.request(server, "GET", f"/lockers/{quoted}/edit")
        self.assertEqual(status, 200)
        self.assertIn("echo hi", body)

        status, _, _ = self.request(server, "POST", f"/lockers/{quoted}/delete")
        self.assertEqual(status, 303)
        self.assertEqual(Store.open(self.path).entries(), [])

    def test_selecting_a_locker_for_the_card_panel(self):
        Store.open(self.path).add("one", "cmd1")
        Store.open(self.path).add("two", "cmd2")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/?selected=two")

        self.assertEqual(status, 200)
        self.assertIn('data-selected="two"', body)
        self.assertIn("?selected=one", body, "each row must offer a selection link")


class PageTests(ServerTestCase):
    def test_render_page_defaults_selection_to_the_first_locker(self):
        Store.open(self.path).add("one", "cmd1")
        Store.open(self.path).add("two", "cmd2")

        body = render_page(Store.open(self.path).entries())

        self.assertIn('data-selected="one"', body)

    def test_port_fallback_when_busy(self):
        blocker = make_server(self.path, port=0)
        self.addCleanup(blocker.server_close)
        busy = blocker.server_address[1]

        server = bind_server(self.path, port=busy)
        self.addCleanup(server.server_close)

        self.assertGreater(server.server_address[1], busy)


class StubCardClient:
    """Stands in for CardClient so no test touches the network."""

    def __init__(self, card=None, error=None):
        self.card = card
        self.error = error
        self.calls = []

    def card_for(self, repo):
        self.calls.append(repo)
        if self.error is not None:
            raise self.error
        return self.card, False


def sample_card(repo="Qwen/Qwen3-8B"):
    return Card(
        repo=repo,
        api={
            "downloads": 10,
            "likes": 2,
            "pipeline_tag": "text-generation",
            "lastModified": "2026-01-01T00:00:00.000Z",
            "tags": ["license:apache-2.0", "safetensors", "text-generation"],
        },
        readme="# Title\n\nBody",
        fetched_at=1000.0,
    )


class CardApiTests(ServerTestCase):
    def start_with(self, client):
        return self.start(card_client=lambda refresh=False: client)

    def test_api_card_returns_metadata(self):
        server = self.start_with(StubCardClient(card=sample_card()))

        status, body, _ = self.request(server, "GET", "/api/card?repo=Qwen/Qwen3-8B")

        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["repo"], "Qwen/Qwen3-8B")
        self.assertEqual(data["author"], "Qwen")
        self.assertEqual(data["downloads"], 10)
        self.assertEqual(data["likes"], 2)
        self.assertEqual(data["pipeline_tag"], "text-generation")
        self.assertEqual(data["license"], "apache-2.0")
        self.assertEqual(data["last_modified"], "2026-01-01T00:00:00.000Z")
        self.assertIn("safetensors", data["tags"])
        self.assertFalse(data["cached"])
        self.assertIn("<h1>Title</h1>", data["readme_html"])

    def test_api_card_404_repo_returns_error_body(self):
        server = self.start_with(StubCardClient(error=RepoNotFound("no such repo")))

        status, body, _ = self.request(server, "GET", "/api/card?repo=X/Y")

        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["error"], "repo_not_found")
        self.assertIn("message", data)

    def test_api_card_offline_returns_unavailable(self):
        server = self.start_with(StubCardClient(error=CardUnavailable("offline")))

        status, body, _ = self.request(server, "GET", "/api/card?repo=X/Y")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"], "unavailable")

    def test_api_card_no_candidate(self):
        stub = StubCardClient()
        server = self.start_with(stub)

        status, body, _ = self.request(
            server, "GET", "/api/card?command=" + quote("llama-server --port 8080")
        )

        self.assertEqual(json.loads(body)["error"], "no_candidate")
        self.assertEqual(stub.calls, [])

    def test_api_card_rejects_a_malicious_repo_parameter(self):
        stub = StubCardClient(card=sample_card())
        server = self.start_with(stub)

        for bad in ("../../etc/passwd", "javascript:alert(1)"):
            with self.subTest(repo=bad):
                status, body, _ = self.request(
                    server, "GET", "/api/card?repo=" + quote(bad, safe="")
                )
                self.assertEqual(json.loads(body)["error"], "no_candidate")

        self.assertEqual(stub.calls, [], "a hostile repo must never be fetched")

    def test_api_card_refresh_parameter_bypasses_cache(self):
        seen = {}

        def factory(refresh=False):
            seen["refresh"] = refresh
            return StubCardClient(card=sample_card())

        server = self.start(card_client=factory)

        self.request(server, "GET", "/api/card?repo=Qwen/Qwen3-8B")
        self.assertFalse(seen["refresh"])

        self.request(server, "GET", "/api/card?repo=Qwen/Qwen3-8B&refresh=1")
        self.assertTrue(seen["refresh"])


class PanelTests(ServerTestCase):
    def test_index_includes_card_panel_and_script(self):
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn('id="card-panel"', body)
        self.assertIn("/api/card", body)

    def test_index_includes_copy_button(self):
        Store.open(self.path).add("x", "echo hi")
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn('class="copy"', body)
        # The button must be wired to the clipboard, not merely present: the
        # markup existed before this task, the handler is what this task adds.
        self.assertIn("navigator.clipboard", body)

    def test_repo_hint_lives_outside_the_card_panel(self):
        # The panel's innerHTML is replaced when a card loads, which would
        # detach a hint element nested inside it.
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn('id="repo-hint"', body)
        panel = body[body.index('id="card-panel"') : body.index("</aside>")]
        self.assertNotIn("repo-hint", panel)

    def test_placeholder_like_text_in_a_command_is_never_substituted(self):
        command = 'echo "{{selected}}" and {{hf_repo}}'
        Store.open(self.path).add("x", command)
        server = self.start()

        status, body, _ = self.request(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("{{selected}}", body)
        self.assertIn("{{hf_repo}}", body)

        status, body, _ = self.request(server, "GET", "/lockers/x/edit")
        self.assertEqual(status, 200)
        self.assertIn("{{selected}}", body)
        self.assertIn("{{hf_repo}}", body)


if __name__ == "__main__":
    unittest.main()
