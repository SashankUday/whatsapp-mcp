import sqlite3
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any
import os.path
import secrets
import threading
import time
import requests
import json
import audio
from blocklist import is_blocked, blocked_jids
from names import NameResolver, split_jid, normalise_number

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
# The bridge only listens on 127.0.0.1. Never route its calls through a proxy.
WHATSAPP_API_BASE_URL = "http://127.0.0.1:8080/api"
# Shared secret the bridge creates on first start; every API call must send it
BRIDGE_TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'api-token')
_bridge = requests.Session()
_bridge.trust_env = False


class BridgeTokenError(requests.RequestException):
    """The bridge's API token is missing or unreadable."""


def _bridge_token() -> str:
    """Read the API token fresh each call, so a rotated token is picked up."""
    try:
        with open(BRIDGE_TOKEN_PATH) as f:
            token = f.read().strip()
    except OSError as e:
        raise BridgeTokenError(f"Bridge API token not readable ({e.strerror}); start the bridge once to create it")
    if not token:
        raise BridgeTokenError("Bridge API token is empty; delete it and restart the bridge")
    return token


def _bridge_post(path: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> requests.Response:
    """POST JSON to the bridge with its API token."""
    return _bridge.post(f"{WHATSAPP_API_BASE_URL}{path}", json=payload, timeout=timeout,
                        headers={"Authorization": f"Bearer {_bridge_token()}"})

def _not_in(column: str, jids) -> Tuple[str, list]:
    """SQL clause and params excluding the given chat JIDs."""
    if not jids:
        return "1=1", []
    return f"{column} NOT IN ({','.join('?' * len(jids))})", list(jids)

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    sender_name: Optional[str] = None
    read_state: Optional[str] = None  # "read", "unread" or "unknown"
    dismissed: bool = False  # dismissed locally (never sent to WhatsApp)

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None
    last_sender_name: Optional[str] = None
    unread_count: Optional[int] = None  # incoming unread messages not dismissed; None if not tracked
    marked_unread: Optional[bool] = None  # chat marked unread on another device

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str
    lid: Optional[str] = None

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def _resolver() -> NameResolver:
    """A name resolver for one tool call; it caches lookups for that call."""
    return NameResolver(MESSAGES_DB_PATH)

def get_sender_name(sender_jid: str, resolver: Optional[NameResolver] = None) -> str:
    """Human-readable name for a sender ID (phone JID, LID or bare number).

    Tries whatsmeow's contacts (via the LID map for LIDs), then 1:1 chat
    names, then falls back to the raw ID. Never raises.
    """
    try:
        return (resolver or _resolver()).sender_name(sender_jid)
    except Exception as e:
        print(f"Error getting sender name: {e}")
        return sender_jid

def _has_read_state(cursor) -> bool:
    """Whether the bridge has added read-state tracking to messages.db."""
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)").fetchall()}
    return {"read_state", "dismissed_at"} <= columns

def _require_read_state(cursor) -> None:
    if not _has_read_state(cursor):
        raise ValueError("Read-state tracking isn't available yet: rebuild and restart the WhatsApp bridge first.")

def _read_cols(cursor, table: str = "messages") -> str:
    """Read-state columns to append to a message SELECT, or placeholders before the bridge migrates."""
    if _has_read_state(cursor):
        return f"{table}.read_state, {table}.dismissed_at"
    return "'unknown', NULL"

def _chat_unread_info(cursor, jids) -> Dict[str, Tuple[int, bool]]:
    """Unread count (incoming, not dismissed) and marked-unread flag per chat JID."""
    jids = list(dict.fromkeys(jids))
    if not jids or not _has_read_state(cursor):
        return {}
    in_sql, params = _in("chat_jid", jids)
    counts = dict(cursor.execute(
        f"""SELECT chat_jid, COUNT(*) FROM messages
            WHERE {in_sql} AND is_from_me = 0 AND read_state = 'unread' AND dismissed_at IS NULL
            GROUP BY chat_jid""", params).fetchall())
    marked = dict(cursor.execute(
        f"SELECT chat_jid, marked_unread FROM chat_read_state WHERE {in_sql}", params).fetchall())
    return {j: (counts.get(j, 0), bool(marked.get(j, 0))) for j in jids}

