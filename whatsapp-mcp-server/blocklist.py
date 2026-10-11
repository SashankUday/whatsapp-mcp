"""Chat blocklist shared with the Go bridge.

The file lives at <repo root>/blocklist.txt unless WHATSAPP_MCP_BLOCKLIST is set.
One entry per line, '#' starts a comment. An entry is a phone number, an exact
JID (contains '@'), or a chat name ("name: <chat name>" or bare text,
case-insensitive).

The file is re-read on every lookup and reparsed when it changes, so edits
apply without a restart. A missing file blocks nothing; a file that exists but can't be read raises
BlocklistError so read tools fail instead of returning unfiltered data.
"""

import os
import threading
from dataclasses import dataclass, field
from typing import Optional, Set

DEFAULT_BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'blocklist.txt')

_PHONE_SEPARATORS = set(" \t+-()[]")


class BlocklistError(Exception):
    """The blocklist file exists but could not be read."""


@dataclass
class _Entries:
    phones: Set[str] = field(default_factory=set)
    jids: Set[str] = field(default_factory=set)
    names: Set[str] = field(default_factory=set)

    def matches(self, jid: Optional[str], name: Optional[str]) -> bool:
        if jid:
            if jid.lower() in self.jids:
                return True
            if _jid_user(jid) in self.phones:
                return True
        if name and name.strip().lower() in self.names:
            return True
        return False


_lock = threading.Lock()
_cache_key = None
_cache_entries = _Entries()


def blocklist_path() -> str:
    return os.environ.get("WHATSAPP_MCP_BLOCKLIST") or DEFAULT_BLOCKLIST_PATH


def _normalize_phone(s: str) -> str:
    """Strip spaces, '+', '-' and brackets; return '' unless only digits remain."""
    digits = "".join(c for c in s if c not in _PHONE_SEPARATORS)
    return digits if digits.isdigit() and digits.isascii() else ""


def _jid_user(jid: str) -> str:
    """User part of a JID with any ':device' suffix removed."""
    return jid.split("@", 1)[0].split(":", 1)[0]


def _parse(text: str) -> _Entries:
    entries = _Entries()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line[:5].lower() == "name:":
            name = line[5:].strip()
            if name:
                entries.names.add(name.lower())
        elif "@" in line:
            entries.jids.add(line.lower())
        elif _normalize_phone(line):
            entries.phones.add(_normalize_phone(line))
        else:
            entries.names.add(line.lower())
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
            _cache_key, _cache_entries = key, _parse(text)
        return _cache_entries


def is_blocked(jid: Optional[str], name: Optional[str] = None) -> bool:
    """Whether a chat is blocked by JID, phone number or chat name."""
    return _load().matches(jid, name)


def blocked_jids(cursor) -> Set[str]:
    """Resolve blocklist entries to the concrete chat JIDs in the database.

    Looks at both the chats table (JID and name) and the distinct chat_jid
    values in the messages table, so messages whose chat row is missing are
    still excluded.
    """
    entries = _load()
    if not (entries.phones or entries.jids or entries.names):
        return set()

    result = set(entries.jids)
    cursor.execute("SELECT jid, name FROM chats")
    for jid, name in cursor.fetchall():
        if entries.matches(jid, name):
            result.add(jid)
    cursor.execute("SELECT DISTINCT chat_jid FROM messages")
    for (jid,) in cursor.fetchall():
        if entries.matches(jid, None):
            result.add(jid)
    return result
