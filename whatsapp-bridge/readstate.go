package main

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Read-state tracking.
//
// Each stored message has a read_state of "read", "unread" or "unknown", with
// read_source saying where it came from:
//
//	sent                 a message you sent
//	live                 received while the bridge was running, not read yet
//	receipt              you read it on another device (read / read-self receipt)
//	chat_marked_read     you marked the whole chat as read on another device
//	history_sync         approximated from a history sync's per-chat unread count
//	marked_on_whatsapp   marked read on WhatsApp through the MCP server
//
// Messages stored before read-state tracking existed are "unknown". The MCP
// server's local "dismissed" flag (dismissed_at) is separate and never sent to
// WhatsApp. Blocked chats are skipped entirely, so nothing about their activity
// is recorded.

const readStateSchemaVersion = 1

// migrateReadState adds the read-state columns and tables. It backs up
// messages.db (inside the git-ignored store/) before changing anything.
func migrateReadState(db *sql.DB, dbPath string) error {
	var version int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return fmt.Errorf("failed to read schema version: %v", err)
	}
	if version >= readStateSchemaVersion {
		return nil
	}

	var count int
	db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&count)
	if count > 0 {
		backup := fmt.Sprintf("%s.pre-read-state-%s.bak", dbPath, time.Now().Format("20060102-150405"))
		if _, err := db.Exec("VACUUM INTO ?", backup); err != nil {
			return fmt.Errorf("failed to back up %s before migrating: %v", dbPath, err)
		}
		fmt.Printf("Backed up %s to %s before adding read-state tracking\n", dbPath, backup)
	}

	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	existing := map[string]bool{}
	rows, err := tx.Query("SELECT name FROM pragma_table_info('messages')")
	if err != nil {
		return err
	}
	for rows.Next() {
		var name string
		rows.Scan(&name)
		existing[name] = true
	}
	rows.Close()

	for _, col := range []struct{ name, def string }{
		{"read_state", "TEXT NOT NULL DEFAULT 'unknown'"},
		{"read_source", "TEXT"},
		{"read_at", "INTEGER"},
		{"dismissed_at", "INTEGER"},
	} {
		if !existing[col.name] {
			if _, err := tx.Exec(fmt.Sprintf("ALTER TABLE messages ADD COLUMN %s %s", col.name, col.def)); err != nil {
				return fmt.Errorf("failed to add column %s: %v", col.name, err)
			}
		}
	}
	if _, err := tx.Exec(`
		UPDATE messages SET read_state = 'read', read_source = 'sent' WHERE is_from_me = 1 AND read_state = 'unknown';

		CREATE TABLE IF NOT EXISTS self_read_receipts (
			chat_jid   TEXT,
			message_id TEXT,
			read_at    INTEGER,
			PRIMARY KEY (chat_jid, message_id)
		);

		CREATE TABLE IF NOT EXISTS chat_read_state (
			chat_jid             TEXT PRIMARY KEY,
			read_up_to           INTEGER,
			marked_unread        INTEGER NOT NULL DEFAULT 0,
			history_unread_count INTEGER,
			updated_at           INTEGER
		);

		CREATE INDEX IF NOT EXISTS idx_messages_read_state ON messages (chat_jid, read_state);
	`); err != nil {
		return fmt.Errorf("failed to add read-state tables: %v", err)
	}
	if _, err := tx.Exec(fmt.Sprintf("PRAGMA user_version = %d", readStateSchemaVersion)); err != nil {
		return err
	}
	return tx.Commit()
}

// initialReadState decides the read state of a message being stored for the
// first time. Receipts or chat-level marks that arrived before the message
// itself are applied here.
func (store *MessageStore) initialReadState(id, chatJID string, timestamp time.Time, isFromMe, live bool) (string, string, *int64) {
	if isFromMe {
		return "read", "sent", nil
	}
	var readAt int64
	if err := store.db.QueryRow(
		"SELECT read_at FROM self_read_receipts WHERE chat_jid = ? AND message_id = ?", chatJID, id,
	).Scan(&readAt); err == nil {
		return "read", "receipt", &readAt
	}
	var readUpTo sql.NullInt64
	if err := store.db.QueryRow(
		"SELECT read_up_to FROM chat_read_state WHERE chat_jid = ?", chatJID,
	).Scan(&readUpTo); err == nil && readUpTo.Valid && timestamp.Unix() <= readUpTo.Int64 {
		return "read", "chat_marked_read", &readUpTo.Int64
	}
	if live {
		return "unread", "live", nil
	}
	return "unknown", "", nil
}

// chatJIDStrings returns a chat's JID and, for 1:1 chats, its LID/phone alias,
// since a chat may be stored under either form.
func chatJIDStrings(client *whatsmeow.Client, chat types.JID) []string {
	var jids []string
	for _, j := range contactJIDs(client, chat.ToNonAD()) {
		jids = append(jids, j.String())
	}
	return jids
}

func placeholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

