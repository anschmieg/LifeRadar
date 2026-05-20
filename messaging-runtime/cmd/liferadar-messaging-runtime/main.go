package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type config struct {
	port                 string
	dbURL                string
	beeperBaseURL        string
	beeperToken          string
	syncInterval         time.Duration
	backfillPageLimit    int
	backfillChatLimit    int
	backfillMessageLimit int
	httpTimeout          time.Duration
}

type app struct {
	cfg                     config
	db                      *pgxpool.Pool
	httpClient              *http.Client
	logger                  *log.Logger
	lastSyncAt              time.Time
	lastEventAt             time.Time
	lastWebsocketConnect    time.Time
	lastWebsocketDisconnect time.Time
	websocketConnected      bool
	lastSyncStats           syncStats
	lastError               string
	mu                      sync.RWMutex
}

type syncStats struct {
	AccountsCount  int            `json:"accounts_count"`
	ChatsCount     int            `json:"chats_count"`
	MessagesCount  int            `json:"messages_count"`
	ChatTypeCounts map[string]int `json:"chat_type_counts"`
}

type collectionMeta struct {
	HasMore    bool
	NextCursor string
}

type connectorAccount struct {
	Provider     string         `json:"provider"`
	AccountID    string         `json:"account_id"`
	DisplayLabel string         `json:"display_label,omitempty"`
	AuthState    string         `json:"auth_state"`
	Enabled      bool           `json:"enabled"`
	LastSyncedAt *time.Time     `json:"last_synced_at,omitempty"`
	LastErrorAt  *time.Time     `json:"last_error_at,omitempty"`
	LastError    string         `json:"last_error,omitempty"`
	Metadata     map[string]any `json:"metadata"`
}

type connectorStatus struct {
	Provider string             `json:"provider"`
	Enabled  bool               `json:"enabled"`
	Accounts []connectorAccount `json:"accounts"`
	Metadata map[string]any     `json:"metadata"`
}

type sendRequest struct {
	ConversationID string `json:"conversation_id"`
	ExternalID     string `json:"external_id"`
	ContentText    string `json:"content_text"`
}

type sendResponse struct {
	Status    string `json:"status"`
	MessageID string `json:"message_id"`
}

