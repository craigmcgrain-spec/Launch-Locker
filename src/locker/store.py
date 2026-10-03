"""The single owner of the locker file: load, validate, and persist it.

Nothing else in the package reads or writes the locker file. Commands are stored
verbatim; this module never normalises or re-quotes them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 1


class StoreError(Exception):
    """Base class for every store failure."""


class StoreCorrupt(StoreError):
    """The store file exists but cannot be understood.

    Raised for malformed JSON and for valid JSON whose shape does not match the
    schema. A corrupt store is never overwritten, so this is always safe to
    surface to the user as a refusal rather than a repair.
    """


class ReadOnlyStore(StoreError):
    """The store file was written by a newer version of Launch Locker."""


@dataclass(frozen=True)
class Entry:
    name: str
    command: str
    hf_repo: str | None
    created: str
    updated: str


def utcnow() -> str:
    """ISO 8601 UTC, seconds precision, ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_store_path(
    cli_path: str | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """Locate the locker file: ``--file``, then ``LOCKER_FILE``, then XDG."""
    if cli_path is not None:
        return Path(cli_path).expanduser()

    env = os.environ if env is None else env

    env_file = env.get("LOCKER_FILE")
    if env_file:
        return Path(env_file).expanduser()

    config_home = env.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "locker" / "lockers.json"


def _validate(raw: object, path: Path) -> tuple[dict[str, Entry], bool]:
    """Validate a parsed store, raising :class:`StoreCorrupt` on any surprise.

    Validation is deliberately strict and complete: callers must never see a
    raw ``KeyError``, ``TypeError``, or ``AttributeError`` from bad data.
    """
    if not isinstance(raw, dict):
        raise StoreCorrupt(f"{path}: top level must be a JSON object")

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise StoreCorrupt(f"{path}: 'version' must be an integer")

    lockers = raw.get("lockers")
    if not isinstance(lockers, dict):
        raise StoreCorrupt(f"{path}: 'lockers' must be a JSON object")

    entries: dict[str, Entry] = {}
    for name, item in lockers.items():
        if not isinstance(name, str) or not name:
            raise StoreCorrupt(f"{path}: locker names must be non-empty strings")
        if not isinstance(item, dict):
            raise StoreCorrupt(f"{path}: entry {name!r} must be a JSON object")

        command = item.get("command")
        if not isinstance(command, str) or not command:
            raise StoreCorrupt(
                f"{path}: entry {name!r} needs a non-empty string 'command'"
            )

        hf_repo = item.get("hf_repo")
        if hf_repo is not None and not isinstance(hf_repo, str):
            raise StoreCorrupt(
                f"{path}: entry {name!r} 'hf_repo' must be a string or null"
            )

        stamps = {}
        for field in ("created", "updated"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise StoreCorrupt(
                    f"{path}: entry {name!r} needs a non-empty string {field!r}"
                )
            stamps[field] = value

        entries[name] = Entry(
            name=name,
            command=command,
            hf_repo=hf_repo,
            created=stamps["created"],
            updated=stamps["updated"],
        )

    return entries, version > SCHEMA_VERSION


class Store:
    """An in-memory view of the locker file, saved on every mutation."""

    def __init__(
        self,
        path: Path,
        entries: dict[str, Entry] | None = None,
        readonly: bool = False,
    ) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._entries: dict[str, Entry] = entries if entries is not None else {}

    @classmethod
    def open(cls, path: Path) -> Store:
        """Load the store. A missing file is an empty store, not an error."""
        path = Path(path)
        if not path.exists():
            return cls(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreCorrupt(f"{path}: {exc}") from exc

        entries, readonly = _validate(raw, path)
        return cls(path, entries, readonly)

    def entries(self) -> list[Entry]:
        """Entries in insertion order."""
        return list(self._entries.values())

    def put(self, entry: Entry) -> None:
        """Store an already-built entry and persist immediately."""
        self._ensure_writable()
        self._entries[entry.name] = entry
        self._save()

    def _ensure_writable(self) -> None:
        if self.readonly:
            raise ReadOnlyStore(
                f"{self.path}: written by a newer version of Launch Locker; "
                "refusing to modify it"
            )

    def _save(self) -> None:
        """Write atomically: temp file in the same directory, then rename."""
        self._ensure_writable()

        payload = {
            "version": SCHEMA_VERSION,
            "lockers": {
                name: {
                    "command": entry.command,
                    "hf_repo": entry.hf_repo,
                    "created": entry.created,
                    "updated": entry.updated,
                }
                for name, entry in self._entries.items()
            },
        }

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        tmp = parent / (self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