func stringArgs(values ...[]string) []interface{} {
	var args []interface{}
	for _, vs := range values {
		for _, v := range vs {
			args = append(args, v)
		}
	}
	return args
}

// handleReceipt records messages you read on another device. Receipts from
// other people (them reading your messages) are ignored.
func handleReceipt(client *whatsmeow.Client, store *MessageStore, receipt *events.Receipt, logger waLog.Logger) {
	if !receipt.IsFromMe || len(receipt.MessageIDs) == 0 {
		return
	}
	switch receipt.Type {
	case types.ReceiptTypeRead, types.ReceiptTypeReadSelf, types.ReceiptTypePlayed, types.ReceiptTypePlayedSelf:
	default:
		return
	}
	if chatBlockedWithAliases(client, receipt.Chat, messageChatAliases(receipt.MessageSource)...) {
		return
	}
	chats := chatJIDStrings(client, receipt.Chat)
	readAt := receipt.Timestamp.Unix()
	ids := []string(receipt.MessageIDs)

	tx, err := store.db.Begin()
	if err != nil {
		logger.Warnf("Failed to record read receipt: %v", err)
		return
	}
	defer tx.Rollback()
	for _, chat := range chats {
		for _, id := range ids {
			tx.Exec("INSERT OR REPLACE INTO self_read_receipts (chat_jid, message_id, read_at) VALUES (?, ?, ?)", chat, id, readAt)
		}
	}
	res, err := tx.Exec(
		fmt.Sprintf(`UPDATE messages SET read_state = 'read', read_source = 'receipt', read_at = ?
			WHERE chat_jid IN (%s) AND id IN (%s) AND is_from_me = 0 AND read_state != 'read'`,
			placeholders(len(chats)), placeholders(len(ids))),
		append([]interface{}{readAt}, stringArgs(chats, ids)...)...,
	)
	if err != nil {
		logger.Warnf("Failed to mark messages read: %v", err)
		return
	}
	// Reading in a chat clears a "marked unread" flag on it
	tx.Exec(fmt.Sprintf("UPDATE chat_read_state SET marked_unread = 0, updated_at = ? WHERE chat_jid IN (%s)", placeholders(len(chats))),
		append([]interface{}{time.Now().Unix()}, stringArgs(chats)...)...)
	if err := tx.Commit(); err != nil {
		logger.Warnf("Failed to record read receipt: %v", err)
		return
	}
	n, _ := res.RowsAffected()
	fmt.Printf("Read on another device: %d message(s) in %s now read (%s receipt, %d ID(s))\n", n, receipt.Chat, receipt.Type, len(ids))
}

// handleMarkChatAsRead applies a whole chat being marked read or unread on
// another device.
func handleMarkChatAsRead(client *whatsmeow.Client, store *MessageStore, evt *events.MarkChatAsRead, logger waLog.Logger) {
	if chatBlocked(client, evt.JID) {
		return
	}
	chats := chatJIDStrings(client, evt.JID)
	now := time.Now().Unix()
	read := evt.Action.GetRead()

	if !read {
		for _, chat := range chats {
			if _, err := store.db.Exec(`INSERT INTO chat_read_state (chat_jid, marked_unread, updated_at) VALUES (?, 1, ?)
				ON CONFLICT(chat_jid) DO UPDATE SET marked_unread = 1, updated_at = excluded.updated_at`, chat, now); err != nil {
				logger.Warnf("Failed to record chat marked unread: %v", err)
			}
		}
		fmt.Printf("Chat %s marked unread on another device\n", evt.JID)
		return
	}

	// Everything up to the time of marking counts as read
	cutoff := evt.Timestamp.Unix()
	if last := evt.Action.GetMessageRange().GetLastMessageTimestamp(); last > cutoff && last < now+86400 {
		cutoff = last
	}
	if cutoff <= 0 {
		cutoff = now
	}
	res, err := store.db.Exec(
		fmt.Sprintf(`UPDATE messages SET read_state = 'read', read_source = 'chat_marked_read', read_at = ?
			WHERE chat_jid IN (%s) AND is_from_me = 0 AND read_state != 'read'
			AND CAST(strftime('%%s', timestamp) AS INTEGER) <= ?`, placeholders(len(chats))),
		append(append([]interface{}{cutoff}, stringArgs(chats)...), cutoff)...,
	)
	if err != nil {
		logger.Warnf("Failed to mark chat read: %v", err)
		return
	}
	for _, chat := range chats {
		store.db.Exec(`INSERT INTO chat_read_state (chat_jid, read_up_to, marked_unread, updated_at) VALUES (?, ?, 0, ?)
			ON CONFLICT(chat_jid) DO UPDATE SET read_up_to = MAX(IFNULL(read_up_to, 0), excluded.read_up_to),
			marked_unread = 0, updated_at = excluded.updated_at`, chat, cutoff, now)
	}
	n, _ := res.RowsAffected()
	fmt.Printf("Chat %s marked read on another device: %d message(s) now read\n", evt.JID, n)
}

