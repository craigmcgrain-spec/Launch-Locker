"""Hugging Face model-card support: detecting the repo, fetching, and caching.

Detection is pure and offline. Everything that touches the network lives behind
an injectable opener so tests never reach huggingface.co.
"""

from __future__ import annotations

import re
import shlex

REPO_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_MODEL_FILE_SUFFIXES = (".gguf", ".safetensors", ".bin", ".pt")

# Flags whose following value may name a repo. The value is still validated, so
# `--model ./local.gguf` is skipped rather than mistaken for a repo id.
_REPO_FLAGS = frozenset(
    {"-hf", "--hf", "--hf-repo", "--repo-id", "--model", "-m", "--model-id"}
)


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
