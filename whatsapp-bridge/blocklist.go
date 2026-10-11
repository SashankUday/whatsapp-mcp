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
// '#' starts a comment. Blocking is per chat and by ID only:
//
//   - a number (phone number or LID digits; spaces, '+', '-' and brackets are
//     ignored) blocks the 1:1 chat with that person;
//   - a JID blocks that chat: "<number>@s.whatsapp.net", "<lid>@lid" or a
//     group "<id>@g.us".
//
// A person's phone JID and LID are the same chat; chatBlocked in main.go checks
// both. Display names are never matched. Any line that isn't a number or a JID
// makes the whole file invalid, which blocks everything, so a leftover name or
// a mistyped number can't silently unblock a chat.
//
// The file is re-read on every check and reparsed when it changes, so edits
// apply without a restart. A missing file blocks nothing; a file that exists
// but can't be read or parsed blocks everything.

const defaultBlocklistPath = "../blocklist.txt"

type blocklist struct {
	mu sync.Mutex

	path    string
	content string
	loaded  bool
	failed  bool // file exists but could not be read or parsed: fail closed

	numbers map[string]bool
	jids    map[string]bool
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

// normalizeJID lowercases a JID and strips any ":device" suffix from its user
// part. It returns the user and server parts.
func normalizeJID(jid string) (string, string) {
	user, server, _ := strings.Cut(strings.ToLower(strings.TrimSpace(jid)), "@")
	user, _, _ = strings.Cut(user, ":")
	return user, server
}

func (bl *blocklist) reset() {
	bl.numbers = map[string]bool{}
	bl.jids = map[string]bool{}
}

func (bl *blocklist) parse(data []byte) error {
	scanner := bufio.NewScanner(bytes.NewReader(data))
	for lineNo := 1; scanner.Scan(); lineNo++ {
		line := scanner.Text()
		if i := strings.Index(line, "#"); i >= 0 {
			line = line[:i]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.Contains(line, "@") {
			if user, server := normalizeJID(line); user != "" && server != "" && !strings.Contains(line, " ") {
				bl.jids[user+"@"+server] = true
				continue
			}
		} else if number := normalizePhone(line); number != "" {
			bl.numbers[number] = true
			continue
		}
		return fmt.Errorf("line %d is not a phone number, LID or JID (names are not supported): %q", lineNo, line)
	}
	return nil
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
		// Not loaded: the next successful read must reparse, whatever its content
		bl.path, bl.content, bl.loaded, bl.failed = path, "", false, true
		return
	}

	// A missing file is treated as empty: nothing blocked
	content := string(data)
	if bl.loaded && bl.path == path && bl.content == content {
		return
	}
	bl.reset()
	bl.path, bl.content, bl.loaded, bl.failed = path, content, true, false
	if err := bl.parse(data); err != nil {
		fmt.Printf("Blocklist %s is invalid, blocking all chats: %v\n", path, err)
		bl.reset()
		bl.failed = true
	}
}

// IsBlocked reports whether this exact chat JID is on the blocklist. Use
// chatBlocked to also check a 1:1 chat's LID/phone alias.
func IsBlocked(chatJID string) bool {
	bl := chatBlocklist
	bl.mu.Lock()
	defer bl.mu.Unlock()

	bl.refresh()
	if bl.failed {
		return true
	}

	user, server := normalizeJID(chatJID)
	if bl.jids[user+"@"+server] {
		return true
	}
	return (server == "s.whatsapp.net" || server == "lid") && bl.numbers[user]
}