func main() {
	cfg := loadConfig()
	logger := log.New(os.Stdout, "[liferadar-messaging-runtime] ", log.LstdFlags|log.Lmicroseconds)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	db, err := pgxpool.New(ctx, cfg.dbURL)
	if err != nil {
		logger.Fatalf("connect db: %v", err)
	}
	defer db.Close()

	app := &app{
		cfg:        cfg,
		db:         db,
		httpClient: &http.Client{Timeout: cfg.httpTimeout},
		logger:     logger,
	}

	go app.runSyncLoop(ctx)
	go app.runWebsocketLoop(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/health", app.handleHealth)
	mux.HandleFunc("/connectors", app.handleConnectors)
	mux.HandleFunc("/send", app.handleSend)

	server := &http.Server{
		Addr:              ":" + cfg.port,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()

	logger.Printf("listening on :%s", cfg.port)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Fatalf("server failed: %v", err)
	}
}

func loadConfig() config {
	return config{
		port:                 env("LIFERADAR_MESSAGING_RUNTIME_PORT", "8030"),
		dbURL:                databaseURL(),
		beeperBaseURL:        strings.TrimRight(env("BEEPER_DESKTOP_BASE_URL", "http://liferadar-beeper-sidecar:23373"), "/"),
		beeperToken:          strings.TrimSpace(os.Getenv("BEEPER_ACCESS_TOKEN")),
		syncInterval:         envDuration("LIFERADAR_BEEPER_SYNC_INTERVAL", 10*time.Minute),
		backfillPageLimit:    envInt("LIFERADAR_BEEPER_BACKFILL_PAGE_LIMIT", 100),
		backfillChatLimit:    envInt("LIFERADAR_BEEPER_BACKFILL_CHAT_LIMIT", 250),
		backfillMessageLimit: envInt("LIFERADAR_BEEPER_BACKFILL_MESSAGE_LIMIT", 2000),
		httpTimeout:          envDuration("LIFERADAR_BEEPER_HTTP_TIMEOUT", 45*time.Second),
	}
}

func databaseURL() string {
	if value := strings.TrimSpace(os.Getenv("LIFERADAR_DATABASE_URL")); value != "" {
		return value
	}
	host := env("LIFERADAR_DB_HOST", "localhost")
	port := env("LIFERADAR_DB_PORT", "5432")
	name := env("LIFERADAR_DB_NAME", "life_radar")
	user := env("LIFERADAR_DB_USER", "life_radar")
	password := url.QueryEscape(os.Getenv("LIFERADAR_DB_PASSWORD"))
	return fmt.Sprintf("postgres://%s:%s@%s:%s/%s", user, password, host, port, name)
}

func env(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := time.ParseDuration(raw)
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func (a *app) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	info, err := a.beeperInfo(r.Context())
	status := map[string]any{
		"status":           "ok",
		"service":          "liferadar-messaging-runtime",
		"beeper_reachable": err == nil,
	}
	if err != nil {
		status["status"] = "degraded"
		status["error"] = err.Error()
	} else {
		status["beeper"] = info
	}
	a.respondJSON(w, http.StatusOK, status)
}

func (a *app) handleConnectors(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	status, _ := a.connectorStatus(r.Context())
	a.respondJSON(w, http.StatusOK, []connectorStatus{status})
}

func (a *app) handleSend(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req sendRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(req.ExternalID) == "" || strings.TrimSpace(req.ContentText) == "" {
		http.Error(w, "external_id and content_text are required", http.StatusBadRequest)
		return
	}
	payload := map[string]any{"text": req.ContentText}
	data, err := a.beeperJSON(r.Context(), http.MethodPost, "/v1/chats/"+url.PathEscape(req.ExternalID)+"/messages", payload, true)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	messageID, _ := stringValue(data, "pendingMessageID")
	if messageID == "" {
		messageID, _ = stringValue(data, "messageID")
	}
	if messageID == "" {
		http.Error(w, "beeper send did not return a message id", http.StatusBadGateway)
		return
	}
	a.respondJSON(w, http.StatusOK, sendResponse{Status: "sent", MessageID: messageID})
}

func (a *app) connectorStatus(ctx context.Context) (connectorStatus, error) {
	status := connectorStatus{
		Provider: "beeper",
		Enabled:  false,
		Metadata: map[string]any{
			"beeper_base_url":  a.cfg.beeperBaseURL,
			"token_configured": strings.TrimSpace(a.cfg.beeperToken) != "",
			"token_kind":       "beeper_desktop_api_bearer",
		},
	}
	if strings.TrimSpace(a.cfg.beeperToken) == "" {
		status.Metadata["token_error"] = "BEEPER_ACCESS_TOKEN is not configured yet"
		return status, nil
	}

	info, infoErr := a.beeperInfo(ctx)
	introspection, introErr := a.introspectToken(ctx)
	accounts, accountErr := a.beeperAccounts(ctx)
	if accountErr == nil && a.db != nil {
		if err := a.persistAccounts(ctx, accounts); err != nil {
			a.logger.Printf("persist accounts: %v", err)
		}
	}
	diagnostics, diagnosticErr := a.collectDiagnostics(ctx)

	status.Enabled = infoErr == nil && accountErr == nil
	status.Accounts = accounts
	status.Metadata["accounts_count"] = len(accounts)
	if infoErr == nil {
		status.Metadata["info"] = info
	} else {
		status.Metadata["info_error"] = infoErr.Error()
	}
	if introErr == nil {
		status.Metadata["token"] = introspection
	} else if strings.TrimSpace(a.cfg.beeperToken) == "" {
		status.Metadata["token_error"] = "BEEPER_ACCESS_TOKEN is not configured yet"
	} else {
		status.Metadata["token_introspection_error"] = introErr.Error()
	}
	if accountErr != nil {
		status.Metadata["accounts_error"] = accountErr.Error()
	}
	if diagnosticErr == nil {
		status.Metadata["chats_count"] = diagnostics.ChatsCount
		status.Metadata["messages_count"] = diagnostics.MessagesCount
		status.Metadata["chat_type_counts"] = diagnostics.ChatTypeCounts
	} else {
		status.Metadata["diagnostics_error"] = diagnosticErr.Error()
	}
	a.mu.RLock()
	if !a.lastSyncAt.IsZero() {
		status.Metadata["last_sync_at"] = a.lastSyncAt.UTC().Format(time.RFC3339)
	}
	if a.lastSyncStats.AccountsCount > 0 || a.lastSyncStats.ChatsCount > 0 || a.lastSyncStats.MessagesCount > 0 {
		status.Metadata["last_sync_accounts_count"] = a.lastSyncStats.AccountsCount
		status.Metadata["last_sync_chats_count"] = a.lastSyncStats.ChatsCount
		status.Metadata["last_sync_messages_count"] = a.lastSyncStats.MessagesCount
		status.Metadata["last_sync_chat_type_counts"] = a.lastSyncStats.ChatTypeCounts
	}
	if !a.lastEventAt.IsZero() {
		status.Metadata["last_event_at"] = a.lastEventAt.UTC().Format(time.RFC3339)
	}
	status.Metadata["websocket_connected"] = a.websocketConnected
	if !a.lastWebsocketConnect.IsZero() {
		status.Metadata["last_websocket_connect_at"] = a.lastWebsocketConnect.UTC().Format(time.RFC3339)
	}
	if !a.lastWebsocketDisconnect.IsZero() {
		status.Metadata["last_websocket_disconnect_at"] = a.lastWebsocketDisconnect.UTC().Format(time.RFC3339)
	}
	if a.lastError != "" {
		status.Metadata["last_error"] = a.lastError
	}
	a.mu.RUnlock()
	return status, nil
}

func (a *app) runSyncLoop(ctx context.Context) {
	if strings.TrimSpace(a.cfg.beeperToken) == "" {
		a.logger.Printf("BEEPER_ACCESS_TOKEN is not configured; skipping sync loop")
		return
	}
	a.syncOnce(context.Background())
	ticker := time.NewTicker(a.cfg.syncInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			a.syncOnce(context.Background())
		}
	}
}

