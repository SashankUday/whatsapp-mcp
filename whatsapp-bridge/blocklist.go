package main

import (
	"bufio"
	"bytes"
	"fmt"
	"os"
	"strings"
	"sync"
)

// Chat blocklist shared with the Python MCP server.
//
// The file lives at ../blocklist.txt (the repo root, since the bridge runs from
// whatsapp-bridge/) unless WHATSAPP_MCP_BLOCKLIST is set. One entry per line,
// '#' starts a comment. An entry is a phone number, an exact JID (contains '@'),
// or a chat name ("name: <chat name>" or bare text, case-insensitive).
//
// The file is re-read on every check and reparsed when it changes, so edits
// apply without a restart. A missing file blocks nothing; a file that exists
// but can't be read blocks everything.

const defaultBlocklistPath = "../blocklist.txt"

type blocklist struct {
	mu sync.Mutex

	path    string
	content string
	loaded  bool
	failed  bool // file exists but could not be read: fail closed

	phones map[string]bool
	jids   map[string]bool
	names  map[string]bool

	// JIDs blocked because their chat name matched, so that later checks
	// without a name (e.g. StoreMessage) still block them.
	nameBlockedJIDs map[string]bool
}

var chatBlocklist = &blocklist{}

func blocklistPath() string {
	if p := os.Getenv("WHATSAPP_MCP_BLOCKLIST"); p != "" {
		return p
	}
	return defaultBlocklistPath
}

// normalizePhone strips spaces, '+', '-' and brackets. It returns "" if what's
// left is not a plain run of digits.
func normalizePhone(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r == ' ' || r == '\t' || r == '+' || r == '-' || r == '(' || r == ')' || r == '[' || r == ']':
			continue
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		default:
			return ""
		}
	}
	return b.String()
}

// jidUser returns the user part of a JID with any ":device" suffix removed.
func jidUser(jid string) string {
	user := jid
	if i := strings.Index(user, "@"); i >= 0 {
		user = user[:i]
	}
	if i := strings.Index(user, ":"); i >= 0 {
		user = user[:i]
	}
	return user
}

func (bl *blocklist) reset() {
	bl.phones = map[string]bool{}
	bl.jids = map[string]bool{}
	bl.names = map[string]bool{}
	bl.nameBlockedJIDs = map[string]bool{}
}

func (bl *blocklist) parse(data []byte) {
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for scanner.Scan() {
		line := scanner.Text()
		if i := strings.Index(line, "#"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if len(line) >= 5 && strings.EqualFold(line[:5], "name:") {
			if name := strings.TrimSpace(line[5:]); name != "" {
				bl.names[strings.ToLower(name)] = true
			}
		} else if strings.Contains(line, "@") {
			bl.jids[strings.ToLower(line)] = true
		} else if phone := normalizePhone(line); phone != "" {
			bl.phones[phone] = true
		} else {
			bl.names[strings.ToLower(line)] = true
		}
	}
}

// refresh re-reads the file and reparses it if its contents changed. Reading
// on every check (the file is tiny) means permission changes are noticed too.
// Caller must hold bl.mu.
func (bl *blocklist) refresh() {
	path := blocklistPath()
	data, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		if !bl.failed {
			fmt.Printf("Blocklist %s could not be read, blocking all chats: %v\n", path, err)
		}
		bl.reset()
		bl.path, bl.content, bl.loaded, bl.failed = path, "", true, true
		return
	}

	// A missing file is treated as empty: nothing blocked
	content := string(data)
	if bl.loaded && !bl.failed && bl.path == path && bl.content == content {
		return
	}
	bl.reset()
	bl.path, bl.content, bl.loaded, bl.failed = path, content, true, false
	bl.parse(data)
}

// IsBlocked reports whether a chat is on the blocklist. name may be empty.
func IsBlocked(chatJID, name string) bool {
	bl := chatBlocklist
	bl.mu.Lock()
	defer bl.mu.Unlock()

	bl.refresh()
	if bl.failed {
		return true
	}

	jid := strings.ToLower(chatJID)
	if bl.jids[jid] || bl.nameBlockedJIDs[jid] {
		return true
	}
	if user := jidUser(chatJID); user != "" && bl.phones[user] {
		return true
	}
	if name != "" && bl.names[strings.ToLower(strings.TrimSpace(name))] {
		if jid != "" {
			bl.nameBlockedJIDs[jid] = true
		}
		return true
	}
	return false
}
