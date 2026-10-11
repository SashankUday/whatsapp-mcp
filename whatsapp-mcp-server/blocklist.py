"""Chat blocklist shared with the Go bridge.

The file lives at <repo root>/blocklist.txt unless WHATSAPP_MCP_BLOCKLIST is set.
One entry per line, '#' starts a comment. Blocking is per chat and by ID only:

- a number (phone number or LID digits; spaces, '+', '-' and brackets are
  ignored) blocks the 1:1 chat with that person;
- a JID blocks that chat: "<number>@s.whatsapp.net", "<lid>@lid" or a group
  "<id>@g.us".

A person's phone JID and LID are the same chat: given a resolver (which reads
whatsmeow_lid_map), an entry for either form blocks both. Blocking a person
does not hide their messages in group chats that aren't blocked.

Display names are never matched. Any line that isn't a number or a JID makes
the whole file invalid, so a leftover name or a mistyped number can't silently
unblock a chat.

The file is re-read on every lookup and reparsed when it changes, so edits
apply without a restart. A missing file blocks nothing; a file that exists but
can't be read or parsed raises BlocklistError so read tools fail instead of
returning unfiltered data.
"""

import os
import threading
from dataclasses import dataclass, field
from typing import Optional, Set

DEFAULT_BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'blocklist.txt')

_PHONE_SEPARATORS = set(" \t+-()[]")


class BlocklistError(Exception):
    """The blocklist file exists but could not be read or parsed."""


_PERSON_SERVERS = ("s.whatsapp.net", "lid")


@dataclass
class _Entries:
    numbers: Set[str] = field(default_factory=set)
    jids: Set[str] = field(default_factory=set)

    def matches(self, jid: Optional[str]) -> bool:
        """Whether this exact chat JID is blocked (aliases are handled by callers)."""
        if not jid:
            return False
        user, _, server = jid.strip().lower().partition("@")
        user = user.split(":", 1)[0]
        if f"{user}@{server}" in self.jids:
            return True
        return server in _PERSON_SERVERS and user in self.numbers


_lock = threading.Lock()
_cache_key = None
_cache_entries = _Entries()


def blocklist_path() -> str:
    return os.environ.get("WHATSAPP_MCP_BLOCKLIST") or DEFAULT_BLOCKLIST_PATH


def _normalize_phone(s: str) -> str:
    """Strip spaces, '+', '-' and brackets; return '' unless only digits remain."""
    digits = "".join(c for c in s if c not in _PHONE_SEPARATORS)
    return digits if digits.isdigit() and digits.isascii() else ""


def _parse(text: str) -> _Entries:
    entries = _Entries()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "@" in line:
            user, _, server = line.lower().partition("@")
            user = user.split(":", 1)[0]
            if user and server and " " not in line:
                entries.jids.add(f"{user}@{server}")
                continue
        elif _normalize_phone(line):
            entries.numbers.add(_normalize_phone(line))
            continue
        raise BlocklistError(
            f"Blocklist line {number} is not a phone number, LID or JID (names are not supported): {line!r}"
        )
    return entries


def _load() -> _Entries:
    """Read the blocklist, reparsing only when its contents change.

    Reading on every call (the file is tiny) means permission changes are
    noticed too, not just edits.
    """
    global _cache_key, _cache_entries
    path = blocklist_path()
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeDecodeError) as e:
            _cache_key = None
            raise BlocklistError(f"Blocklist {path} could not be read: {e}") from e

        key = (path, text)
        if key != _cache_key:
            try:
                entries = _parse(text)
            except BlocklistError as e:
                _cache_key = None
                raise BlocklistError(f"{path}: {e}") from e
            _cache_key, _cache_entries = key, entries
        return _cache_entries


def _matches(entries: _Entries, jid: Optional[str], resolver) -> bool:
    """Match a chat by its JID and, given a resolver, its LID/phone alias."""
    if entries.matches(jid):
        return True
    return resolver is not None and bool(jid) and any(entries.matches(a) for a in resolver.aliases(jid))


def is_blocked(jid: Optional[str], resolver=None) -> bool:
    """Whether a chat is blocked by its JID or its LID/phone alias."""
    return _matches(_load(), jid, resolver)


def blocked_jids(cursor, resolver=None) -> Set[str]:
    """Resolve blocklist entries to the concrete chat JIDs in the database.

    Looks at both the chats table and the distinct chat_jid values in the
    messages table, so messages whose chat row is missing are still excluded.
    With a resolver, each blocked chat's alias (LID or phone JID) is excluded
    too.
    """
    entries = _load()
    if not (entries.numbers or entries.jids):
        return set()

    result = set(entries.jids)
    cursor.execute("SELECT jid FROM chats UNION SELECT DISTINCT chat_jid FROM messages")
    for (jid,) in cursor.fetchall():
        if _matches(entries, jid, resolver):
            result.add(jid)
    if resolver is not None:
        for jid in list(result):
            result.update(resolver.aliases(jid))
    return result
