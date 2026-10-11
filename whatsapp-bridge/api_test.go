package main

import (
	"encoding/hex"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The API must accept connections on 127.0.0.1 and refuse them on every other
// interface, including IPv6 loopback and the machine's LAN addresses.
func TestAPIListensOnLoopbackOnly(t *testing.T) {
	listener, err := listenAPI(0)
	if err != nil {
		t.Fatalf("listenAPI: %v", err)
	}
	defer listener.Close()
	go http.Serve(listener, http.NotFoundHandler())

	addr := listener.Addr().(*net.TCPAddr)
	if !addr.IP.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Errorf("API bound to %s, want 127.0.0.1", addr.IP)
	}
	port := addr.Port

	conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), time.Second)
	if err != nil {
		t.Fatalf("dial 127.0.0.1: %v", err)
	}
	conn.Close()

	targets := []net.IP{net.IPv6loopback}
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		t.Fatalf("InterfaceAddrs: %v", err)
	}
	external := 0
	for _, a := range addrs {
		ipNet, ok := a.(*net.IPNet)
		if !ok || ipNet.IP.IsLoopback() || ipNet.IP.IsLinkLocalUnicast() {
			continue
		}
		targets = append(targets, ipNet.IP)
		external++
	}
	if external == 0 {
		t.Log("no non-loopback interface address found; only checking ::1")
	}

	for _, ip := range targets {
		target := net.JoinHostPort(ip.String(), fmt.Sprint(port))
		if conn, err := net.DialTimeout("tcp", target, time.Second); err == nil {
			conn.Close()
			t.Errorf("connection to %s succeeded, want refused", target)
		}
	}
}

const testToken = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

type apiRequest struct {
	name        string
	method      string
	path        string
	host        string
	origin      *string
	auth        string
	contentType string
	want        int
}

func str(s string) *string { return &s }

func runAPIRequests(t *testing.T, handler http.Handler, cases []apiRequest) {
	t.Helper()
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			method, path, host := c.method, c.path, c.host
			if method == "" {
				method = http.MethodPost
			}
			if path == "" {
				path = "/api/send"
			}
			if host == "" {
				host = "127.0.0.1:8080"
			}
			req := httptest.NewRequest(method, path, strings.NewReader("{}"))
			req.Host = host
			if c.origin != nil {
				req.Header.Set("Origin", *c.origin)
			}
			if c.auth != "" {
				req.Header.Set("Authorization", c.auth)
			}
			if c.contentType != "" {
				req.Header.Set("Content-Type", c.contentType)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != c.want {
				t.Errorf("status %d, want %d (%s)", rec.Code, c.want, strings.TrimSpace(rec.Body.String()))
			}
		})
	}
}

func TestRequireLocal(t *testing.T) {
	handler := newAPIHandler(nil, nil, apiPort, testToken)
	auth := "Bearer " + testToken
	json := "application/json"

	runAPIRequests(t, handler, []apiRequest{
		// Host header (DNS rebinding)
		{name: "foreign host", host: "evil.example:8080", auth: auth, contentType: json, want: http.StatusForbidden},
		{name: "LAN address as host", host: "192.168.1.10:8080", auth: auth, contentType: json, want: http.StatusForbidden},
		{name: "loopback without port", host: "127.0.0.1", auth: auth, contentType: json, want: http.StatusForbidden},

		// Origin header: any value is refused, even with a valid token
		{name: "cross-site origin", origin: str("https://evil.example"), auth: auth, contentType: json, want: http.StatusForbidden},
		{name: "same-origin page", origin: str("http://127.0.0.1:8080"), auth: auth, contentType: json, want: http.StatusForbidden},
		{name: "null origin (sandboxed page)", origin: str("null"), auth: auth, contentType: json, want: http.StatusForbidden},
		{name: "empty origin", origin: str(""), auth: auth, contentType: json, want: http.StatusForbidden},

		// Token
		{name: "no token", contentType: json, want: http.StatusUnauthorized},
		{name: "wrong token", auth: "Bearer " + strings.Repeat("0", 64), contentType: json, want: http.StatusUnauthorized},
		{name: "token prefix only", auth: "Bearer " + testToken[:32], contentType: json, want: http.StatusUnauthorized},
		{name: "token without Bearer", auth: testToken, contentType: json, want: http.StatusUnauthorized},
		{name: "empty bearer", auth: "Bearer ", contentType: json, want: http.StatusUnauthorized},

		// Content type (cross-site form posts)
		{name: "text/plain", auth: auth, contentType: "text/plain", want: http.StatusUnsupportedMediaType},
		{name: "form encoded", auth: auth, contentType: "application/x-www-form-urlencoded", want: http.StatusUnsupportedMediaType},
		{name: "no content type", auth: auth, want: http.StatusUnsupportedMediaType},

		// An empty JSON body passes the guard and is rejected by the handler's
		// own validation, before anything is sent.
		{name: "valid request reaches handler", auth: auth, contentType: json, want: http.StatusBadRequest},
		{name: "localhost is allowed", host: "localhost:8080", auth: auth, contentType: "application/json; charset=utf-8", want: http.StatusBadRequest},
	})
}

