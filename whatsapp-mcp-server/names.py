"""Resolve WhatsApp IDs (phone JIDs, LIDs, bare numbers) to display names.

Group participants are now identified by LIDs ("105506358227070@lid") rather
than phone numbers. The bridge's whatsmeow store (whatsapp.db) maps LIDs to
phone numbers (whatsmeow_lid_map, bare user parts on both sides) and keeps
contact names (whatsmeow_contacts, keyed by full JID). This module reads it
read-only.

A NameResolver loads those tables once and caches every lookup, so create one
per tool call and reuse it for all the messages in that call. Lookups never
raise: on any error they fall back to the raw ID.
"""

import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple
from urllib.request import pathname2url

WHATSAPP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')

PN_SERVER = "s.whatsapp.net"
LID_SERVER = "lid"


def split_jid(jid: Optional[str]) -> Tuple[str, str]:
    """User and server parts of a JID, without any ':device' suffix.

    Bare IDs (as the bridge stores most senders) have an empty server.
    """
    user, _, server = (jid or "").strip().partition("@")
    return user.split(":", 1)[0], server


def normalise_number(value: str) -> str:
    """Strip spaces, '+', '-' and brackets from a phone number."""
    return re.sub(r"[\s+\-()\[\]]", "", value or "")


def connect_read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{pathname2url(os.path.abspath(path))}?mode=ro", uri=True)


