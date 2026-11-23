package main

// The health check the image runs on itself (/receiver -health), against made-up receivers on this machine.

import (
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func serveOnAPort(t *testing.T, status int) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != healthRoute || !strings.HasPrefix(r.Host, "127.0.0.1:") { // this machine, by address
			w.WriteHeader(http.StatusTeapot)
			return
		}
		w.WriteHeader(status)
	}))
	t.Cleanup(server.Close)
	return portOf(t, server.Listener.Addr().String())
}

func portOf(t *testing.T, address string) string {
	t.Helper()
	_, port, err := net.SplitHostPort(address)
	if err != nil {
		t.Fatal(err)
	}
	return port
}

func TestTheHealthFlagExitsZeroOnlyWhenHealthzAnswers200(t *testing.T) {
	t.Setenv("PORT", serveOnAPort(t, http.StatusOK))
	if code := checkHealth(); code != 0 {
		t.Errorf("200: exit %d", code)
	}
	t.Setenv("PORT", serveOnAPort(t, http.StatusServiceUnavailable))
	if code := checkHealth(); code != 1 {
		t.Errorf("503: exit %d", code)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed := strconv.Itoa(listener.Addr().(*net.TCPAddr).Port)
	listener.Close()
	t.Setenv("PORT", closed)
	if code := checkHealth(); code != 1 {
		t.Errorf("nothing listening: exit %d", code)
	}
	t.Setenv("PORT", "eighty")
	if code := checkHealth(); code != 1 {
		t.Errorf("a PORT that is not a number: exit %d", code)
	}
}
