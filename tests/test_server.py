"""Tests for the local web UI: routes, guards, escaping, and port fallback."""

import http.client
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode

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


if __name__ == "__main__":
    unittest.main()
