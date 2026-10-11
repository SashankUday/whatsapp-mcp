package main

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
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

func TestRequireLocal(t *testing.T) {
	handler := newAPIHandler(nil, nil, apiPort)

	cases := []struct {
		name        string
		host        string
		contentType string
		want        int
	}{
		{"foreign host (DNS rebinding)", "evil.example:8080", "application/json", http.StatusForbidden},
		{"LAN address as host", "192.168.1.10:8080", "application/json", http.StatusForbidden},
		{"loopback without port", "127.0.0.1", "application/json", http.StatusForbidden},
		{"text/plain (cross-site form post)", "127.0.0.1:8080", "text/plain", http.StatusUnsupportedMediaType},
		{"form encoded", "127.0.0.1:8080", "application/x-www-form-urlencoded", http.StatusUnsupportedMediaType},
		{"no content type", "127.0.0.1:8080", "", http.StatusUnsupportedMediaType},
		// An empty JSON body passes the guard and is rejected by the handler's
		// own validation, before anything is sent.
		{"valid request reaches handler", "127.0.0.1:8080", "application/json", http.StatusBadRequest},
		{"localhost is allowed", "localhost:8080", "application/json; charset=utf-8", http.StatusBadRequest},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/api/send", strings.NewReader("{}"))
			req.Host = c.host
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
