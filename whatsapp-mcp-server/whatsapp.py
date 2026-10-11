import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio
from blocklist import is_blocked, blocked_jids
from names import NameResolver, split_jid, normalise_number

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

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

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None
    last_sender_name: Optional[str] = None

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

def _message_from_row(row, resolver: NameResolver) -> Message:
    """Build a Message from (timestamp, sender, chat name, content, is_from_me, chat jid, id, media_type)."""
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
    return message

def _chat_from_row(row, resolver: NameResolver) -> Chat:
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
    return chat

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
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
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
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context.

    sender_phone_number may be a phone number, a phone JID or a LID; messages
    stored under any of that person's IDs match.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        resolver = _resolver()
        blocked = blocked_jids(cursor, resolver)
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
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
    # Get the target message first (blocked chats look like a missing message)
    excl_sql, excl_params = _not_in("messages.chat_jid", blocked)
    cursor.execute(f"""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
        FROM messages
        JOIN chats ON messages.chat_jid = chats.jid
        WHERE messages.id = ? AND {excl_sql}
    """, (message_id, *excl_params))
    msg_data = cursor.fetchone()
    
    if not msg_data:
        raise ValueError(f"Message with ID {message_id} not found")
        
    target_message = _message_from_row(msg_data, resolver)
    
    # Get messages before
    cursor.execute("""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
        FROM messages
        JOIN chats ON messages.chat_jid = chats.jid
        WHERE messages.chat_jid = ? AND messages.timestamp < ?
        ORDER BY messages.timestamp DESC
        LIMIT ?
    """, (msg_data[5], msg_data[0], before))
    before_messages = [_message_from_row(msg, resolver) for msg in cursor.fetchall()]
    
    # Get messages after
    cursor.execute("""
        SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
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
        return [_chat_from_row(chat_data, resolver) for chat_data in cursor.fetchall()]
        
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
        
        return [_chat_from_row(chat_data, resolver) for chat_data in cursor.fetchall()]
        
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
                m.media_type
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
            
        return _chat_from_row(chat_data, resolver)
        
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
            
        return _chat_from_row(chat_data, resolver)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
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
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
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
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
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
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
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