// applyHistoryUnreadCount approximates read state from a history sync: the
// newest unreadCount incoming messages are unread and older ones read. Only
// messages whose state is unknown or itself came from a history sync change.
func (store *MessageStore) applyHistoryUnreadCount(chatJID string, unreadCount uint32, markedUnread bool) error {
	_, err := store.db.Exec(`
		UPDATE messages SET
			read_state = CASE WHEN id IN (
				SELECT id FROM messages WHERE chat_jid = ?1 AND is_from_me = 0
				ORDER BY CAST(strftime('%s', timestamp) AS INTEGER) DESC LIMIT ?2
			) THEN 'unread' ELSE 'read' END,
			read_source = 'history_sync', read_at = NULL
		WHERE chat_jid = ?1 AND is_from_me = 0 AND (read_state = 'unknown' OR read_source = 'history_sync')`,
		chatJID, unreadCount)
	if err != nil {
		return err
	}
	_, err = store.db.Exec(`INSERT INTO chat_read_state (chat_jid, marked_unread, history_unread_count, updated_at) VALUES (?, ?, ?, ?)
		ON CONFLICT(chat_jid) DO UPDATE SET marked_unread = excluded.marked_unread,
		history_unread_count = excluded.history_unread_count, updated_at = excluded.updated_at`,
		chatJID, markedUnread, unreadCount, time.Now().Unix())
	return err
}

// markReadBatch is one MarkRead call: messages in one chat from one sender.
type markReadBatch struct {
	Chat   types.JID
	Sender types.JID // empty for 1:1 chats
	IDs    []types.MessageID
}

// planMarkRead finds which of the given messages can be marked read on
// WhatsApp, grouped into MarkRead calls. Only incoming messages in the given
// chat (or its LID/phone alias) are included.
func planMarkRead(client *whatsmeow.Client, store *MessageStore, chat types.JID, ids []string) ([]markReadBatch, error) {
	if len(ids) == 0 {
		return nil, fmt.Errorf("no message IDs given")
	}
	chats := chatJIDStrings(client, chat)
	rows, err := store.db.Query(
		fmt.Sprintf(`SELECT chat_jid, id, sender FROM messages
			WHERE chat_jid IN (%s) AND id IN (%s) AND is_from_me = 0 ORDER BY timestamp`,
			placeholders(len(chats)), placeholders(len(ids))),
		stringArgs(chats, ids)...,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var batches []markReadBatch
	index := map[string]int{}
	for rows.Next() {
		var chatJID, id, sender string
		if err := rows.Scan(&chatJID, &id, &sender); err != nil {
			return nil, err
		}
		chatParsed, err := types.ParseJID(chatJID)
		if err != nil {
			continue
		}
		var senderJID types.JID
		if chatParsed.Server == types.GroupServer {
			senderJID = senderJIDFor(client, sender)
		}
		key := chatParsed.String() + "|" + senderJID.String()
		if i, ok := index[key]; ok {
			batches[i].IDs = append(batches[i].IDs, id)
		} else {
			index[key] = len(batches)
			batches = append(batches, markReadBatch{Chat: chatParsed, Sender: senderJID, IDs: []types.MessageID{id}})
		}
	}
	return batches, rows.Err()
}

// senderJIDFor turns a stored sender (a full JID, or a bare number that may be
// a phone number or a LID) into a JID.
func senderJIDFor(client *whatsmeow.Client, sender string) types.JID {
	if strings.Contains(sender, "@") {
		if jid, err := types.ParseJID(sender); err == nil {
			return jid.ToNonAD()
		}
	}
	lid := types.NewJID(sender, types.HiddenUserServer)
	if client != nil && client.Store != nil && client.Store.LIDs != nil {
		if pn, err := client.Store.LIDs.GetPNForLID(context.Background(), lid); err == nil && !pn.IsEmpty() {
			return lid
		}
	}
	return types.NewJID(sender, types.DefaultUserServer)
}

// markReadOnWhatsApp sends read receipts for the planned batches and records
// the messages as read. Senders may see blue ticks.
func markReadOnWhatsApp(client *whatsmeow.Client, store *MessageStore, batches []markReadBatch) (int, error) {
	marked := 0
	now := time.Now()
	for _, batch := range batches {
		if err := client.MarkRead(context.Background(), batch.IDs, now, batch.Chat, batch.Sender); err != nil {
			return marked, fmt.Errorf("failed to send read receipts: %v", err)
		}
		ids := make([]string, len(batch.IDs))
		copy(ids, batch.IDs)
		store.db.Exec(
			fmt.Sprintf(`UPDATE messages SET read_state = 'read', read_source = 'marked_on_whatsapp', read_at = ?
				WHERE chat_jid = ? AND id IN (%s)`, placeholders(len(ids))),
			append([]interface{}{now.Unix(), batch.Chat.String()}, stringArgs(ids)...)...,
		)
		marked += len(batch.IDs)
	}
	return marked, nil
}