def _message_from_row(row, resolver: NameResolver) -> Message:
    """Build a Message from (timestamp, sender, chat name, content, is_from_me, chat jid, id, media_type[, read_state, dismissed_at])."""
    message = Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=resolver.chat_display_name(row[5], row[2]),
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7]
    )
    message.sender_name = "Me" if message.is_from_me else get_sender_name(message.sender, resolver)
    if len(row) >= 10:
        message.read_state = row[8] or "unknown"
        message.dismissed = row[9] is not None
    return message

def _chat_from_row(row, resolver: NameResolver, unread: Optional[Dict[str, Tuple[int, bool]]] = None) -> Chat:
    """Build a Chat from (jid, name, last_message_time, last message, last sender, last_is_from_me)."""
    chat = Chat(
        jid=row[0],
        name=resolver.chat_display_name(row[0], row[1]),
        last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
        last_message=row[3],
        last_sender=row[4],
        last_is_from_me=row[5]
    )
    if chat.last_is_from_me:
        chat.last_sender_name = "Me"
    elif chat.last_sender:
        chat.last_sender_name = get_sender_name(chat.last_sender, resolver)
    if unread is not None and chat.jid in unread:
        chat.unread_count, chat.marked_unread = unread[chat.jid]
    return chat

def _chats_from_rows(cursor, rows, resolver: NameResolver) -> List[Chat]:
    unread = _chat_unread_info(cursor, [row[0] for row in rows])
    return [_chat_from_row(row, resolver, unread) for row in rows]

def _read_tag(message: Message) -> str:
    state = message.read_state or "unknown"
    return f"{state}, dismissed" if message.dismissed else state

def _person_jids(jid: str, resolver: NameResolver) -> List[str]:
    """A person's JID plus their LID or phone JID, for matching chats and senders."""
    return [jid] + resolver.aliases(jid)

def _in(column: str, values) -> Tuple[str, list]:
    """SQL clause and params matching any of the given values."""
    values = list(values)
    if not values:
        return "1=0", []
    return f"{column} IN ({','.join('?' * len(values))})", values

