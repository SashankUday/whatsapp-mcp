package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAdv"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// All IDs below are made up. Phone numbers use the 07700 900xxx range, which
// is reserved for fiction.

// newTestBridge sets up what a freshly paired bridge has: an empty LID map,
// an empty messages.db and the given blocklist, in a temporary directory.
func newTestBridge(t *testing.T, blocklist string) (*whatsmeow.Client, *MessageStore) {
	t.Helper()
	dir := t.TempDir()
	t.Chdir(dir)
	path := filepath.Join(dir, "blocklist.txt")
	if err := os.WriteFile(path, []byte(blocklist), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("WHATSAPP_MCP_BLOCKLIST", path)

	ctx := context.Background()
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+filepath.Join(dir, "wa.db")+"?_foreign_keys=on", waLog.Noop)
	if err != nil {
		t.Fatal(err)
	}
	device := container.NewDevice()
	device.ID = &types.JID{User: "447700900000", Device: 7, Server: types.DefaultUserServer}
	device.LID = types.JID{User: "100000000000000", Device: 7, Server: types.HiddenUserServer}
	device.Account = &waAdv.ADVSignedDeviceIdentity{ // dummy values of the lengths the schema requires
		Details: []byte{0}, AccountSignature: make([]byte, 64), AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64),
	}
	if err := container.PutDevice(ctx, device); err != nil {
		t.Fatal(err)
	}
	client := whatsmeow.NewClient(device, waLog.Noop)

	store, err := NewMessageStore()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	return client, store
}

// rowsFor counts rows mentioning any of the given chat JIDs in every table
// the bridge writes.
func rowsFor(t *testing.T, store *MessageStore, jids ...string) map[string]int {
	t.Helper()
	counts := map[string]int{}
	for table, column := range map[string]string{
		"chats": "jid", "messages": "chat_jid", "self_read_receipts": "chat_jid", "chat_read_state": "chat_jid",
	} {
		for _, jid := range jids {
			var n int
			if err := store.db.QueryRow("SELECT COUNT(*) FROM "+table+" WHERE "+column+" = ?", jid).Scan(&n); err != nil {
				t.Fatal(err)
			}
			counts[table] += n
		}
	}
	return counts
}

func assertNoRows(t *testing.T, store *MessageStore, what string, jids ...string) {
	t.Helper()
	for table, n := range rowsFor(t, store, jids...) {
		if n != 0 {
			t.Errorf("%s: %d row(s) in %s", what, n, table)
		}
	}
}

func historyConversation(id string, msgID string, ts time.Time) *waHistorySync.Conversation {
	remote := id
	return &waHistorySync.Conversation{
		ID:          proto.String(id),
		Name:        proto.String("test chat"),
		UnreadCount: proto.Uint32(1),
		Messages: []*waHistorySync.HistorySyncMsg{{
			Message: &waWeb.WebMessageInfo{
				Key:              &waCommon.MessageKey{RemoteJID: &remote, FromMe: proto.Bool(false), ID: proto.String(msgID)},
				Message:          &waE2E.Message{Conversation: proto.String("test")},
				MessageTimestamp: proto.Uint64(uint64(ts.Unix())),
			},
		}},
	}
}

