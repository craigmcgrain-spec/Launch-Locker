"""Tests for Hugging Face detection, card fetching, caching, and fallback.

No test touches the network: `CardClient` takes an injectable opener.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError

from locker.hf import (
    CardClient,
    CardUnavailable,
    NoCandidateDetected,
    RepoNotFound,
    cache_dir_for,
    detect_repos,
    is_repo_id,
    lookup,
)
from locker.md import MAX_README_CHARS


def api_url(repo: str) -> str:
    return f"https://huggingface.co/api/models/{repo}"


def readme_url(repo: str) -> str:
    return f"https://huggingface.co/{repo}/raw/main/README.md"


def http_404(url: str = "https://huggingface.co/x") -> HTTPError:
    return HTTPError(url, 404, "Not Found", {}, None)


def http_401(url: str = "https://huggingface.co/x") -> HTTPError:
    return HTTPError(url, 401, "Unauthorized", {}, None)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeOpener:
    """Serves canned bytes by URL, records requests, raises for unknown URLs."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]

    def __call__(self, request, timeout=None):
        url = getattr(request, "full_url", request)
        headers = dict(getattr(request, "headers", None) or {})
        self.requests.append((url, headers))

        result = self.responses.get(url)
        if result is None:
            raise http_404(url)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


CARD_API = json.dumps({"downloads": 5, "likes": 1, "tags": ["text-generation"]})


class CardTestCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cache = self.dir / "cache"

    def opener(self, repo="Qwen/Qwen3-8B", api=CARD_API, readme=b"# Hi\n"):
        responses = {}
        if api is not None:
            responses[api_url(repo)] = api.encode() if isinstance(api, str) else api
        if readme is not None:
            responses[readme_url(repo)] = readme
        return FakeOpener(responses)

    def client(self, opener, **kwargs):
        kwargs.setdefault("now", lambda: 1000.0)
        return CardClient(self.cache, opener=opener, **kwargs)