func (a *app) syncOnce(ctx context.Context) {
	stats, err := a.sync(ctx)
	if err != nil {
		a.setError(err)
		a.logger.Printf("sync failed: %v", err)
		return
	}
	a.mu.Lock()
	a.lastSyncAt = time.Now().UTC()
	a.lastSyncStats = stats
	a.lastError = ""
	a.mu.Unlock()
}

func (a *app) sync(ctx context.Context) (syncStats, error) {
	accounts, err := a.beeperAccounts(ctx)
	if err != nil {
		return syncStats{}, err
	}
	if err := a.persistAccounts(ctx, accounts); err != nil {
		return syncStats{}, err
	}
	chats, err := a.listChats(ctx)
	if err != nil {
		return syncStats{}, err
	}
	stats := syncStats{
		AccountsCount:  len(accounts),
		ChatsCount:     len(chats),
		ChatTypeCounts: map[string]int{},
	}
	for _, chat := range chats {
		stats.ChatTypeCounts[chat.Type]++
		count, err := a.syncChat(ctx, chat)
		if err != nil {
			a.logger.Printf("sync chat %s: %v", chat.ID, err)
			continue
		}
		stats.MessagesCount += count
	}
	return stats, nil
}

func (a *app) syncChat(ctx context.Context, chat beeperChat) (int, error) {
	conversationID, err := a.upsertConversation(ctx, chat)
	if err != nil {
		return 0, err
	}
	checkpoint, err := a.loadCheckpoint(ctx, chat.AccountID, "chat:"+chat.ID)
	if err != nil {
		return 0, err
	}
	var latest string
	var cursor string
	total := 0

	for {
		page, err := a.listMessages(ctx, chat.ID, cursor)
		if err != nil {
			return total, err
		}
		if len(page.Items) == 0 {
			break
		}
		sort.Slice(page.Items, func(i, j int) bool {
			return page.Items[i].SortKey < page.Items[j].SortKey
		})

		stop := false
		for _, message := range page.Items {
			if latest == "" || message.SortKey > latest {
				latest = message.SortKey
			}
			if checkpoint != "" && message.SortKey <= checkpoint {
				stop = true
				continue
			}
			if message.AccountID == "" {
				message.AccountID = chat.AccountID
			}
			if message.ChatID == "" {
				message.ChatID = chat.ID
			}
			if err := a.upsertMessage(ctx, conversationID, chat, message); err != nil {
				a.logger.Printf("upsert message %s: %v", message.ID, err)
			}
			total++
			if total >= a.cfg.backfillMessageLimit {
				stop = true
				break
			}
		}
		if stop || !page.HasMore || page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
	}

	if latest != "" {
		value := map[string]any{
			"latest_sort_key": latest,
			"updated_at":      time.Now().UTC().Format(time.RFC3339),
		}
		if err := a.saveCheckpoint(ctx, chat.AccountID, "chat:"+chat.ID, value); err != nil {
			return total, err
		}
	}
	return total, nil
}