// A history sync must write nothing for a blocked chat (chat row, messages or
// read state), even when the chat arrives under the form that isn't on the
// blocklist and whatsmeow hasn't stored the LID mapping yet.
func TestHistorySyncWritesNothingForBlockedChats(t *testing.T) {
	client, store := newTestBridge(t, `
+44 7700 900001               # A: phone number only
200000000000002@lid           # B: LID only
447700900003                  # C: phone number only
447700900004                  # D: phone number only
120363000000000005@g.us       # a group
`)
	ts := time.Now().Add(-time.Hour)

	// A arrives under its LID, with the phone JID on the conversation
	convA := historyConversation("300000000000001@lid", "A1", ts)
	convA.PnJID = proto.String("447700900001@s.whatsapp.net")
	// B arrives under its phone JID, with the LID on the conversation
	convB := historyConversation("447700900002@s.whatsapp.net", "B1", ts)
	convB.LidJID = proto.String("200000000000002@lid")
	// C arrives under its LID; only the sync's mapping list links it
	convC := historyConversation("300000000000003@lid", "C1", ts)
	// D arrives under the legacy c.us server
	convD := historyConversation("447700900004@c.us", "D1", ts)
	convGroup := historyConversation("120363000000000005@g.us", "G1", ts)
	convAllowed := historyConversation("447700900099@s.whatsapp.net", "OK1", ts)

	handleHistorySync(client, store, &events.HistorySync{Data: &waHistorySync.HistorySync{
		SyncType:      waHistorySync.HistorySync_INITIAL_BOOTSTRAP.Enum(),
		Conversations: []*waHistorySync.Conversation{convA, convB, convC, convD, convGroup, convAllowed},
		PhoneNumberToLidMappings: []*waHistorySync.PhoneNumberToLIDMapping{
			{PnJID: proto.String("447700900003@s.whatsapp.net"), LidJID: proto.String("300000000000003@lid")},
		},
	}}, waLog.Noop)

	assertNoRows(t, store, "A (blocked by phone, arrived as LID)", "300000000000001@lid", "447700900001@s.whatsapp.net")
	assertNoRows(t, store, "B (blocked by LID, arrived as phone)", "447700900002@s.whatsapp.net", "200000000000002@lid")
	assertNoRows(t, store, "C (blocked by phone, linked by sync mapping)", "300000000000003@lid", "447700900003@s.whatsapp.net")
	assertNoRows(t, store, "D (blocked by phone, arrived as c.us)", "447700900004@c.us", "447700900004@s.whatsapp.net")
	assertNoRows(t, store, "blocked group", "120363000000000005@g.us")

	// The unblocked chat is stored normally: proves the sync was processed
	got := rowsFor(t, store, "447700900099@s.whatsapp.net")
	if got["chats"] != 1 || got["messages"] != 1 || got["chat_read_state"] != 1 {
		t.Errorf("unblocked chat: got %v, want 1 chat, 1 message, 1 read-state row", got)
	}
	var total int
	store.db.QueryRow("SELECT (SELECT COUNT(*) FROM chats) + (SELECT COUNT(*) FROM messages) + (SELECT COUNT(*) FROM chat_read_state) + (SELECT COUNT(*) FROM self_read_receipts)").Scan(&total)
	if total != 3 {
		t.Errorf("%d rows in total, want 3 (all from the unblocked chat)", total)
	}
}

// Live messages and read receipts for a blocked 1:1 chat write nothing, even
// when the chat arrives under its unlisted form and only the event's own
// alt address links it to the blocklist entry.
func TestLiveEventsWriteNothingForBlockedChats(t *testing.T) {
	client, store := newTestBridge(t, "447700900011\n200000000000012@lid\n")
	now := time.Now()

	incoming := &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:      types.JID{User: "300000000000011", Server: types.HiddenUserServer},
				Sender:    types.JID{User: "300000000000011", Server: types.HiddenUserServer},
				SenderAlt: types.JID{User: "447700900011", Server: types.DefaultUserServer},
			},
			ID: "L1", Timestamp: now,
		},
		Message: &waE2E.Message{Conversation: proto.String("test")},
	}
	outgoing := &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:         types.JID{User: "447700900012", Server: types.DefaultUserServer},
				Sender:       *client.Store.ID,
				IsFromMe:     true,
				RecipientAlt: types.JID{User: "200000000000012", Server: types.HiddenUserServer},
			},
			ID: "L2", Timestamp: now,
		},
		Message: &waE2E.Message{Conversation: proto.String("test")},
	}
	handleMessage(client, store, incoming, waLog.Noop)
	handleMessage(client, store, outgoing, waLog.Noop)

	handleReceipt(client, store, &events.Receipt{
		MessageSource: types.MessageSource{
			Chat:      types.JID{User: "300000000000011", Server: types.HiddenUserServer},
			Sender:    *client.Store.ID,
			IsFromMe:  true,
			SenderAlt: types.JID{User: "447700900011", Server: types.DefaultUserServer},
		},
		MessageIDs: []types.MessageID{"L1"},
		Timestamp:  now,
		Type:       types.ReceiptTypeReadSelf,
	}, waLog.Noop)

	assertNoRows(t, store, "incoming, blocked by phone, arrived as LID",
		"300000000000011@lid", "447700900011@s.whatsapp.net")
	assertNoRows(t, store, "outgoing, blocked by LID, arrived as phone",
		"447700900012@s.whatsapp.net", "200000000000012@lid")

	// Control: an unblocked live message is stored
	allowed := &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:   types.JID{User: "447700900099", Server: types.DefaultUserServer},
				Sender: types.JID{User: "447700900099", Server: types.DefaultUserServer},
			},
			ID: "OK2", Timestamp: now,
		},
		Message: &waE2E.Message{Conversation: proto.String("test")},
	}
	handleMessage(client, store, allowed, waLog.Noop)
	if got := rowsFor(t, store, "447700900099@s.whatsapp.net"); got["messages"] != 1 {
		t.Errorf("unblocked live message not stored: %v", got)
	}
}