class DetectionTests(unittest.TestCase):
    def test_detects_hf_flag(self):
        self.assertEqual(
            detect_repos("llama-server -hf Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
        )

    def test_detects_hf_repo_and_repo_id_flags(self):
        for flag in ("--hf", "--hf-repo", "--repo-id"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    detect_repos(f"tool {flag} Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
                )

    def test_detects_model_flag_value(self):
        self.assertEqual(
            detect_repos("llama-server --model unsloth/Qwen3-8B-GGUF"),
            ["unsloth/Qwen3-8B-GGUF"],
        )

    def test_model_flag_with_local_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server --model ./models/qwen.gguf"), [])

    def test_model_flag_with_absolute_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server --model /models/x.gguf"), [])

    def test_gguf_extension_is_skipped(self):
        self.assertEqual(
            detect_repos("llama-server -hf Qwen/Qwen3-8B-GGUF/./x.gguf"), []
        )

        for suffix in (".gguf", ".safetensors", ".bin", ".pt"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    detect_repos(f"llama-server -hf owner/name{suffix}"), []
                )

    def test_url_is_skipped(self):
        found = detect_repos("open https://huggingface.co/Qwen/Qwen3-8B")

        self.assertEqual([c for c in found if "://" in c], [])

    def test_bare_token_is_detected(self):
        self.assertEqual(
            detect_repos("some-tool Qwen/Qwen3-8B --port 8080"), ["Qwen/Qwen3-8B"]
        )

    def test_home_relative_path_is_skipped(self):
        self.assertEqual(detect_repos("llama-server -m ~/models/qwen"), [])

    def test_three_segment_path_is_skipped(self):
        self.assertEqual(detect_repos("tool a/b/c"), [])

    def test_flags_come_before_bare_tokens(self):
        self.assertEqual(detect_repos("tool --model X/Y Z/W"), ["X/Y", "Z/W"])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(
            detect_repos("tool -hf Qwen/Qwen3-8B Qwen/Qwen3-8B"), ["Qwen/Qwen3-8B"]
        )

    def test_quant_suffix_is_stripped(self):
        # llama.cpp documents `-hf <repo>[:quant]`; the quant names a file inside
        # the repo, so the repo is everything before the colon.
        self.assertEqual(
            detect_repos("llama-server -hf unsloth/Qwen3-8B-GGUF:Q4_K_M"),
            ["unsloth/Qwen3-8B-GGUF"],
        )

    def test_no_candidates_returns_empty_list(self):
        self.assertEqual(detect_repos("llama-server --port 8080"), [])

    def test_is_repo_id_rejects_bad_shapes(self):
        for bad in ("", "a", "a/", "/b", "a/b/c", "a b/c"):
            with self.subTest(candidate=bad):
                self.assertFalse(is_repo_id(bad))

        self.assertTrue(is_repo_id("Qwen/Qwen3-8B"))
        self.assertTrue(is_repo_id("unsloth/Qwen3-8B-GGUF"))


class FetchTests(CardTestCase):
    def test_fetches_api_and_readme_and_caches(self):
        opener = self.opener()
        card, from_cache = self.client(opener).card_for("Qwen/Qwen3-8B")

        self.assertEqual(card.api["downloads"], 5)
        self.assertIn("Hi", card.readme)
        self.assertFalse(from_cache)
        self.assertEqual(len(opener.requests), 2)

        second_opener = self.opener()
        cached, from_cache = self.client(second_opener).card_for("Qwen/Qwen3-8B")

        self.assertTrue(from_cache)
        self.assertEqual(second_opener.requests, [])
        self.assertEqual(cached.api, card.api)

    def test_cache_expiry(self):
        self.client(self.opener(), now=lambda: 1000.0).card_for("Qwen/Qwen3-8B")

        opener = self.opener()
        self.client(opener, now=lambda: 1000.0 + 86401).card_for("Qwen/Qwen3-8B")

        self.assertEqual(len(opener.requests), 2)

    def test_refresh_bypasses_fresh_cache(self):
        self.client(self.opener()).card_for("Qwen/Qwen3-8B")

        opener = self.opener()
        _, from_cache = self.client(opener, refresh=True).card_for("Qwen/Qwen3-8B")

        self.assertFalse(from_cache)
        self.assertEqual(len(opener.requests), 2)

    def test_cache_is_written_atomically(self):
        self.client(self.opener()).card_for("Qwen/Qwen3-8B")

        self.assertEqual(list(self.cache.rglob("*.tmp")), [])

    def test_repo_404_raises_repo_not_found(self):
        with self.assertRaises(RepoNotFound):
            self.client(FakeOpener({})).card_for("Qwen/Qwen3-8B")

    def test_repo_401_is_treated_as_not_found(self):
        # Hugging Face answers 401, not 404, for a repo that does not exist or is
        # gated (verified against the live API: both an invented repo and
        # meta-llama/Llama-3-70B return 401). Reporting that as `unavailable`
        # would tell the user to retry a request that can never succeed.
        url = api_url("Qwen/Qwen3-8B")
        opener = FakeOpener({url: http_401(url)})

        with self.assertRaises(RepoNotFound):
            self.client(opener).card_for("Qwen/Qwen3-8B")

    def test_timeout_raises_card_unavailable(self):
        opener = FakeOpener({api_url("Qwen/Qwen3-8B"): TimeoutError("timed out")})

        with self.assertRaises(CardUnavailable):
            self.client(opener).card_for("Qwen/Qwen3-8B")

    def test_offline_with_stale_cache_returns_stale_card(self):
        self.client(self.opener(), now=lambda: 1000.0).card_for("Qwen/Qwen3-8B")

        offline = FakeOpener({api_url("Qwen/Qwen3-8B"): URLError("offline")})
        card, from_cache = self.client(offline, now=lambda: 10_000_000.0).card_for(
            "Qwen/Qwen3-8B"
        )

        self.assertTrue(from_cache)
        self.assertEqual(card.api["downloads"], 5)

    def test_offline_without_cache_raises_card_unavailable(self):
        offline = FakeOpener({api_url("Qwen/Qwen3-8B"): URLError("offline")})

        with self.assertRaises(CardUnavailable):
            self.client(offline).card_for("Qwen/Qwen3-8B")

    def test_missing_readme_still_returns_card(self):
        opener = self.opener(readme=None)

        card, _ = self.client(opener).card_for("Qwen/Qwen3-8B")

        self.assertEqual(card.readme, "")
        self.assertEqual(card.api["downloads"], 5)

    def test_empty_readme_still_returns_card(self):
        card, _ = self.client(self.opener(readme=b"")).card_for("Qwen/Qwen3-8B")

        self.assertEqual(card.readme, "")

    def test_crlf_readme_is_normalised(self):
        card, _ = self.client(
            self.opener(readme=b"# Title\r\n\r\nBody\r\n")
        ).card_for("Qwen/Qwen3-8B")

        self.assertNotIn("\r", card.readme)
        self.assertIn("Body", card.readme)

    def test_readme_is_truncated_before_caching(self):
        huge = b"x" * (MAX_README_CHARS + 5000)
        self.client(self.opener(readme=huge)).card_for("Qwen/Qwen3-8B")

        cached = self.cache / "Qwen__Qwen3-8B" / "README.md"

        self.assertTrue(cached.exists())
        self.assertLessEqual(cached.stat().st_size, MAX_README_CHARS)

    def test_user_agent_is_set(self):
        opener = self.opener()
        self.client(opener).card_for("Qwen/Qwen3-8B")

        for url, headers in opener.requests:
            with self.subTest(url=url):
                normalized = {k.lower(): v for k, v in headers.items()}
                self.assertIn("launch-locker", normalized.get("user-agent", "").lower())

    def test_readme_read_failure_does_not_lose_metadata(self):
        opener = self.opener()
        opener.responses[readme_url("Qwen/Qwen3-8B")] = URLError("boom")

        card, _ = self.client(opener).card_for("Qwen/Qwen3-8B")

        self.assertEqual(card.readme, "")
        self.assertEqual(card.api["downloads"], 5)


class LookupTests(CardTestCase):
    def test_pinned_repo_skips_detection(self):
        opener = self.opener(repo="X/Y")
        card, _ = lookup("tool A/B", "X/Y", self.client(opener))

        self.assertEqual(card.repo, "X/Y")
        self.assertEqual(set(opener.urls), {api_url("X/Y"), readme_url("X/Y")})

    def test_candidates_advance_on_404(self):
        opener = self.opener(repo="C/D")
        card, _ = lookup("tool A/B C/D", None, self.client(opener))

        self.assertEqual(card.repo, "C/D")

    def test_all_candidates_404_raises_repo_not_found(self):
        opener = FakeOpener({})

        with self.assertRaises(RepoNotFound) as caught:
            lookup("tool A/B C/D", None, self.client(opener))

        self.assertIn("A/B", str(caught.exception))

    def test_no_candidate_raises_no_candidate_detected(self):
        with self.assertRaises(NoCandidateDetected):
            lookup("llama-server --port 8080", None, self.client(FakeOpener({})))


class CacheDirTests(unittest.TestCase):
    def test_respects_xdg_cache_home(self):
        self.assertEqual(
            cache_dir_for({"XDG_CACHE_HOME": "/tmp/cache"}),
            Path("/tmp/cache/locker/hf"),
        )

    def test_falls_back_to_dot_cache(self):
        self.assertTrue(str(cache_dir_for({})).endswith(".cache/locker/hf"))


if __name__ == "__main__":
    unittest.main()
