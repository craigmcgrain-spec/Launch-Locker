"""Hugging Face model-card support: detecting the repo, fetching, and caching.

Detection is pure and offline. Everything that touches the network lives behind
an injectable opener so tests never reach huggingface.co.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .md import MAX_README_CHARS

REPO_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 86_400
USER_AGENT = f"Launch-Locker/{__version__}"

_MODEL_FILE_SUFFIXES = (".gguf", ".safetensors", ".bin", ".pt")

# Flags whose following value may name a repo. The value is still validated, so
# `--model ./local.gguf` is skipped rather than mistaken for a repo id.
_REPO_FLAGS = frozenset(
    {"-hf", "--hf", "--hf-repo", "--repo-id", "--model", "-m", "--model-id"}
)


class CardError(Exception):
    """Base class for every model-card failure."""


class RepoNotFound(CardError):
    """Hugging Face answered 404: no such repo."""


class CardUnavailable(CardError):
    """The card could not be fetched and no cache could stand in for it."""


class NoCandidateDetected(CardError):
    """The command names no Hugging Face repo."""


@dataclass(frozen=True)
class Card:
    repo: str
    api: dict
    readme: str
    fetched_at: float


def cache_dir_for(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CACHE_HOME/locker/hf``, defaulting to ``~/.cache/locker/hf``."""
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "locker" / "hf"


def is_repo_id(candidate: str) -> bool:
    """True for exactly ``owner/name`` with each side a plain identifier."""
    if candidate.count("/") != 1:
        return False

    owner, name = candidate.split("/")
    return bool(REPO_SEGMENT.match(owner)) and bool(REPO_SEGMENT.match(name))


def _is_path_like(token: str) -> bool:
    """True for URLs, filesystem paths, and model-file names."""
    return (
        "://" in token
        or token.startswith(("/", "~", "."))
        or token.lower().endswith(_MODEL_FILE_SUFFIXES)
    )


def detect_repos(command: str) -> list[str]:
    """Ordered, de-duplicated repo candidates found in a launch command.

    Explicit flags are considered before bare ``owner/name`` tokens, so a flag's
    value wins the first slot. Returns every candidate rather than one guess, so
    the caller can try each against the API in order.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Half-typed commands with unbalanced quotes are common; fall back to a
        # whitespace split rather than failing detection outright.
        tokens = command.split()

    found: list[str] = []
    seen: set[str] = set()

    def consider(token: str) -> None:
        if _is_path_like(token):
            return

        # llama.cpp documents `-hf <repo>[:quant]`, where the quant names a file
        # inside the repo rather than the repo itself. HF identifiers cannot
        # contain ':', so everything before the first colon is the repo.
        candidate = token.split(":", 1)[0]

        if candidate in seen or not is_repo_id(candidate):
            return
        seen.add(candidate)
        found.append(candidate)

    for index, token in enumerate(tokens):
        if token in _REPO_FLAGS and index + 1 < len(tokens):
            consider(tokens[index + 1])

    for token in tokens:
        consider(token)

    return found


def _api_url(repo: str) -> str:
    return f"https://huggingface.co/api/models/{repo}"


def _readme_url(repo: str) -> str:
    return f"https://huggingface.co/{repo}/raw/main/README.md"


def _cache_slug(repo: str) -> str:
    return repo.replace("/", "__")


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class CardClient:
    """Fetches and caches model cards.

    ``opener`` is injected so tests never touch the network; it defaults to
    ``urllib.request.urlopen``.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        opener: Callable | None = None,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        refresh: bool = False,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.refresh = refresh
        self._now = now
        self._opener = urlopen if opener is None else opener

    def card_for(self, repo: str) -> tuple[Card, bool]:
        """Return ``(card, from_cache)``.

        A fresh cache wins. A stale cache is served when the network fails, but
        never when the repo is definitively gone (404), because the fix differs.
        """
        cached = self._read_cache(repo)

        if cached is not None and not self.refresh:
            if self._now() - cached.fetched_at <= self.ttl_seconds:
                return cached, True

        try:
            card = self._fetch(repo)
        except CardUnavailable:
            if cached is not None:
                return cached, True
            raise

        self._write_cache(card)
        return card, False

    def _fetch(self, repo: str) -> Card:
        try:
            payload = json.loads(self._get(_api_url(repo)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CardUnavailable(f"{repo}: malformed model metadata") from exc

        if not isinstance(payload, dict):
            raise CardUnavailable(f"{repo}: model metadata is not an object")

        readme = ""
        try:
            text = self._get(_readme_url(repo)).decode("utf-8", "replace")
            readme = text.replace("\r\n", "\n").replace("\r", "\n")[:MAX_README_CHARS]
        except CardError:
            # The API already confirmed the repo exists, so metadata alone still
            # makes a useful card: a README problem must not lose it.
            readme = ""

        return Card(repo=repo, api=payload, readme=readme, fetched_at=self._now())

    def _get(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise RepoNotFound(f"Hugging Face has no repo named {url}") from exc
            if exc.code == 401:
                # Hugging Face answers 401 both for a repo that does not exist and
                # for a gated one, so this means "not available to you" rather
                # than a transient failure: retrying will never help.
                raise RepoNotFound(
                    f"Hugging Face has no public repo named {url} "
                    "(it may not exist, or it may require authentication)"
                ) from exc
            raise CardUnavailable(f"{url}: HTTP {exc.code}") from exc
        except (URLError, OSError) as exc:
            raise CardUnavailable(f"{url}: {exc}") from exc

    def _read_cache(self, repo: str) -> Card | None:
        directory = self.cache_dir / _cache_slug(repo)
        try:
            raw = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

        if not isinstance(raw, dict):
            return None

        fetched_at = raw.get("fetched_at")
        api = raw.get("api")
        if not isinstance(fetched_at, (int, float)) or not isinstance(api, dict):
            return None

        try:
            readme = (directory / "README.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            readme = ""

        return Card(
            repo=raw.get("repo") or repo,
            api=api,
            readme=readme,
            fetched_at=float(fetched_at),
        )

    def _write_cache(self, card: Card) -> None:
        directory = self.cache_dir / _cache_slug(card.repo)
        directory.mkdir(parents=True, exist_ok=True)

        meta = {"repo": card.repo, "fetched_at": card.fetched_at, "api": card.api}
        _atomic_write(
            directory / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2)
        )
        _atomic_write(directory / "README.md", card.readme)


def lookup(
    command: str, pinned: str | None, client: CardClient
) -> tuple[Card, bool]:
    """Resolve the card for a command.

    A pinned repo is authoritative and skips detection. Otherwise candidates are
    tried in order, advancing past any that Hugging Face reports as nonexistent.
    """
    if pinned:
        return client.card_for(pinned)

    candidates = detect_repos(command)
    if not candidates:
        raise NoCandidateDetected("no Hugging Face repo detected in this command")

    first_error: RepoNotFound | None = None
    for candidate in candidates:
        try:
            return client.card_for(candidate)
        except RepoNotFound as exc:
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise first_error
    raise NoCandidateDetected("no Hugging Face repo detected in this command")