func (a *app) runWebsocketLoop(ctx context.Context) {
	if strings.TrimSpace(a.cfg.beeperToken) == "" {
		a.logger.Printf("BEEPER_ACCESS_TOKEN is not configured; skipping websocket loop")
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		if err := a.consumeWebsocket(ctx); err != nil {
			a.setError(err)
			a.logger.Printf("websocket loop failed: %v", err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(10 * time.Second):
			}
		}
	}
}

func (a *app) consumeWebsocket(ctx context.Context) error {
	endpoint := a.cfg.beeperBaseURL + "/v1/ws"
	header := http.Header{"Authorization": []string{"Bearer " + a.cfg.beeperToken}}
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, strings.Replace(endpoint, "http://", "ws://", 1), header)
	if err != nil {
		return err
	}
	defer func() {
		conn.Close()
		a.mu.Lock()
		a.websocketConnected = false
		a.lastWebsocketDisconnect = time.Now().UTC()
		a.mu.Unlock()
	}()
	a.mu.Lock()
	a.websocketConnected = true
	a.lastWebsocketConnect = time.Now().UTC()
	a.mu.Unlock()

	if err := conn.WriteJSON(map[string]any{
		"type":      "subscriptions.set",
		"requestID": "r1",
		"chatIDs":   []string{"*"},
	}); err != nil {
		return err
	}

	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		var payload map[string]any
		if err := conn.ReadJSON(&payload); err != nil {
			return err
		}
		a.mu.Lock()
		a.lastEventAt = time.Now().UTC()
		a.mu.Unlock()
		_ = a.handleEvent(context.Background(), payload)
	}
}

func (a *app) handleEvent(ctx context.Context, payload map[string]any) error {
	eventType := strings.ToLower(asString(payload["type"]))
	data := mapValue(payload["data"])
	if len(data) == 0 {
		data = mapValue(payload["payload"])
	}

	switch {
	case strings.Contains(eventType, "chat") && (strings.Contains(eventType, "upsert") || strings.Contains(eventType, "updated")):
		chat := parseChat(data)
		if chat.ID == "" {
			return nil
		}
		_, err := a.upsertConversation(ctx, chat)
		return err
	case strings.Contains(eventType, "message") && (strings.Contains(eventType, "upsert") || strings.Contains(eventType, "created") || strings.Contains(eventType, "updated")):
		message := parseMessage(data)
		chat := parseChat(data)
		if chat.ID == "" {
			chat.ID = message.ChatID
			chat.AccountID = message.AccountID
			chat.Network = asString(data["network"])
			chat.Title = asString(data["chatName"])
		}
		if message.ID == "" || chat.ID == "" {
			return nil
		}
		conversationID, err := a.upsertConversation(ctx, chat)
		if err != nil {
			return err
		}
		return a.upsertMessage(ctx, conversationID, chat, message)
	default:
		return nil
	}
}

