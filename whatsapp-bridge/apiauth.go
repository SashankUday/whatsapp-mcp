package main

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strings"
)

// The REST API requires a shared secret on every request, so other programs
// on this machine (or a web page) can't use it. The bridge generates the token
// on first start and keeps it in store/ (git-ignored), readable only by you.
// The MCP server reads the same file. Delete it and restart the bridge to
// rotate it.
const apiTokenPath = "store/api-token"

const apiTokenBytes = 32

// loadOrCreateAPIToken returns the API token stored at path, creating it with
// 0600 permissions if it doesn't exist. An existing file with wider
// permissions is tightened to 0600.
func loadOrCreateAPIToken(path string) (string, error) {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err == nil {
		buf := make([]byte, apiTokenBytes)
		if _, err := rand.Read(buf); err != nil {
			f.Close()
			os.Remove(path)
			return "", fmt.Errorf("failed to generate API token: %v", err)
		}
		token := hex.EncodeToString(buf)
		if _, err := f.WriteString(token + "\n"); err != nil {
			f.Close()
			os.Remove(path)
			return "", fmt.Errorf("failed to write API token: %v", err)
		}
		if err := f.Close(); err != nil {
			os.Remove(path)
			return "", fmt.Errorf("failed to write API token: %v", err)
		}
		fmt.Printf("Created API token at %s\n", path)
		return token, nil
	}
	if !errors.Is(err, fs.ErrExist) {
		return "", fmt.Errorf("failed to create API token: %v", err)
	}

	info, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("failed to read API token: %v", err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("%s is not a regular file; delete it and restart to create a new token", path)
	}
	if info.Mode().Perm() != 0600 {
		if err := os.Chmod(path, 0600); err != nil {
			return "", fmt.Errorf("failed to restrict %s to 0600: %v", path, err)
		}
		fmt.Printf("Restricted %s to owner-only permissions\n", path)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("failed to read API token: %v", err)
	}
	token := strings.TrimSpace(string(data))
	if decoded, err := hex.DecodeString(token); err != nil || len(decoded) != apiTokenBytes {
		return "", fmt.Errorf("%s is not a valid API token; delete it and restart to create a new one", path)
	}
	return token, nil
}

// validAPIToken reports whether the request's Authorization header carries the
// API token, compared in constant time.
func validAPIToken(header, token string) bool {
	presented, ok := strings.CutPrefix(header, "Bearer ")
	if !ok || token == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(presented), []byte(token)) == 1
}