class NameResolver:
    def __init__(self, messages_db_path: Optional[str] = None, whatsapp_db_path: Optional[str] = None):
        self.messages_db_path = messages_db_path
        self.whatsapp_db_path = whatsapp_db_path or WHATSAPP_DB_PATH
        self._loaded = False
        self._lid_to_pn: Dict[str, str] = {}
        self._pn_to_lid: Dict[str, str] = {}
        # JID -> (full_name, first_name, push_name, business_name)
        self._contacts: Dict[str, Tuple[str, str, str, str]] = {}
        self._sender_cache: Dict[str, str] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            conn = connect_read_only(self.whatsapp_db_path)
            try:
                for lid, pn in conn.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                    lid, pn = split_jid(lid)[0], split_jid(pn)[0]
                    if lid and pn:
                        self._lid_to_pn[lid] = pn
                        self._pn_to_lid[pn] = lid
                for jid, full, first, push, business in conn.execute(
                    "SELECT their_jid, full_name, first_name, push_name, business_name FROM whatsmeow_contacts"
                ):
                    user, server = split_jid(jid)
                    if user:
                        self._contacts[f"{user}@{server}"] = tuple(
                            (n or "").strip() for n in (full, first, push, business)
                        )
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"Could not read contact names from whatsapp.db: {e}")

    def identity(self, jid: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """(phone JID, LID JID) for a person, either of which may be None.

        Accepts phone JIDs, LIDs, and bare numbers, which may be either; an
        unknown bare number yields both candidate JIDs. Group and other
        non-person JIDs give (None, None).
        """
        self._load()
        user, server = split_jid(jid)
        if not user:
            return None, None
        if server == LID_SERVER:
            pn = self._lid_to_pn.get(user)
            return (f"{pn}@{PN_SERVER}" if pn else None), f"{user}@{LID_SERVER}"
        if server in (PN_SERVER, "c.us"):
            lid = self._pn_to_lid.get(user)
            return f"{user}@{PN_SERVER}", (f"{lid}@{LID_SERVER}" if lid else None)
        if server == "" and user.isdigit():
            if user in self._lid_to_pn:
                return f"{self._lid_to_pn[user]}@{PN_SERVER}", f"{user}@{LID_SERVER}"
            if user in self._pn_to_lid:
                return f"{user}@{PN_SERVER}", f"{self._pn_to_lid[user]}@{LID_SERVER}"
            return f"{user}@{PN_SERVER}", f"{user}@{LID_SERVER}"
        return None, None

    def aliases(self, jid: Optional[str]) -> List[str]:
        """Other JIDs for the same person: the LID for a phone JID and vice versa."""
        user, server = split_jid(jid)
        own = f"{user}@{server}"
        return [j for j in self.identity(jid) if j and j != own]

    def sender_ids(self, sender: str) -> List[str]:
        """Every form a sender may be stored as: phone/LID JIDs and their bare numbers."""
        sender = (sender or "").strip()
        if "@" not in sender:
            sender = normalise_number(sender)
        ids = {sender}
        for jid in self.identity(sender):
            if jid:
                ids.add(jid)
                ids.add(split_jid(jid)[0])
        ids.discard("")
        return sorted(ids)

    def contact_names(self, jid: Optional[str]) -> List[str]:
        """Contact names for a person across their phone JID and LID, best first.

        Order: address-book full name, first name, self-set push name,
        business name.
        """
        records = [self._contacts[j] for j in self.identity(jid) if j and j in self._contacts]
        names: List[str] = []
        for field in range(4):
            for record in records:
                if record[field] and record[field] not in names:
                    names.append(record[field])
        return names

    def sender_name(self, sender: Optional[str]) -> str:
        """Human-readable name for a message sender, or the raw ID if unknown."""
        if not sender:
            return sender or ""
        if sender not in self._sender_cache:
            try:
                names = self.contact_names(sender)
                self._sender_cache[sender] = names[0] if names else (self._chat_name_fallback(sender) or sender)
            except Exception as e:
                print(f"Error resolving sender name for {sender}: {e}")
                self._sender_cache[sender] = sender
        return self._sender_cache[sender]

    def _chat_name_fallback(self, sender: str) -> Optional[str]:
        """Name of a 1:1 chat with this person, from messages.db."""
        if not self.messages_db_path:
            return None
        user, server = split_jid(sender)
        candidates = [j for j in self.identity(sender) if j] or ([sender] if server else [])
        try:
            conn = connect_read_only(self.messages_db_path)
            try:
                for jid in candidates:
                    row = conn.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
                    if row and self._is_real_name(row[0], jid):
                        return row[0]
                # As before: a 1:1 chat whose JID contains the number
                if user:
                    row = conn.execute(
                        "SELECT name, jid FROM chats WHERE jid LIKE ? AND jid NOT LIKE '%@g.us' LIMIT 1",
                        (f"%{user}%",),
                    ).fetchone()
                    if row and self._is_real_name(row[0], row[1]):
                        return row[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"Database error while getting sender name: {e}")
        return None

    @staticmethod
    def _is_real_name(name: Optional[str], jid: str) -> bool:
        """A stored chat name that is more than just the JID's number."""
        name = (name or "").strip()
        return bool(name) and name not in (split_jid(jid)[0], jid)

    def chat_display_name(self, chat_jid: Optional[str], stored_name: Optional[str]) -> Optional[str]:
        """The stored chat name, or the contact's name for 1:1 chats only named by number."""
        try:
            if self._is_real_name(stored_name, chat_jid or "") or split_jid(chat_jid)[1] == "g.us":
                return stored_name
            names = self.contact_names(chat_jid)
            return names[0] if names else stored_name
        except Exception as e:
            print(f"Error resolving chat name for {chat_jid}: {e}")
            return stored_name

    def search(self, query: str) -> List[Tuple[Optional[str], Optional[str], Optional[str]]]:
        """People whose contact name or number contains query.

        Returns (phone JID, LID JID, best name) once per person.
        """
        self._load()
        q = (query or "").strip().lower()
        q_number = normalise_number(q) if normalise_number(q).isdigit() else None
        seen = set()
        results = []
        for jid in self._contacts:
            if split_jid(jid)[1] not in (PN_SERVER, LID_SERVER):
                continue
            pn, lid = self.identity(jid)
            key = pn or lid
            if key in seen:
                continue
            seen.add(key)
            names = self.contact_names(jid)
            ids = " ".join(j for j in (pn, lid) if j)
            if q and not (
                any(q in n.lower() for n in names)
                or q in ids
                or (q_number and q_number in ids)
            ):
                continue
            results.append((pn, lid, names[0] if names else None))
        return results