func (a *app) beeperInfo(ctx context.Context) (map[string]any, error) {
	return a.beeperJSON(ctx, http.MethodGet, "/v1/info", nil, true)
}

func (a *app) introspectToken(ctx context.Context) (map[string]any, error) {
	form := url.Values{}
	form.Set("token", a.cfg.beeperToken)
	form.Set("token_type_hint", "access_token")

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.cfg.beeperBaseURL+"/oauth/introspect", strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("introspect failed: %s", strings.TrimSpace(string(body)))
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func (a *app) beeperAccounts(ctx context.Context) ([]connectorAccount, error) {
	items, _, err := a.beeperCollection(ctx, http.MethodGet, "/v1/accounts", nil, true)
	if err != nil {
		return nil, err
	}
	var out []connectorAccount
	for _, raw := range items {
		item := mapValue(raw)
		if len(item) == 0 {
			continue
		}
		now := time.Now().UTC()
		out = append(out, connectorAccount{
			Provider:     "beeper",
			AccountID:    asString(item["accountID"]),
			DisplayLabel: firstNonEmpty(asString(mapValue(item["user"])["fullName"]), asString(mapValue(item["user"])["username"]), asString(mapValue(item["user"])["phoneNumber"]), asString(item["network"])),
			AuthState:    firstNonEmpty(asString(item["status"]), "connected"),
			Enabled:      !strings.EqualFold(asString(item["status"]), "disabled"),
			LastSyncedAt: &now,
			Metadata: map[string]any{
				"network": asString(item["network"]),
				"user":    mapValue(item["user"]),
				"bridge":  mapValue(item["bridge"]),
			},
		})
	}
	return out, nil
}

type beeperChat struct {
	ID        string
	AccountID string
	Network   string
	Type      string
	Title     string
	LastEvent *time.Time
	Metadata  map[string]any
}

type messagePage struct {
	Items      []beeperMessage
	HasMore    bool
	NextCursor string
}

type beeperMessage struct {
	ID        string
	AccountID string
	ChatID    string
	SenderID  string
	SortKey   string
	Timestamp time.Time
	Text      string
	Inbound   bool
	Metadata  map[string]any
}

func (a *app) listChats(ctx context.Context) ([]beeperChat, error) {
	var chats []beeperChat
	cursor := ""
	for len(chats) < a.cfg.backfillChatLimit {
		path := fmt.Sprintf("/v1/chats?limit=%d", a.cfg.backfillPageLimit)
		if cursor != "" {
			path += "&cursor=" + url.QueryEscape(cursor) + "&direction=before"
		}
		items, meta, err := a.beeperCollection(ctx, http.MethodGet, path, nil, true)
		if err != nil {
			return nil, err
		}
		if len(items) == 0 {
			break
		}
		for _, raw := range items {
			chats = append(chats, parseChat(mapValue(raw)))
			if len(chats) >= a.cfg.backfillChatLimit {
				break
			}
		}
		if !meta.HasMore || meta.NextCursor == "" {
			break
		}
		cursor = meta.NextCursor
	}
	return chats, nil
}

func (a *app) listMessages(ctx context.Context, chatID, cursor string) (messagePage, error) {
	path := fmt.Sprintf("/v1/chats/%s/messages?limit=%d", url.PathEscape(chatID), a.cfg.backfillPageLimit)
	if cursor != "" {
		path += "&cursor=" + url.QueryEscape(cursor) + "&direction=before"
	}
	items, meta, err := a.beeperCollection(ctx, http.MethodGet, path, nil, true)
	if err != nil {
		return messagePage{}, err
	}
	result := messagePage{
		HasMore:    meta.HasMore,
		NextCursor: meta.NextCursor,
	}
	for _, raw := range items {
		message := parseMessage(mapValue(raw))
		if message.ChatID == "" {
			message.ChatID = chatID
		}
		result.Items = append(result.Items, message)
	}
	return result, nil
}

func (a *app) collectDiagnostics(ctx context.Context) (syncStats, error) {
	chats, err := a.listChats(ctx)
	if err != nil {
		return syncStats{}, err
	}
	stats := syncStats{
		ChatsCount:     len(chats),
		ChatTypeCounts: map[string]int{},
	}
	for _, chat := range chats {
		stats.ChatTypeCounts[chat.Type]++
		cursor := ""
		for {
			page, err := a.listMessages(ctx, chat.ID, cursor)
			if err != nil {
				return stats, err
			}
			stats.MessagesCount += len(page.Items)
			if !page.HasMore || page.NextCursor == "" {
				break
			}
			cursor = page.NextCursor
		}
	}
	return stats, nil
}

func (a *app) beeperCollection(ctx context.Context, method, path string, body any, auth bool) ([]any, collectionMeta, error) {
	data, err := a.beeperRaw(ctx, method, path, body, auth)
	if err != nil {
		return nil, collectionMeta{}, err
	}
	return normalizeBeeperCollection(data)
}

func normalizeBeeperCollection(data []byte) ([]any, collectionMeta, error) {
	if len(data) == 0 {
		return nil, collectionMeta{}, nil
	}
	var items []any
	if err := json.Unmarshal(data, &items); err == nil {
		return items, collectionMeta{}, nil
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, collectionMeta{}, err
	}
	meta := collectionMeta{
		HasMore:    boolValue(payload["hasMore"]),
		NextCursor: firstNonEmpty(asString(payload["nextCursor"]), asString(payload["cursor"])),
	}
	items = arrayValue(payload["items"])
	if len(items) == 0 {
		items = arrayValue(payload["data"])
	}
	if len(items) == 0 {
		if _, ok := payload["id"]; ok {
			items = []any{payload}
		} else if _, ok := payload["accountID"]; ok {
			items = []any{payload}
		}
	}
	return items, meta, nil
}

func (a *app) beeperJSON(ctx context.Context, method, path string, body any, auth bool) (map[string]any, error) {
	data, err := a.beeperRaw(ctx, method, path, body, auth)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return map[string]any{}, nil
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		var arr []any
		if errArr := json.Unmarshal(data, &arr); errArr == nil {
			return map[string]any{"items": arr}, nil
		}
		return nil, err
	}
	return payload, nil
}

