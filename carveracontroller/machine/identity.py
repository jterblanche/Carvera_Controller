"""Controller identity: a stable random id and a display name.

Sent in the hello handshake so the machine and other controllers can
recognise this one (docs/protocol/connection-follows-me.md §6.1). Kivy-free:
persistence is done through the ``IdentityStore`` protocol, implemented by a
thin adapter over whatever settings storage the application uses.
"""

from __future__ import annotations

import secrets
import socket
from dataclasses import dataclass
from typing import Protocol

MAX_NAME_BYTES = 31

_KEY_ID = "controller_id"
_KEY_NAME = "controller_name"


def generate_id() -> int:
    """A random 64-bit id. Collisions are not a practical concern at this scale."""
    return secrets.randbits(64)


def trim_name(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Trim ``name`` to at most ``max_bytes`` UTF-8 bytes.

    Never splits a multi-byte character: trims back from the byte limit to
    the nearest valid UTF-8 boundary.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    trimmed = encoded[:max_bytes]
    while trimmed:
        try:
            return trimmed.decode("utf-8")
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    return ""


def default_name() -> str:
    """The computer's name, trimmed to the wire limit."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return trim_name(host or "Controller")


@dataclass(frozen=True)
class ControllerIdentity:
    id: int
    name: str

    def __post_init__(self) -> None:
        trimmed = trim_name(self.name)
        if trimmed != self.name:
            object.__setattr__(self, "name", trimmed)


class IdentityStore(Protocol):
    """Minimal key/value persistence the identity needs from the app's settings."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def load_or_create_identity(store: IdentityStore) -> ControllerIdentity:
    """Load the persisted identity, creating and persisting a fresh one if absent."""
    raw_id = store.get(_KEY_ID)
    controller_id: int | None
    try:
        controller_id = int(raw_id) if raw_id else None
    except ValueError:
        controller_id = None
    if controller_id is None:
        controller_id = generate_id()
        store.set(_KEY_ID, str(controller_id))

    raw_name = store.get(_KEY_NAME)
    name = raw_name if raw_name else default_name()
    if not raw_name:
        store.set(_KEY_NAME, name)

    return ControllerIdentity(id=controller_id, name=name)


def set_name(store: IdentityStore, name: str) -> ControllerIdentity:
    """Update and persist the display name (trimmed to the wire limit)."""
    trimmed = trim_name(name) or default_name()
    store.set(_KEY_NAME, trimmed)

    raw_id = store.get(_KEY_ID)
    try:
        controller_id = int(raw_id) if raw_id else None
    except ValueError:
        controller_id = None
    if controller_id is None:
        controller_id = generate_id()
        store.set(_KEY_ID, str(controller_id))

    return ControllerIdentity(id=controller_id, name=trimmed)
