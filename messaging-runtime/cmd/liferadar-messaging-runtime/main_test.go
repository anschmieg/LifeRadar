package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

type fakeRoundTripper map[string][]byte

func (f fakeRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	body, ok := f[req.Method+" "+req.URL.Path]
	if !ok {
		body = []byte(`{}`)
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       io.NopCloser(bytes.NewReader(body)),
		Request:    req,
	}, nil
}

func TestNormalizeBeeperCollectionSupportsArrayAndWrappedItems(t *testing.T) {
	arrayPayload := []byte(`[{"id":"chat-1"},{"id":"chat-2"}]`)
	wrappedPayload := []byte(`{"items":[{"id":"chat-3"}],"hasMore":true,"nextCursor":"older"}`)

	arrayItems, arrayMeta, err := normalizeBeeperCollection(arrayPayload)
	if err != nil {
		t.Fatalf("array payload returned error: %v", err)
	}
	if len(arrayItems) != 2 {
		t.Fatalf("array payload item count = %d, want 2", len(arrayItems))
	}
	if arrayMeta.HasMore || arrayMeta.NextCursor != "" {
		t.Fatalf("array payload meta = %+v, want zero pagination", arrayMeta)
	}

	wrappedItems, wrappedMeta, err := normalizeBeeperCollection(wrappedPayload)
	if err != nil {
		t.Fatalf("wrapped payload returned error: %v", err)
	}
	if len(wrappedItems) != 1 {
		t.Fatalf("wrapped payload item count = %d, want 1", len(wrappedItems))
	}
	if !wrappedMeta.HasMore || wrappedMeta.NextCursor != "older" {
		t.Fatalf("wrapped payload meta = %+v, want hasMore older", wrappedMeta)
	}
}

func TestListMessagesBackfillsMissingChatIDFromPath(t *testing.T) {
	app := &app{
		cfg: config{beeperBaseURL: "http://beeper.local", beeperToken: "desktop-token", backfillPageLimit: 50},
		httpClient: &http.Client{Transport: fakeRoundTripper{
			"GET /v1/chats/chat-1/messages": []byte(`[{"id":"msg-1","sortKey":"1","timestamp":"2026-05-20T16:00:00Z","text":"hello"}]`),
		}},
	}

	page, err := app.listMessages(context.Background(), "chat-1", "")
	if err != nil {
		t.Fatalf("listMessages returned error: %v", err)
	}
	if len(page.Items) != 1 {
		t.Fatalf("message count = %d, want 1", len(page.Items))
	}
	if page.Items[0].ChatID != "chat-1" {
		t.Fatalf("message ChatID = %q, want chat-1", page.Items[0].ChatID)
	}
}

func TestConnectorStatusMetadataIncludesOperationalCounts(t *testing.T) {
	app := &app{
		cfg: config{beeperBaseURL: "http://beeper.local", beeperToken: "desktop-token", backfillPageLimit: 50, backfillChatLimit: 50},
		httpClient: &http.Client{Transport: fakeRoundTripper{
			"GET /v1/info":                  []byte(`{"version":"5.0.0"}`),
			"POST /oauth/introspect":        []byte(`{"active":true}`),
			"GET /v1/accounts":              []byte(`[{"accountID":"telegram","network":"Telegram","status":"connected"}]`),
			"GET /v1/chats":                 []byte(`[{"id":"chat-1","accountID":"telegram","network":"Telegram","title":"Ada","type":"single"},{"id":"chat-2","accountID":"telegram","network":"Telegram","title":"Group","type":"group"}]`),
			"GET /v1/chats/chat-1/messages": []byte(`[{"id":"msg-1","chatID":"chat-1","sortKey":"1","timestamp":"2026-05-20T16:00:00Z","text":"hello"}]`),
			"GET /v1/chats/chat-2/messages": []byte(`[{"id":"msg-2","chatID":"chat-2","sortKey":"2","timestamp":"2026-05-20T16:01:00Z","text":"group hello"}]`),
		}},
	}

	status, _ := app.connectorStatus(context.Background())
	body, _ := json.Marshal(status.Metadata)
	text := string(body)
	for _, want := range []string{
		`"token_configured":true`,
		`"token_kind":"beeper_desktop_api_bearer"`,
		`"accounts_count":1`,
		`"chats_count":2`,
		`"messages_count":2`,
		`"chat_type_counts"`,
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("metadata %s missing %s", text, want)
		}
	}
}