func (a *app) beeperRaw(ctx context.Context, method, path string, body any, auth bool) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, a.cfg.beeperBaseURL+path, reader)
	if err != nil {
		return nil, err
	}
	if auth {
		req.Header.Set("Authorization", "Bearer "+a.cfg.beeperToken)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("beeper api %s %s failed: %s", method, path, strings.TrimSpace(string(data)))
	}
	return data, nil
}

func (a *app) persistAccounts(ctx context.Context, accounts []connectorAccount) error {
	for _, account := range accounts {
		metadata, _ := json.Marshal(account.Metadata)
		_, err := a.db.Exec(ctx, `
			insert into life_radar.connector_accounts
				(provider, account_id, display_label, auth_state, enabled, last_synced_at, last_error, metadata)
			values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
			on conflict (provider, account_id) do update
			set display_label = excluded.display_label,
			    auth_state = excluded.auth_state,
			    enabled = excluded.enabled,
			    last_synced_at = excluded.last_synced_at,
			    last_error = excluded.last_error,
			    metadata = life_radar.connector_accounts.metadata || excluded.metadata,
			    updated_at = now()
		`, account.Provider, account.AccountID, account.DisplayLabel, account.AuthState, account.Enabled, account.LastSyncedAt, account.LastError, string(metadata))
		if err != nil {
			return err
		}
	}
	return nil
}