// Every endpoint, including unknown paths and GETs, needs the token.
func TestEveryEndpointNeedsToken(t *testing.T) {
	handler := newAPIHandler(nil, nil, apiPort, testToken)
	var cases []apiRequest
	for _, path := range []string{"/api/send", "/api/download", "/api/mark-read", "/api/unknown", "/"} {
		for _, method := range []string{http.MethodGet, http.MethodPost} {
			cases = append(cases, apiRequest{
				name: method + " " + path, method: method, path: path,
				contentType: "application/json", want: http.StatusUnauthorized,
			})
		}
	}
	runAPIRequests(t, handler, cases)
}

// With no token configured, nothing is accepted.
func TestEmptyTokenRejectsEverything(t *testing.T) {
	handler := newAPIHandler(nil, nil, apiPort, "")
	runAPIRequests(t, handler, []apiRequest{
		{name: "no token", contentType: "application/json", want: http.StatusUnauthorized},
		{name: "empty bearer", auth: "Bearer ", contentType: "application/json", want: http.StatusUnauthorized},
	})
}

func TestLoadOrCreateAPIToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "api-token")

	token, err := loadOrCreateAPIToken(path)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if decoded, err := hex.DecodeString(token); err != nil || len(decoded) != 32 {
		t.Fatalf("token %q is not 32 bytes of hex", token)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("new token file has mode %o, want 600", perm)
	}

	again, err := loadOrCreateAPIToken(path)
	if err != nil || again != token {
		t.Errorf("second load returned %q, %v; want the same token", again, err)
	}

	// A token file with wider permissions is tightened, not replaced
	os.Chmod(path, 0644)
	again, err = loadOrCreateAPIToken(path)
	if err != nil || again != token {
		t.Errorf("load after chmod returned %q, %v; want the same token", again, err)
	}
	info, _ = os.Stat(path)
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("token file left at mode %o, want 600", perm)
	}

	other, err := loadOrCreateAPIToken(filepath.Join(t.TempDir(), "api-token"))
	if err != nil || other == token {
		t.Errorf("a second token file should get a different token")
	}
}

func TestLoadOrCreateAPITokenRejectsBadFiles(t *testing.T) {
	dir := t.TempDir()

	short := filepath.Join(dir, "short")
	os.WriteFile(short, []byte("abc\n"), 0600)
	if _, err := loadOrCreateAPIToken(short); err == nil {
		t.Error("short token accepted")
	}

	empty := filepath.Join(dir, "empty")
	os.WriteFile(empty, nil, 0600)
	if _, err := loadOrCreateAPIToken(empty); err == nil {
		t.Error("empty token accepted")
	}

	target := filepath.Join(dir, "target")
	os.WriteFile(target, []byte(testToken), 0600)
	link := filepath.Join(dir, "link")
	os.Symlink(target, link)
	if _, err := loadOrCreateAPIToken(link); err == nil {
		t.Error("symlinked token file accepted")
	}

	notDir := filepath.Join(dir, "missing-dir", "api-token")
	if _, err := loadOrCreateAPIToken(notDir); err == nil {
		t.Error("token created in a missing directory")
	}
}

func TestValidAPIToken(t *testing.T) {
	cases := map[string]bool{
		"Bearer " + testToken:       true,
		"bearer " + testToken:       false,
		"Bearer  " + testToken:      false,
		"Bearer " + testToken + "x": false,
		"Basic " + testToken:        false,
		"":                          false,
	}
	for header, want := range cases {
		if got := validAPIToken(header, testToken); got != want {
			t.Errorf("validAPIToken(%q) = %v, want %v", header, got, want)
		}
	}
	if validAPIToken("Bearer ", "") {
		t.Error("empty token accepted")
	}
}

// The guard holds over a real loopback connection, as the MCP server uses it.
func TestServedAPIOverLoopback(t *testing.T) {
	listener, err := listenAPI(0)
	if err != nil {
		t.Fatalf("listenAPI: %v", err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	go http.Serve(listener, newAPIHandler(nil, nil, port, testToken))

	url := fmt.Sprintf("http://127.0.0.1:%d/api/download", port)
	post := func(auth, origin string) int {
		req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader("{}"))
		req.Header.Set("Content-Type", "application/json")
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		if origin != "" {
			req.Header.Set("Origin", origin)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("request: %v", err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}

	if got := post("", ""); got != http.StatusUnauthorized {
		t.Errorf("no token: %d, want 401", got)
	}
	if got := post("Bearer "+testToken, "https://evil.example"); got != http.StatusForbidden {
		t.Errorf("origin with token: %d, want 403", got)
	}
	// Passes the guard; the handler rejects the empty body
	if got := post("Bearer "+testToken, ""); got != http.StatusBadRequest {
		t.Errorf("valid token: %d, want 400", got)
	}
}