def format_message(message: Message, show_chat_info: bool = True, resolver: Optional[NameResolver] = None) -> None:
    """Print a single message with consistent formatting."""
    resolver = resolver or _resolver()
    output = ""
    
    chat_name = resolver.chat_display_name(message.chat_jid, message.chat_name)
    if show_chat_info and chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] [{_read_tag(message)}] Chat: {chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] [{_read_tag(message)}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender, resolver) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True, resolver: Optional[NameResolver] = None) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    resolver = resolver or _resolver()
    for message in messages:
        output += format_message(message, show_chat_info, resolver)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    unread_only: bool = False
) -> List[Message]:
    """Get messages matching the specified criteria with optional context.

    sender_phone_number may be a phone number, a phone JID or a LID; messages
    stored under any of that person's IDs match. unread_only keeps incoming
    messages that are unread and not dismissed.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        blocked = blocked_jids(cursor, resolver)
        
        # Build base query
        if unread_only:
            _require_read_state(cursor)
        query_parts = [f"SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, {_read_cols(cursor)} FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        excl_sql, excl_params = _not_in("messages.chat_jid", blocked)
        where_clauses = [excl_sql]
        params = list(excl_params)
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            sender_sql, sender_params = _in("messages.sender", resolver.sender_ids(sender_phone_number))
            where_clauses.append(sender_sql)
            params.extend(sender_params)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")

        if unread_only:
            where_clauses.append("messages.is_from_me = 0 AND messages.read_state = 'unread' AND messages.dismissed_at IS NULL")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        result = [_message_from_row(msg, resolver) for msg in cursor.fetchall()]
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = _get_message_context(cursor, msg.id, context_before, context_after, blocked, resolver)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True, resolver=resolver)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True, resolver=resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def _get_message_context(cursor, message_id: str, before: int, after: int, blocked, resolver: NameResolver) -> MessageContext:
    """Context around a message, using an open cursor and a precomputed blocked set."""
    read_cols = _read_cols(cursor)
    # Get the target message first (blocked chats look like a missing message)
    excl_sql, excl_params = _not_in("messages.chat_jid", blocked)
    cursor.execute(f"""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, {read_cols}
        FROM messages
        JOIN chats ON messages.chat_jid = chats.jid
        WHERE messages.id = ? AND {excl_sql}
    """, (message_id, *excl_params))
    msg_data = cursor.fetchone()
    
    if not msg_data:
        raise ValueError(f"Message with ID {message_id} not found")
        
    target_message = _message_from_row(msg_data, resolver)
    
    # Get messages before
    cursor.execute(f"""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, {read_cols}
        FROM messages
        JOIN chats ON messages.chat_jid = chats.jid
        WHERE messages.chat_jid = ? AND messages.timestamp < ?
        ORDER BY messages.timestamp DESC
        LIMIT ?
    """, (msg_data[5], msg_data[0], before))
    before_messages = [_message_from_row(msg, resolver) for msg in cursor.fetchall()]
    
    # Get messages after
    cursor.execute(f"""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, {read_cols}
        FROM messages
        JOIN chats ON messages.chat_jid = chats.jid
        WHERE messages.chat_jid = ? AND messages.timestamp > ?
        ORDER BY messages.timestamp ASC
        LIMIT ?
    """, (msg_data[5], msg_data[0], after))
    after_messages = [_message_from_row(msg, resolver) for msg in cursor.fetchall()]
    
    return MessageContext(
        message=target_message,
        before=before_messages,
        after=after_messages
    )


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        return _get_message_context(cursor, message_id, before, after, blocked_jids(cursor, resolver), resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria.

    A query also matches 1:1 chats whose contact name contains it, even when
    the stored chat name is only a number.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        excl_sql, excl_params = _not_in("chats.jid", blocked_jids(cursor, resolver))
        where_clauses = [excl_sql]
        params = list(excl_params)
        
        if query:
            contact_jids = [j for pn, lid, _ in resolver.search(query) for j in (pn, lid) if j]
            contact_sql, contact_params = _in("chats.jid", contact_jids)
            where_clauses.append(f"(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ? OR {contact_sql})")
            params.extend([f"%{query}%", f"%{query}%", *contact_params])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        return _chats_from_rows(cursor, cursor.fetchall(), resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number.

    Searches 1:1 chats and WhatsApp's contact store (address-book and
    self-set names), so people you only share groups with are found too.
    Each result has the phone JID as jid where known, and the LID if known.
    Blocked contacts are left out.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        blocked = blocked_jids(cursor, resolver)
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        excl_sql, excl_params = _not_in("jid", blocked)
        cursor.execute(f"""
            SELECT DISTINCT 
                jid,
                name
            FROM chats
            WHERE 
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
                AND {excl_sql}
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern, *excl_params))
        
        people = [resolver.identity(jid) + (resolver.chat_display_name(jid, name),) for jid, name in cursor.fetchall()]
        people += resolver.search(query)
        
        result = []
        seen = set()
        for pn, lid, name in people:
            key = pn or lid
            if not key or key in seen:
                continue
            seen.add(key)
            if any(j in blocked or is_blocked(j, resolver) for j in (pn, lid) if j):
                continue
            result.append(Contact(
                phone_number=split_jid(pn)[0] if pn else "",
                name=name,
                jid=pn or lid,
                lid=lid
            ))
            if len(result) >= 50:
                break
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for (phone JID or LID)
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        
        excl_sql, excl_params = _not_in("c.jid", blocked_jids(cursor, resolver))
        sender_sql, sender_params = _in("m.sender", resolver.sender_ids(jid))
        chat_sql, chat_params = _in("c.jid", _person_jids(jid, resolver))
        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE ({sender_sql} OR {chat_sql}) AND {excl_sql}
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (*sender_params, *chat_params, *excl_params, limit, page * limit))
        
        return _chats_from_rows(cursor, cursor.fetchall(), resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact (phone JID or LID)."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        
        excl_sql, excl_params = _not_in("c.jid", blocked_jids(cursor, resolver))
        sender_sql, sender_params = _in("m.sender", resolver.sender_ids(jid))
        chat_sql, chat_params = _in("c.jid", _person_jids(jid, resolver))
        cursor.execute(f"""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type,
                {_read_cols(cursor, "m")}
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE ({sender_sql} OR {chat_sql}) AND {excl_sql}
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (*sender_params, *chat_params, *excl_params))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        return format_message(_message_from_row(msg_data, resolver), resolver=resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data or is_blocked(chat_data[0], resolver):
            return None
            
        return _chats_from_rows(cursor, [chat_data], resolver)[0]
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number (also finds 1:1 chats keyed by LID)."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        
        lid_jids = [lid for _, lid in [resolver.identity(normalise_number(sender_phone_number))] if lid]
        lid_sql, lid_params = _in("c.jid", lid_jids)
        excl_sql, excl_params = _not_in("c.jid", blocked_jids(cursor, resolver))
        cursor.execute(f"""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE (c.jid LIKE ? OR {lid_sql}) AND c.jid NOT LIKE '%@g.us' AND {excl_sql}
            LIMIT 1
        """, (f"%{sender_phone_number}%", *lid_params, *excl_params))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return _chats_from_rows(cursor, [chat_data], resolver)[0]
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def _visible_chat(cursor, chat_jid: str, resolver: NameResolver) -> Optional[Tuple[str, Optional[str]]]:
    """(jid, display name) of a chat the tools may touch; None if it's missing or blocked.

    Blocked and missing chats look the same, so a refusal doesn't reveal that a
    blocked chat exists.
    """
    if not chat_jid or is_blocked(chat_jid, resolver):
        return None
    row = cursor.execute("SELECT jid, name FROM chats WHERE jid = ?", (chat_jid,)).fetchone()
    if not row:
        return None
    return row[0], resolver.chat_display_name(row[0], row[1])

def _chat_forms(chat_jid: str, resolver: NameResolver) -> List[str]:
    """A chat's JID plus, for 1:1 chats, its LID/phone alias (messages may be stored under either)."""
    return [chat_jid] + (resolver.aliases(chat_jid) if not chat_jid.endswith("@g.us") else [])


def list_unread_messages(limit_per_chat: int = 50, include_marked_unread: bool = True) -> List[Dict[str, Any]]:
    """Unread incoming messages across chats, grouped by chat, oldest first.

    Only messages known to be unread count: messages stored before read-state
    tracking existed are "unknown" and left out, as are dismissed messages and
    blocked chats. Chats marked unread on another device appear even when no
    stored message is unread.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        _require_read_state(cursor)
        resolver = _resolver()
        blocked = blocked_jids(cursor, resolver)
        excl_sql, excl_params = _not_in("m.chat_jid", blocked)
        cursor.execute(f"""
            SELECT m.timestamp, m.sender, c.name, m.content, m.is_from_me, c.jid, m.id, m.media_type, m.read_state, m.dismissed_at
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.is_from_me = 0 AND m.read_state = 'unread' AND m.dismissed_at IS NULL AND {excl_sql}
            ORDER BY CAST(strftime('%s', m.timestamp) AS INTEGER) ASC
        """, excl_params)

        groups: Dict[str, Dict[str, Any]] = {}
        for row in cursor.fetchall():
            message = _message_from_row(row, resolver)
            group = groups.setdefault(message.chat_jid, {
                "chat_jid": message.chat_jid,
                "chat_name": message.chat_name,
                "unread_count": 0,
                "marked_unread": False,
                "messages": [],
            })
            group["unread_count"] += 1
            if len(group["messages"]) < limit_per_chat:
                group["messages"].append({
                    "id": message.id,
                    "timestamp": message.timestamp.isoformat(),
                    "sender": message.sender,
                    "sender_name": message.sender_name,
                    "content": message.content,
                    "media_type": message.media_type,
                })

        excl_sql, excl_params = _not_in("s.chat_jid", blocked)
        for jid, name in cursor.execute(f"""
            SELECT s.chat_jid, c.name FROM chat_read_state s JOIN chats c ON c.jid = s.chat_jid
            WHERE s.marked_unread = 1 AND {excl_sql}
        """, excl_params).fetchall():
            if jid in groups:
                groups[jid]["marked_unread"] = True
            elif include_marked_unread:
                groups[jid] = {"chat_jid": jid, "chat_name": resolver.chat_display_name(jid, name),
                               "unread_count": 0, "marked_unread": True, "messages": []}

        for group in groups.values():
            group["truncated"] = group["unread_count"] > len(group["messages"])
        return list(groups.values())

    finally:
        if 'conn' in locals():
            conn.close()


def dismiss_messages(chat_jid: str, message_ids: Optional[List[str]] = None, undo: bool = False) -> Dict[str, Any]:
    """Dismiss messages locally so they drop out of unread listings, or undo that.

    Only a local flag in messages.db changes: nothing is sent to WhatsApp and
    nobody sees anything. Without message_ids, dismisses every unread incoming
    message in the chat (or, with undo, restores every dismissed one).
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=10)
        cursor = conn.cursor()
        _require_read_state(cursor)
        resolver = _resolver()
        chat = _visible_chat(cursor, chat_jid, resolver)
        if chat is None:
            raise ValueError(f"Chat not found: {chat_jid}")
        forms_sql, forms_params = _in("chat_jid", _chat_forms(chat[0], resolver))
        ids_sql, ids_params = _in("id", message_ids) if message_ids else ("1=1", [])

        if undo:
            cursor.execute(f"""UPDATE messages SET dismissed_at = NULL
                WHERE {forms_sql} AND {ids_sql} AND dismissed_at IS NOT NULL""", (*forms_params, *ids_params))
        else:
            unread_only = "1=1" if message_ids else "read_state = 'unread'"
            cursor.execute(f"""UPDATE messages SET dismissed_at = ?
                WHERE {forms_sql} AND {ids_sql} AND {unread_only} AND is_from_me = 0 AND dismissed_at IS NULL""",
                (int(time.time()), *forms_params, *ids_params))
        changed = cursor.rowcount
        conn.commit()
        return {
            "chat_jid": chat[0],
            "chat_name": chat[1],
            ("restored" if undo else "dismissed"): changed,
            "note": "Local only: nothing was sent to WhatsApp. Undo with undo=true.",
        }

    finally:
        if 'conn' in locals():
            conn.close()


# Pending mark-read confirmations: token -> (expiry, chat list, {chat_jid: [message ids]})
_MARK_READ_TOKEN_TTL = 300
_mark_read_tokens: Dict[str, Tuple[float, Tuple[str, ...], Dict[str, List[str]]]] = {}
_mark_read_lock = threading.Lock()

_BLUE_TICK_WARNING = ("This sends read receipts to WhatsApp. Your phone and other devices will show these "
                      "messages as read, and if you have read receipts switched on, the senders may see blue ticks. "
                      "It can't be undone.")


def mark_read_on_whatsapp(chat_jids: List[str], confirmation_token: Optional[str] = None,
                          confirm_multiple_chats: bool = False) -> Dict[str, Any]:
    """Mark unread messages read on WhatsApp itself, in two steps.

    Without a token this is a dry run: it reports which chats and how many
    messages per chat would be marked, and returns a short-lived single-use
    confirmation token. Only a second call with that token and the same chat
    list sends anything. Blocked chats are refused, and more than one chat
    needs confirm_multiple_chats=True.
    """
    chats_requested = tuple(dict.fromkeys(j.strip() for j in (chat_jids or []) if j and j.strip()))
    if not chats_requested:
        raise ValueError("chat_jids must list at least one chat")
    if len(chats_requested) > 1 and not confirm_multiple_chats:
        raise ValueError("Only one chat per call. To mark several, list them explicitly and set confirm_multiple_chats=true.")

    if confirmation_token:
        with _mark_read_lock:
            now = time.time()
            for token, (expiry, _, _) in list(_mark_read_tokens.items()):
                if expiry < now:
                    del _mark_read_tokens[token]
            pending = _mark_read_tokens.pop(confirmation_token, None)
        if pending is None:
            raise ValueError("Invalid or expired confirmation token. Run again without a token for a new dry run.")
        _, token_chats, plan = pending
        if token_chats != chats_requested:
            raise ValueError("The chat list doesn't match the dry run this token was issued for. Run a new dry run.")
        return _send_mark_read(plan)

    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        _require_read_state(cursor)
        resolver = _resolver()
        plan: Dict[str, List[str]] = {}
        summary = []
        for requested in chats_requested:
            chat = _visible_chat(cursor, requested, resolver)
            if chat is None:
                raise ValueError(f"Chat not found: {requested}")
            forms_sql, forms_params = _in("chat_jid", _chat_forms(chat[0], resolver))
            rows = cursor.execute(f"""
                SELECT chat_jid, id, timestamp FROM messages
                WHERE {forms_sql} AND is_from_me = 0 AND read_state = 'unread'
                ORDER BY CAST(strftime('%s', timestamp) AS INTEGER)""", forms_params).fetchall()
            for stored_chat, message_id, _ in rows:
                plan.setdefault(stored_chat, []).append(message_id)
            summary.append({
                "chat_jid": chat[0],
                "chat_name": chat[1],
                "messages_to_mark": len(rows),
                "oldest": rows[0][2] if rows else None,
                "newest": rows[-1][2] if rows else None,
            })
    finally:
        if 'conn' in locals():
            conn.close()

    total = sum(c["messages_to_mark"] for c in summary)
    result: Dict[str, Any] = {"dry_run": True, "chats": summary, "total_messages": total}
    if total == 0:
        result["message"] = "Nothing to mark: no unread messages in these chats."
        return result
    token = secrets.token_urlsafe(16)
    with _mark_read_lock:
        _mark_read_tokens[token] = (time.time() + _MARK_READ_TOKEN_TTL, chats_requested, plan)
    result.update({
        "warning": _BLUE_TICK_WARNING,
        "confirmation_token": token,
        "expires_in_seconds": _MARK_READ_TOKEN_TTL,
        "next_step": "Only if the user explicitly confirms, call again with the same chat_jids and this confirmation_token.",
    })
    return result


def _send_mark_read(plan: Dict[str, List[str]]) -> Dict[str, Any]:
    """Ask the bridge to send read receipts for a confirmed plan."""
    resolver = _resolver()
    results = []
    for chat_jid, message_ids in plan.items():
        # The blocklist may have changed since the dry run
        if is_blocked(chat_jid, resolver):
            results.append({"chat_jid": chat_jid, "success": False, "message": "Chat not found"})
            continue
        try:
            response = _bridge_post("/mark-read", {"chat_jid": chat_jid, "message_ids": message_ids}, timeout=60)
            body = response.json()
            results.append({"chat_jid": chat_jid, "success": bool(body.get("success")),
                            "marked": body.get("marked", 0), "message": body.get("message", "")})
        except (requests.RequestException, ValueError) as e:
            results.append({"chat_jid": chat_jid, "success": False, "message": f"Bridge request failed: {e}"})
    return {"dry_run": False, "results": results,
            "total_marked": sum(r.get("marked", 0) for r in results if r["success"])}


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        path = "/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = _bridge_post(path, payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        path = "/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = _bridge_post(path, payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        path = "/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = _bridge_post(path, payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def _chat_is_blocked(chat_jid: str) -> bool:
    """Check a chat JID, and its LID/phone alias, against the blocklist."""
    return is_blocked(chat_jid, _resolver())

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    if _chat_is_blocked(chat_jid):
        print("Download failed: chat is blocked")
        return None

    try:
        path = "/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = _bridge_post(path, payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