func (a *app) upsertConversation(ctx context.Context, chat beeperChat) (string, error) {
	participants, _ := json.Marshal([]map[string]any{})
	metadata, _ := json.Marshal(chat.Metadata)
	var id string
	err := a.db.QueryRow(ctx, `
		insert into life_radar.conversations
			(source, external_id, account_id, title, participants, last_event_at, metadata)
		values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
		on conflict (source, external_id) do update
		set account_id = excluded.account_id,
		    title = coalesce(excluded.title, life_radar.conversations.title),
		    last_event_at = greatest(coalesce(life_radar.conversations.last_event_at, excluded.last_event_at), coalesce(excluded.last_event_at, life_radar.conversations.last_event_at)),
		    metadata = life_radar.conversations.metadata || excluded.metadata,
		    updated_at = now()
		returning id::text
	`, sourceForNetwork(chat.Network), chat.ID, chat.AccountID, nullIfEmpty(chat.Title), string(participants), chat.LastEvent, string(metadata)).Scan(&id)
	return id, err
}

func (a *app) upsertMessage(ctx context.Context, conversationID string, chat beeperChat, message beeperMessage) error {
	contentJSON, _ := json.Marshal(message.Metadata)
	provenance, _ := json.Marshal(map[string]any{
		"transport":         "beeper_desktop",
		"beeper_account_id": chat.AccountID,
		"beeper_chat_id":    chat.ID,
		"beeper_network":    chat.Network,
		"beeper_sort_key":   message.SortKey,
	})
	_, err := a.db.Exec(ctx, `
		insert into life_radar.message_events
			(conversation_id, source, external_id, sender_id, occurred_at, content_text, content_json, is_inbound, provenance)
		values ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb)
		on conflict (source, external_id) do update
		set conversation_id = excluded.conversation_id,
		    sender_id = coalesce(excluded.sender_id, life_radar.message_events.sender_id),
		    occurred_at = excluded.occurred_at,
		    content_text = coalesce(excluded.content_text, life_radar.message_events.content_text),
		    content_json = life_radar.message_events.content_json || excluded.content_json,
		    is_inbound = excluded.is_inbound,
		    provenance = life_radar.message_events.provenance || excluded.provenance,
		    updated_at = now()
	`, conversationID, sourceForNetwork(chat.Network), message.ChatID+":"+message.ID, nullIfEmpty(message.SenderID), message.Timestamp, nullIfEmpty(message.Text), string(contentJSON), message.Inbound, string(provenance))
	return err
}

func (a *app) loadCheckpoint(ctx context.Context, accountID, key string) (string, error) {
	var raw []byte
	err := a.db.QueryRow(ctx, `
		select checkpoint_value::text
		from life_radar.connector_sync_checkpoints
		where provider = 'beeper' and account_id = $1 and checkpoint_key = $2
	`, accountID, key).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", nil
	}
	return asString(payload["latest_sort_key"]), nil
}

func (a *app) saveCheckpoint(ctx context.Context, accountID, key string, value map[string]any) error {
	data, _ := json.Marshal(value)
	_, err := a.db.Exec(ctx, `
		insert into life_radar.connector_sync_checkpoints
			(provider, account_id, checkpoint_key, checkpoint_value)
		values ('beeper', $1, $2, $3::jsonb)
		on conflict (provider, account_id, checkpoint_key) do update
		set checkpoint_value = excluded.checkpoint_value,
		    updated_at = now()
	`, accountID, key, string(data))
	return err
}

func (a *app) respondJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func (a *app) setError(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.lastError = err.Error()
}

