package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPingForwardsOnlyFixedPayloadAndUUID(t *testing.T) {
	var gotUserAgent string
	var gotBody string

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUserAgent = r.Header.Get("User-Agent")
		body, _ := io.ReadAll(r.Body)
		gotBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"sessionId":"session-1","visitId":"visit-1"}`))
	}))
	defer upstream.Close()

	cfg := config{
		UmamiEndpoint: upstream.URL,
		WebsiteID:     "website-id",
		Hostname:      "livebound",
		EventName:     "startup",
	}

	req := httptest.NewRequest(http.MethodPost, "/ping", strings.NewReader(`{"id":"550e8400-e29b-41d4-a716-446655440000"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Client-UA-That-Must-Not-Leak")
	req.Header.Set("X-Forwarded-For", "203.0.113.123")
	rec := httptest.NewRecorder()

	pingHandler(cfg, upstream.Client()).ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusNoContent)
	}
	if gotUserAgent != userAgent {
		t.Fatalf("upstream user-agent = %q, want %q", gotUserAgent, userAgent)
	}
	if strings.Contains(gotBody, "203.0.113.123") || strings.Contains(gotBody, "Client-UA-That-Must-Not-Leak") {
		t.Fatalf("client metadata leaked upstream: %s", gotBody)
	}
	for _, want := range []string{"website-id", "livebound", "startup", "550e8400-e29b-41d4-a716-446655440000", "installation_id"} {
		if !strings.Contains(gotBody, want) {
			t.Fatalf("upstream body %q does not contain %q", gotBody, want)
		}
	}
}

func TestPingRejectsSilentUmamiIgnore(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"beep":"boop"}`))
	}))
	defer upstream.Close()

	cfg := config{UmamiEndpoint: upstream.URL, WebsiteID: "website-id", Hostname: "livebound", EventName: "startup"}
	req := httptest.NewRequest(http.MethodPost, "/ping", strings.NewReader(`{"id":"550e8400-e29b-41d4-a716-446655440000"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	pingHandler(cfg, upstream.Client()).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadGateway)
	}
}

func TestPingRejectsUnknownFields(t *testing.T) {
	cfg := config{UmamiEndpoint: "http://127.0.0.1", WebsiteID: "website-id", Hostname: "livebound", EventName: "startup"}
	req := httptest.NewRequest(http.MethodPost, "/ping", strings.NewReader(`{"id":"550e8400-e29b-41d4-a716-446655440000","extra":"no"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	pingHandler(cfg, http.DefaultClient).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadRequest)
	}
}

func TestPingRejectsInvalidUUID(t *testing.T) {
	cfg := config{UmamiEndpoint: "http://127.0.0.1", WebsiteID: "website-id", Hostname: "livebound", EventName: "startup"}
	req := httptest.NewRequest(http.MethodPost, "/ping", strings.NewReader(`{"id":"not-a-uuid"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	pingHandler(cfg, http.DefaultClient).ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadRequest)
	}
}
