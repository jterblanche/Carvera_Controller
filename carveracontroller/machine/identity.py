"""Controller identity: the id, the display name and the launch part.

Sent in the hello handshake so the machine and other controllers can
recognise this one. The id is the computer part (machine/computer_part.py),
worked out at every start and never kept in the settings; the launch part is
chosen at every start. Only the display name is kept in the settings.
Kivy-free: persistence is done through the ``IdentityStore`` protocol,
implemented by a thin adapter over whatever settings storage the application
uses.
"""

from __future__ import annotations

import secrets
import socket
from dataclasses import dataclass, replace
from typing import Protocol

MAX_NAME_BYTES = 31

# Where older versions kept a random id; named here only to remove it.
_KEY_ID = "controller_id"
_KEY_NAME = "controller_name"


def generate_id() -> int:
    """A random 64-bit id, for a Controller built without an identity (tests,
    tools). Collisions are not a practical concern at this scale."""
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
    """``launch`` is the launch part sent after the features byte in every
    hello of this start, or None to send none."""

    id: int
    name: str
    launch: int | None = None

    def __post_init__(self) -> None:
        trimmed = trim_name(self.name)
        if trimmed != self.name:
            object.__setattr__(self, "name", trimmed)


class IdentityStore(Protocol):
    """Minimal key/value persistence the identity needs from the app's settings."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def remove(self, key: str) -> None: ...


def load_identity(store: IdentityStore, computer_id: int, launch: int) -> ControllerIdentity:
    """The identity for this start: the given computer part and launch part,
    and the persisted display name (a default one is created and persisted
    if there is none).

    Removes the random id older versions kept in the settings, so a copied
    settings file carries no id at all. Nothing else in the settings
    changes.
    """
    store.remove(_KEY_ID)

    raw_name = store.get(_KEY_NAME)
    name = raw_name if raw_name else default_name()
    if not raw_name:
        store.set(_KEY_NAME, name)

    return ControllerIdentity(id=computer_id, name=name, launch=launch)


def set_name(store: IdentityStore, name: str, identity: ControllerIdentity) -> ControllerIdentity:
    """Update and persist the display name (trimmed to the wire limit),
    keeping the id and launch part."""
    trimmed = trim_name(name) or default_name()
    store.set(_KEY_NAME, trimmed)
    return replace(identity, name=trimmed)


def with_id(identity: ControllerIdentity, new_id: int) -> ControllerIdentity:
    """The same identity under another id (same name and launch part)."""
    return replace(identity, id=new_id)