func parseChat(data map[string]any) beeperChat {
	preview := mapValue(data["preview"])
	lastEvent := parseTime(preview["timestamp"])
	if lastEvent == nil {
		lastEvent = parseTime(data["lastMessageAt"])
	}
	return beeperChat{
		ID:        firstNonEmpty(asString(data["id"]), asString(data["chatID"])),
		AccountID: asString(data["accountID"]),
		Network:   firstNonEmpty(asString(data["network"]), asString(mapValue(data["account"])["network"])),
		Type:      firstNonEmpty(asString(data["type"]), "unknown"),
		Title:     firstNonEmpty(asString(data["title"]), asString(data["name"]), asString(mapValue(data["user"])["fullName"])),
		LastEvent: lastEvent,
		Metadata: map[string]any{
			"transport":         "beeper_desktop",
			"beeper_account_id": asString(data["accountID"]),
			"beeper_chat_id":    firstNonEmpty(asString(data["id"]), asString(data["chatID"])),
			"beeper_network":    firstNonEmpty(asString(data["network"]), asString(mapValue(data["account"])["network"])),
			"beeper_chat_type":  firstNonEmpty(asString(data["type"]), "unknown"),
			"beeper_chat":       data,
		},
	}
}

func parseMessage(data map[string]any) beeperMessage {
	text := firstNonEmpty(asString(data["text"]), asString(mapValue(data["content"])["text"]), asString(mapValue(data["body"])["text"]))
	timestamp := parseTime(data["timestamp"])
	if timestamp == nil {
		now := time.Now().UTC()
		timestamp = &now
	}
	inbound := true
	if boolVal, ok := data["isFromMe"].(bool); ok {
		inbound = !boolVal
	}
	if boolVal, ok := data["fromMe"].(bool); ok {
		inbound = !boolVal
	}
	if boolVal, ok := data["isSender"].(bool); ok {
		inbound = !boolVal
	}
	return beeperMessage{
		ID:        firstNonEmpty(asString(data["id"]), asString(data["messageID"])),
		AccountID: asString(data["accountID"]),
		ChatID:    asString(data["chatID"]),
		SenderID:  asString(data["senderID"]),
		SortKey:   firstNonEmpty(asString(data["sortKey"]), asString(data["id"])),
		Timestamp: timestamp.UTC(),
		Text:      text,
		Inbound:   inbound,
		Metadata:  data,
	}
}

func parseTime(value any) *time.Time {
	switch v := value.(type) {
	case string:
		if v == "" {
			return nil
		}
		if t, err := time.Parse(time.RFC3339, v); err == nil {
			return &t
		}
		if millis, err := strconv.ParseInt(v, 10, 64); err == nil {
			t := time.UnixMilli(millis).UTC()
			return &t
		}
	case float64:
		t := time.UnixMilli(int64(v)).UTC()
		return &t
	case int64:
		t := time.UnixMilli(v).UTC()
		return &t
	}
	return nil
}

func sourceForNetwork(network string) string {
	normalized := strings.ToLower(strings.TrimSpace(network))
	switch {
	case strings.Contains(normalized, "telegram"):
		return "telegram"
	case strings.Contains(normalized, "whatsapp"):
		return "whatsapp"
	case strings.Contains(normalized, "signal"):
		return "signal"
	default:
		return "beeper"
	}
}

func nullIfEmpty(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}

func mapValue(value any) map[string]any {
	if out, ok := value.(map[string]any); ok {
		return out
	}
	return map[string]any{}
}

func arrayValue(value any) []any {
	if out, ok := value.([]any); ok {
		return out
	}
	return nil
}

func asString(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64)
	case int:
		return strconv.Itoa(v)
	case int64:
		return strconv.FormatInt(v, 10)
	default:
		return ""
	}
}

func boolValue(value any) bool {
	if out, ok := value.(bool); ok {
		return out
	}
	return false
}

func stringValue(data map[string]any, key string) (string, bool) {
	value := asString(data[key])
	return value, value != ""
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
