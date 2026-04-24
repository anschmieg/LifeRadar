package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type config struct {
	port                string
	dbURL               string
	apiKey              string
	mcpURL              string
	messagingRuntimeURL string
	matrixBridgeURL     string
	matrixHomeserverURL string
	matrixSessionPath   string
	matrixStorePath     string
}

type app struct {
	cfg        config
	db         *pgxpool.Pool
	httpClient *http.Client
	logger     *log.Logger
}

type messageSendRequest struct {
	ConversationID string `json:"conversation_id"`
	ContentText    string `json:"content_text"`
	UserApproved   bool   `json:"user_approved"`
	ApprovalNote   string `json:"approval_note"`
}

type matrixLoginRequest struct {
	Identifier               string `json:"identifier"`
	Password                 string `json:"password"`
	IdentifierKind           string `json:"identifier_kind"`
	InitialDeviceDisplayName string `json:"initial_device_display_name"`
}

type matrixVerificationStartRequest struct {
	TargetDeviceID string `json:"target_device_id"`
}

type matrixVerificationDecisionRequest struct {
	Decision string `json:"decision"`
}

type taskCreateRequest struct {
	SourceEntityType      string     `json:"source_entity_type"`
	Title                 string     `json:"title"`
	Summary               *string    `json:"summary"`
	Status                string     `json:"status"`
	ScheduledStart        *time.Time `json:"scheduled_start"`
	ScheduledEnd          *time.Time `json:"scheduled_end"`
	EffortEstimateMinutes *int       `json:"effort_estimate_minutes"`
}

type calendarEventUpsertRequest struct {
	Title              string     `json:"title"`
	Summary            *string    `json:"summary"`
	ScheduledStart     *time.Time `json:"scheduled_start"`
	ScheduledEnd       *time.Time `json:"scheduled_end"`
	CalendarExternalID *string    `json:"calendar_external_id"`
	CalendarProvider   *string    `json:"calendar_provider"`
}

type matrixSessionFile struct {
	AccessToken  string  `json:"access_token"`
	RefreshToken *string `json:"refresh_token"`
	UserID       string  `json:"user_id"`
	DeviceID     string  `json:"device_id"`
	Homeserver   string  `json:"homeserver"`
	ExpiresAt    *string `json:"expires_at"`
	ExpiresIn    any     `json:"expires_in"`
	SavedAt      string  `json:"saved_at"`
}

func main() {
	cfg := loadConfig()
	logger := log.New(os.Stdout, "[liferadar-api] ", log.LstdFlags|log.Lmicroseconds)

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
		httpClient: &http.Client{Timeout: 90 * time.Second},
		logger:     logger,
	}

	server := &http.Server{
		Addr:              ":" + cfg.port,
		Handler:           app.routes(),
		ReadHeaderTimeout: 15 * time.Second,
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
		port:                env("LIFERADAR_API_PORT", "8000"),
		dbURL:               databaseURL(),
		apiKey:              strings.TrimSpace(os.Getenv("LIFERADAR_API_KEY")),
		mcpURL:              env("LIFERADAR_MCP_URL", "http://liferadar-mcp:8090"),
		messagingRuntimeURL: env("LIFERADAR_MESSAGING_RUNTIME_URL", "http://liferadar-messaging-runtime:8030"),
		matrixBridgeURL:     env("LIFERADAR_MATRIX_BRIDGE_URL", "http://liferadar-matrix-bridge:8010"),
		matrixHomeserverURL: strings.TrimRight(env("LIFERADAR_MATRIX_HOMESERVER_URL", "https://matrix.beeper.com"), "/"),
		matrixSessionPath:   env("MATRIX_RUST_SESSION_PATH", "/app/identity/matrix-session.json"),
		matrixStorePath:     env("MATRIX_RUST_STORE", "/app/identity/matrix-rust-sdk-store"),
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

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", a.handleHealth)
	mux.HandleFunc("/connectors", a.handleConnectors)
	mux.HandleFunc("/connectors/", a.handleConnectorSubroutes)
	mux.HandleFunc("/auth/telegram", a.handleTelegramAuth)
	mux.HandleFunc("/auth/whatsapp", a.handleWhatsAppAuth)
	mux.HandleFunc("/auth/matrix-device", a.handleMatrixDevicePage)
	mux.HandleFunc("/matrix/session", a.handleMatrixSession)
	mux.HandleFunc("/matrix/devices", a.handleMatrixDevices)
	mux.HandleFunc("/matrix/login", a.handleMatrixLogin)
	mux.HandleFunc("/matrix/verification/start", a.handleMatrixVerificationStart)
	mux.HandleFunc("/matrix/verification/", a.handleMatrixVerificationSubroutes)
	mux.HandleFunc("/openapi.json", a.handleOpenAPI)
	mux.HandleFunc("/docs", a.handleDocs)
	mux.HandleFunc("/alerts", a.handleAlerts)
	mux.HandleFunc("/conversations", a.handleConversations)
	mux.HandleFunc("/conversations/", a.handleConversation)
	mux.HandleFunc("/messages", a.handleMessages)
	mux.HandleFunc("/messages/send", a.handleSendMessage)
	mux.HandleFunc("/commitments", a.handleCommitments)
	mux.HandleFunc("/reminders", a.handleReminders)
	mux.HandleFunc("/tasks", a.handleTasks)
	mux.HandleFunc("/calendar/events", a.handleCalendarEvents)
	mux.HandleFunc("/memories", a.handleMemories)
	mux.HandleFunc("/probe-status", a.handleProbeStatus)
	mux.HandleFunc("/probe-status/candidates", a.handleProbeCandidates)
	mux.HandleFunc("/search", a.handleSearch)
	mux.HandleFunc("/mcp", a.handleMCP)
	mux.HandleFunc("/mcp/", a.handleMCP)

	return a.withCORS(mux)
}

func (a *app) withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Credentials", "true")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (a *app) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	status := map[string]any{
		"status":   "ok",
		"database": "connected",
		"version":  "1.0.0",
	}
	if err := a.db.Ping(r.Context()); err != nil {
		status["status"] = "degraded"
		status["database"] = "error: " + err.Error()
	}
	a.respondJSON(w, http.StatusOK, status)
}

func (a *app) handleConnectors(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	a.proxyJSON(w, r, a.cfg.messagingRuntimeURL+"/connectors", nil)
}

func (a *app) handleConnectorSubroutes(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, false) {
		return
	}
	a.respondError(w, http.StatusGone, "Connector onboarding is now managed directly in the Beeper Desktop sidecar. Use noVNC or the Beeper Desktop UI to sign in and create an access token.")
}

func (a *app) handleTelegramAuth(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, true) {
		return
	}
	a.respondError(w, http.StatusGone, "Telegram login pages were removed in the Beeper rewrite. Use Beeper Desktop onboarding instead.")
}

func (a *app) handleWhatsAppAuth(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, true) {
		return
	}
	a.respondError(w, http.StatusGone, "WhatsApp login pages were removed in the Beeper rewrite. Use Beeper Desktop onboarding instead.")
}

func (a *app) handleMatrixDevicePage(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, true) {
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, matrixVerificationHTML)
}

func (a *app) handleMatrixSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	session, ok := a.readMatrixSession()
	if !ok {
		a.respondJSON(w, http.StatusOK, map[string]any{"has_session": false, "user_id": nil, "device_id": nil, "homeserver": nil})
		return
	}
	if _, err := a.matrixWhoAmI(r.Context(), session.AccessToken, session.Homeserver); err != nil {
		if code, _ := statusCodeFromMatrixError(err); code == http.StatusUnauthorized {
			a.resetMatrixLocalIdentityState()
			a.respondJSON(w, http.StatusOK, map[string]any{"has_session": false, "user_id": nil, "device_id": nil, "homeserver": nil})
			return
		}
		a.respondUpstreamError(w, err, "Matrix session is invalid")
		return
	}
	a.respondJSON(w, http.StatusOK, map[string]any{
		"has_session": true,
		"user_id":     session.UserID,
		"device_id":   session.DeviceID,
		"homeserver":  session.Homeserver,
	})
}

func (a *app) handleMatrixDevices(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	session, err := a.ensureValidMatrixSession(r.Context())
	if err != nil {
		a.respondUpstreamError(w, err, "No local Matrix session. Sign in again.")
		return
	}
	devices, err := a.matrixListDevices(r.Context(), session)
	if err != nil {
		a.respondUpstreamError(w, err, "Could not load Matrix devices")
		return
	}
	a.respondJSON(w, http.StatusOK, devices)
}

func (a *app) handleMatrixLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	var body matrixLoginRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		a.respondError(w, http.StatusBadRequest, "Invalid request body")
		return
	}
	payload, err := a.performMatrixPasswordLogin(r.Context(), body)
	if err != nil {
		a.respondUpstreamError(w, err, "Matrix login failed")
		return
	}
	a.respondJSON(w, http.StatusOK, payload)
}

func (a *app) handleMatrixVerificationStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	if _, err := a.ensureValidMatrixSession(r.Context()); err != nil {
		a.respondUpstreamError(w, err, "No local Matrix session. Sign in again.")
		return
	}
	var body matrixVerificationStartRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		a.respondError(w, http.StatusBadRequest, "Invalid request body")
		return
	}
	a.proxyJSON(w, r, a.cfg.matrixBridgeURL+"/verification/start", map[string]any{"target_device_id": body.TargetDeviceID})
}

func (a *app) handleMatrixVerificationSubroutes(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, false) {
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/matrix/verification/")
	if path == "" {
		a.respondError(w, http.StatusNotFound, "Not found")
		return
	}
	if strings.HasSuffix(path, "/confirm") {
		if r.Method != http.MethodPost {
			a.methodNotAllowed(w)
			return
		}
		attemptID := strings.TrimSuffix(path, "/confirm")
		attemptID = strings.TrimSuffix(attemptID, "/")
		var body matrixVerificationDecisionRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			a.respondError(w, http.StatusBadRequest, "Invalid request body")
			return
		}
		a.proxyJSON(w, r, a.cfg.matrixBridgeURL+"/verification/"+attemptID+"/confirm", map[string]any{"decision": body.Decision})
		return
	}
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	a.proxyJSON(w, r, a.cfg.matrixBridgeURL+"/verification/"+path, nil)
}

func (a *app) handleOpenAPI(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	a.respondJSON(w, http.StatusOK, map[string]any{
		"openapi": "3.1.0",
		"info": map[string]any{
			"title":       "LifeRadar API",
			"version":     "1.0.0",
			"description": "Personal intelligence and communications triage API",
		},
	})
}

func (a *app) handleDocs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, "<!doctype html><html><head><title>LifeRadar API - Swagger UI</title></head><body><h1>Swagger UI</h1><p>OpenAPI schema is available at <code>/openapi.json</code>.</p></body></html>")
}

func (a *app) handleAlerts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				c.id as conversation_id,
				COALESCE(c.title, c.external_id) as title,
				CASE
					WHEN c.needs_reply THEN 'needs_reply'
					WHEN c.blocked_needs_context THEN 'blocked'
					WHEN c.needs_read THEN 'needs_read'
					WHEN c.important_now THEN 'important'
					WHEN c.due_at IS NOT NULL AND c.due_at < NOW() THEN 'overdue'
					ELSE 'needs_read'
				END AS alert_type,
				COALESCE(c.priority_score::double precision, 0) AS priority_score,
				c.urgency_score::double precision AS urgency_score,
				c.due_at,
				c.source
			FROM life_radar.conversations c
			WHERE c.state = 'active'
			  AND (
				  c.needs_reply = TRUE
				  OR c.needs_read = TRUE
				  OR c.important_now = TRUE
				  OR c.blocked_needs_context = TRUE
				  OR (c.due_at IS NOT NULL AND c.due_at < NOW())
			  )
			ORDER BY c.priority_score DESC NULLS LAST, c.last_event_at DESC
			LIMIT $1
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, limit)
}

func (a *app) handleConversations(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	offset := intQuery(r, "offset", 0)
	source := strings.TrimSpace(r.URL.Query().Get("source"))
	state := strings.TrimSpace(r.URL.Query().Get("state"))

	var needsReply any = nil
	if raw := strings.TrimSpace(r.URL.Query().Get("needs_reply")); raw != "" {
		parsed := strings.EqualFold(raw, "true")
		if strings.EqualFold(raw, "false") || parsed {
			needsReply = parsed
		}
	}

	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				id,
				source,
				external_id,
				account_id,
				title,
				COALESCE(participants, '[]'::jsonb) AS participants,
				COALESCE(state, 'active') AS state,
				COALESCE(needs_read, FALSE) AS needs_read,
				COALESCE(needs_reply, FALSE) AS needs_reply,
				COALESCE(important_now, FALSE) AS important_now,
				COALESCE(waiting_on_other, FALSE) AS waiting_on_other,
				COALESCE(follow_up_later, FALSE) AS follow_up_later,
				COALESCE(ready_to_act, FALSE) AS ready_to_act,
				COALESCE(blocked_needs_context, FALSE) AS blocked_needs_context,
				last_event_at,
				last_triaged_at,
				priority_score::double precision AS priority_score,
				urgency_score::double precision AS urgency_score,
				social_weight::double precision AS social_weight,
				reward_value::double precision AS reward_value,
				energy_fit::double precision AS energy_fit,
				effort_estimate_minutes,
				due_at,
				COALESCE(metadata, '{}'::jsonb) AS metadata,
				created_at,
				updated_at
			FROM life_radar.conversations
			WHERE (COALESCE(state, 'active') = $1 OR ($1 = '' AND COALESCE(state, 'active') != 'archived'))
			  AND ($2 = '' OR source = $2)
			  AND ($3::boolean IS NULL OR needs_reply = $3::boolean)
			ORDER BY priority_score DESC NULLS LAST, last_event_at DESC
			LIMIT $4 OFFSET $5
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, state, source, needsReply, limit, offset)
}

func (a *app) handleConversation(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/conversations/")
	query := `
		SELECT row_to_json(t)
		FROM (
			SELECT
				id,
				source,
				external_id,
				account_id,
				title,
				COALESCE(participants, '[]'::jsonb) AS participants,
				COALESCE(state, 'active') AS state,
				COALESCE(needs_read, FALSE) AS needs_read,
				COALESCE(needs_reply, FALSE) AS needs_reply,
				COALESCE(important_now, FALSE) AS important_now,
				COALESCE(waiting_on_other, FALSE) AS waiting_on_other,
				COALESCE(follow_up_later, FALSE) AS follow_up_later,
				COALESCE(ready_to_act, FALSE) AS ready_to_act,
				COALESCE(blocked_needs_context, FALSE) AS blocked_needs_context,
				last_event_at,
				last_triaged_at,
				priority_score::double precision AS priority_score,
				urgency_score::double precision AS urgency_score,
				social_weight::double precision AS social_weight,
				reward_value::double precision AS reward_value,
				energy_fit::double precision AS energy_fit,
				effort_estimate_minutes,
				due_at,
				COALESCE(metadata, '{}'::jsonb) AS metadata,
				created_at,
				updated_at
			FROM life_radar.conversations
			WHERE id = $1::uuid
		) t
	`
	a.respondSingleRowJSON(w, r.Context(), query, id)
}

func (a *app) handleMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	offset := intQuery(r, "offset", 0)
	source := strings.TrimSpace(r.URL.Query().Get("source"))
	conversationID := strings.TrimSpace(r.URL.Query().Get("conversation_id"))
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				id,
				conversation_id,
				source,
				external_id,
				sender_id,
				sender_label,
				occurred_at,
				content_text,
				COALESCE(content_json, '{}'::jsonb) AS content_json,
				COALESCE(is_inbound, TRUE) AS is_inbound,
				reply_needed,
				needs_read,
				needs_reply,
				importance_score::double precision AS importance_score,
				triage_summary,
				COALESCE(provenance, '{}'::jsonb) AS provenance,
				created_at,
				updated_at
			FROM life_radar.message_events
			WHERE ($1 = '' OR conversation_id = $1::uuid)
			  AND ($2 = '' OR source = $2)
			ORDER BY occurred_at DESC
			LIMIT $3 OFFSET $4
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, conversationID, source, limit, offset)
}

func (a *app) handleCommitments(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	status := strings.TrimSpace(r.URL.Query().Get("status"))
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT * FROM life_radar.commitments
			WHERE ($1 = '' OR status = $1)
			ORDER BY due_at ASC NULLS LAST
			LIMIT $2
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, status, limit)
}

func (a *app) handleReminders(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	status := strings.TrimSpace(r.URL.Query().Get("status"))
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT * FROM life_radar.reminders
			WHERE ($1 = '' OR status = $1)
			ORDER BY remind_at ASC
			LIMIT $2
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, status, limit)
}

func (a *app) handleTasks(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, false) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		limit := intQuery(r, "limit", 50)
		status := strings.TrimSpace(r.URL.Query().Get("status"))
		query := `
			SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
			FROM (
				SELECT * FROM life_radar.planned_actions
				WHERE ($1 = '' OR status = $1)
				ORDER BY scheduled_start ASC NULLS LAST
				LIMIT $2
			) t
		`
		a.respondQueryJSON(w, r.Context(), query, status, limit)
	case http.MethodPost:
		var body taskCreateRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			a.respondError(w, http.StatusBadRequest, "Invalid request body")
			return
		}
		query := `
			WITH ins AS (
				INSERT INTO life_radar.planned_actions
					(source_entity_type, title, summary, status, scheduled_start, scheduled_end, effort_estimate_minutes)
				VALUES ($1, $2, $3, $4, $5, $6, $7)
				RETURNING *
			)
			SELECT row_to_json(ins) FROM ins
		`
		a.respondSingleRowJSON(w, r.Context(), query, body.SourceEntityType, body.Title, body.Summary, defaultString(body.Status, "proposed"), body.ScheduledStart, body.ScheduledEnd, body.EffortEstimateMinutes)
	default:
		a.methodNotAllowed(w)
	}
}

func (a *app) handleCalendarEvents(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, false) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		limit := intQuery(r, "limit", 50)
		fromDate := parseOptionalTime(r.URL.Query().Get("from_date"))
		toDate := parseOptionalTime(r.URL.Query().Get("to_date"))
		if daysRaw := strings.TrimSpace(r.URL.Query().Get("days")); daysRaw != "" && toDate == nil {
			if days, err := strconv.Atoi(daysRaw); err == nil && days > 0 {
				if fromDate == nil {
					now := time.Now().UTC()
					fromDate = &now
				}
				t := fromDate.Add(time.Duration(days) * 24 * time.Hour)
				toDate = &t
			}
		}
		query := `
			SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
			FROM (
				SELECT
					id,
					title,
					summary,
					COALESCE(status, 'scheduled') AS status,
					scheduled_start,
					scheduled_end,
					calendar_provider,
					calendar_external_id,
					effort_estimate_minutes,
					created_at,
					updated_at
				FROM life_radar.planned_actions
				WHERE calendar_external_id IS NOT NULL
				  AND ($1::timestamptz IS NULL OR COALESCE(scheduled_end, scheduled_start) >= $1::timestamptz)
				  AND ($2::timestamptz IS NULL OR scheduled_start <= $2::timestamptz)
				ORDER BY scheduled_start ASC
				LIMIT $3
			) t
		`
		a.respondQueryJSON(w, r.Context(), query, fromDate, toDate, limit)
	case http.MethodPost:
		var body calendarEventUpsertRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			a.respondError(w, http.StatusBadRequest, "Invalid request body")
			return
		}
		var query string
		if body.CalendarExternalID != nil && strings.TrimSpace(*body.CalendarExternalID) != "" {
			query = `
				WITH upserted AS (
					INSERT INTO life_radar.planned_actions
						(title, summary, scheduled_start, scheduled_end, calendar_external_id, calendar_provider, source_entity_type, status)
					VALUES ($1, $2, $3, $4, $5, $6, 'calendar', 'scheduled')
					ON CONFLICT (calendar_external_id) DO UPDATE SET
						title = EXCLUDED.title,
						summary = EXCLUDED.summary,
						scheduled_start = EXCLUDED.scheduled_start,
						scheduled_end = EXCLUDED.scheduled_end,
						calendar_provider = EXCLUDED.calendar_provider
					RETURNING *
				)
				SELECT row_to_json(upserted) FROM upserted
			`
			a.respondSingleRowJSON(w, r.Context(), query, body.Title, body.Summary, body.ScheduledStart, body.ScheduledEnd, body.CalendarExternalID, body.CalendarProvider)
			return
		}
		query = `
			WITH ins AS (
				INSERT INTO life_radar.planned_actions
					(title, summary, scheduled_start, scheduled_end, calendar_provider, source_entity_type, status)
				VALUES ($1, $2, $3, $4, $5, 'calendar', 'scheduled')
				RETURNING *
			)
			SELECT row_to_json(ins) FROM ins
		`
		a.respondSingleRowJSON(w, r.Context(), query, body.Title, body.Summary, body.ScheduledStart, body.ScheduledEnd, body.CalendarProvider)
	default:
		a.methodNotAllowed(w)
	}
}

func (a *app) handleSendMessage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	var body messageSendRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		a.respondError(w, http.StatusBadRequest, "Invalid request body")
		return
	}
	if !body.UserApproved {
		a.respondError(w, http.StatusForbidden, "Explicit user approval is required before sending a message. Prompt the user for confirmation, then retry with user_approved=true and an approval_note describing that approval.")
		return
	}
	var payload []byte
	err := a.db.QueryRow(r.Context(), `
		SELECT row_to_json(t)
		FROM (
			SELECT id, source, external_id, COALESCE(metadata, '{}'::jsonb) AS metadata
			FROM life_radar.conversations
			WHERE id = $1::uuid
		) t
	`, body.ConversationID).Scan(&payload)
	if err != nil {
		a.respondError(w, http.StatusNotFound, "Conversation not found")
		return
	}
	var conversation map[string]any
	if err := json.Unmarshal(payload, &conversation); err != nil {
		a.respondError(w, http.StatusInternalServerError, "Could not decode conversation metadata")
		return
	}
	metadata, _ := conversation["metadata"].(map[string]any)
	if metadata != nil && metadata["transport"] == "beeper_desktop" {
		// Hard send gate: only Beeper-backed Telegram Saved Messages may be sent.
		// Require explicit evidence this is the Saved Messages chat, not just any Beeper chat.
		source, _ := conversation["source"].(string)
		title, _ := conversation["title"].(string)
		if !(strings.EqualFold(source, "telegram") && strings.EqualFold(title, "Telegram Saved Messages")) {
			a.respondError(w, http.StatusForbidden, "Sending is restricted: only the Telegram Saved Messages conversation (Beeper-backed) may receive outbound messages. All other conversations, including other Beeper-backed chats, are blocked.")
			return
		}
		payload := map[string]any{
			"external_id":     conversation["external_id"],
			"content_text":    body.ContentText,
			"conversation_id": body.ConversationID,
		}
		data, err := a.requestJSON(r.Context(), http.MethodPost, a.cfg.messagingRuntimeURL+"/send", payload, nil)
		if err != nil {
			a.respondUpstreamError(w, err, "Messaging runtime error")
			return
		}
		messageID, _ := data["message_id"].(string)
		if messageID == "" {
			a.respondError(w, http.StatusBadGateway, "Messaging runtime did not return a message_id")
			return
		}
		a.respondJSON(w, http.StatusOK, map[string]any{"status": "sent", "message_id": messageID})
		return
	}
	a.respondError(w, http.StatusNotImplemented, "This conversation is not backed by the Beeper runtime. Legacy transports remain queryable for history, but sending is only supported for conversations with metadata.transport='beeper_desktop'.")
}

func (a *app) handleMemories(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	limit := intQuery(r, "limit", 50)
	kind := strings.TrimSpace(r.URL.Query().Get("kind"))
	subjectType := strings.TrimSpace(r.URL.Query().Get("subject_type"))
	var active any = true
	if raw := strings.TrimSpace(r.URL.Query().Get("active")); raw != "" {
		if strings.EqualFold(raw, "true") || strings.EqualFold(raw, "false") {
			active = strings.EqualFold(raw, "true")
		}
	}
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				id, kind, subject_type, subject_key, title, summary, detail,
				COALESCE(sensitivity, 'normal') AS sensitivity,
				confidence, COALESCE(active, TRUE) AS active, source_event_id,
				COALESCE(provenance, '{}'::jsonb) AS provenance,
				created_at, updated_at
			FROM life_radar.memory_records
			WHERE ($1 = '' OR kind = $1)
			  AND ($2 = '' OR subject_type = $2)
			  AND ($3::boolean IS NULL OR active = $3::boolean)
			ORDER BY updated_at DESC
			LIMIT $4
		) t
	`
	a.respondQueryJSON(w, r.Context(), query, kind, subjectType, active, limit)
}

func (a *app) handleProbeStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				id, candidate_id, candidate_type, COALESCE(status, 'ok') AS status,
				observed_at, latency_ms, freshness_seconds, total_events,
				decrypt_failures, encrypted_non_text, running_processes,
				COALESCE(metadata, '{}'::jsonb) AS metadata, notes
			FROM life_radar.runtime_probes
			ORDER BY observed_at DESC
			LIMIT 20
		) t
	`
	a.respondQueryJSON(w, r.Context(), query)
}

func (a *app) handleProbeCandidates(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	query := `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT
				candidate_id, candidate_type, COALESCE(last_status, 'ok') AS last_status,
				last_probe_at, latest_freshness_seconds, latest_total_events,
				latest_decrypt_failures, latest_encrypted_non_text, latest_running_processes,
				latest_notes, COALESCE(metadata, '{}'::jsonb) AS metadata, updated_at
			FROM life_radar.messaging_candidates
			ORDER BY last_probe_at DESC
		) t
	`
	a.respondQueryJSON(w, r.Context(), query)
}

func (a *app) handleSearch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		a.methodNotAllowed(w)
		return
	}
	if !a.requireAPIKey(w, r, false) {
		return
	}
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if q == "" {
		a.respondError(w, http.StatusBadRequest, "Query q is required")
		return
	}
	limit := intQuery(r, "limit", 20)
	likeQ := "%" + q + "%"
	results := make([]map[string]any, 0, limit)
	appendRows := func(raw []byte) {
		var items []map[string]any
		if err := json.Unmarshal(raw, &items); err == nil {
			results = append(results, items...)
		}
	}
	appendRows(a.mustQueryJSON(r.Context(), `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT id::text AS id, 'conversation' AS type, title AS subject, NULL::text AS body,
				   COALESCE(priority_score::double precision, 0) AS score
			FROM life_radar.conversations
			WHERE title ILIKE $1 OR external_id ILIKE $1
			LIMIT $2
		) t
	`, likeQ, limit))
	appendRows(a.mustQueryJSON(r.Context(), `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT id::text AS id, 'message' AS type, sender_label AS subject, content_text AS body, NULL::double precision AS score
			FROM life_radar.message_events
			WHERE content_text ILIKE $1
			ORDER BY occurred_at DESC
			LIMIT $2
		) t
	`, likeQ, limit))
	appendRows(a.mustQueryJSON(r.Context(), `
		SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)
		FROM (
			SELECT id::text AS id, 'memory' AS type, title AS subject, summary AS body,
				   confidence::double precision AS score
			FROM life_radar.memory_records
			WHERE title ILIKE $1 OR summary ILIKE $1 OR detail ILIKE $1
			LIMIT $2
		) t
	`, likeQ, limit))
	if len(results) > limit {
		results = results[:limit]
	}
	a.respondJSON(w, http.StatusOK, results)
}

func (a *app) handleMCP(w http.ResponseWriter, r *http.Request) {
	if !a.requireAPIKey(w, r, false) {
		return
	}
	target, _ := url.Parse(a.cfg.mcpURL)
	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		originalDirector(req)
		req.URL.Path = strings.TrimPrefix(r.URL.Path, "/mcp")
		if req.URL.Path == "" {
			req.URL.Path = "/"
		}
		req.Host = target.Host
	}
	proxy.ErrorHandler = func(rw http.ResponseWriter, req *http.Request, err error) {
		a.respondError(rw, http.StatusServiceUnavailable, "MCP server unavailable")
	}
	proxy.ServeHTTP(w, r)
}

func (a *app) proxyJSON(w http.ResponseWriter, r *http.Request, endpoint string, payload any) {
	data, err := a.requestRaw(r.Context(), r.Method, endpoint, payload, cloneHeaders(r.Header))
	if err != nil {
		a.respondUpstreamError(w, err, "Upstream request failed")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func (a *app) requestJSON(ctx context.Context, method, endpoint string, payload any, headers http.Header) (map[string]any, error) {
	data, err := a.requestRaw(ctx, method, endpoint, payload, headers)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if len(data) == 0 {
		return map[string]any{}, nil
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func (a *app) requestRaw(ctx context.Context, method, endpoint string, payload any, headers http.Header) ([]byte, error) {
	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return nil, err
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for key, values := range headers {
		if strings.EqualFold(key, "Host") {
			continue
		}
		for _, value := range values {
			req.Header.Add(key, value)
		}
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
		return nil, &httpError{statusCode: resp.StatusCode, message: strings.TrimSpace(string(data))}
	}
	return data, nil
}

func (a *app) respondQueryJSON(w http.ResponseWriter, ctx context.Context, query string, args ...any) {
	data := a.mustQueryJSON(ctx, query, args...)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func (a *app) mustQueryJSON(ctx context.Context, query string, args ...any) []byte {
	var raw []byte
	err := a.db.QueryRow(ctx, query, args...).Scan(&raw)
	if err != nil {
		a.logger.Printf("query failed: %v", err)
		return []byte("[]")
	}
	if len(raw) == 0 {
		return []byte("[]")
	}
	return raw
}

func (a *app) respondSingleRowJSON(w http.ResponseWriter, ctx context.Context, query string, args ...any) {
	var raw []byte
	err := a.db.QueryRow(ctx, query, args...).Scan(&raw)
	if err != nil || len(raw) == 0 || string(raw) == "null" {
		a.respondError(w, http.StatusNotFound, "Conversation not found")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func (a *app) requireAPIKey(w http.ResponseWriter, r *http.Request, allowQuery bool) bool {
	if a.cfg.apiKey == "" {
		return true
	}
	provided := strings.TrimSpace(r.Header.Get("X-API-Key"))
	if provided == "" {
		auth := strings.TrimSpace(r.Header.Get("Authorization"))
		if strings.HasPrefix(strings.ToLower(auth), "bearer ") {
			provided = strings.TrimSpace(auth[7:])
		}
	}
	if provided == "" && allowQuery {
		provided = strings.TrimSpace(r.URL.Query().Get("api_key"))
	}
	if subtle.ConstantTimeCompare([]byte(provided), []byte(a.cfg.apiKey)) != 1 {
		a.respondError(w, http.StatusUnauthorized, "Missing or invalid API key")
		return false
	}
	return true
}

func (a *app) readMatrixSession() (matrixSessionFile, bool) {
	var session matrixSessionFile
	data, err := os.ReadFile(a.cfg.matrixSessionPath)
	if err != nil {
		return session, false
	}
	if err := json.Unmarshal(data, &session); err != nil {
		return session, false
	}
	return session, true
}

func (a *app) writeMatrixSession(payload matrixSessionFile) error {
	if err := os.MkdirAll(filepath.Dir(a.cfg.matrixSessionPath), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	tmp := a.cfg.matrixSessionPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, a.cfg.matrixSessionPath)
}

func (a *app) resetMatrixLocalIdentityState() {
	_ = os.Remove(a.cfg.matrixSessionPath)
	_ = os.RemoveAll(a.cfg.matrixStorePath)
}

func (a *app) ensureValidMatrixSession(ctx context.Context) (matrixSessionFile, error) {
	session, ok := a.readMatrixSession()
	if !ok {
		return matrixSessionFile{}, &httpError{statusCode: http.StatusUnauthorized, message: "No local Matrix session. Sign in again."}
	}
	if _, err := a.matrixWhoAmI(ctx, session.AccessToken, session.Homeserver); err != nil {
		if code, _ := statusCodeFromMatrixError(err); code == http.StatusUnauthorized {
			a.resetMatrixLocalIdentityState()
			return matrixSessionFile{}, &httpError{statusCode: http.StatusUnauthorized, message: "Matrix session expired or was revoked. Sign in again."}
		}
		return matrixSessionFile{}, err
	}
	return session, nil
}

func (a *app) matrixWhoAmI(ctx context.Context, accessToken, homeserver string) (map[string]any, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(homeserver, "/")+"/_matrix/client/v3/account/whoami", nil)
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, &httpError{statusCode: resp.StatusCode, message: extractMatrixError(body, "Matrix session is invalid")}
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

func (a *app) performMatrixPasswordLogin(ctx context.Context, body matrixLoginRequest) (map[string]any, error) {
	password := strings.TrimSpace(body.Password)
	if password == "" {
		return nil, &httpError{statusCode: http.StatusBadRequest, message: "Password is required"}
	}
	identifier, err := detectMatrixIdentifier(body.Identifier, body.IdentifierKind)
	if err != nil {
		return nil, err
	}
	requestBody := map[string]any{
		"type":                        "m.login.password",
		"identifier":                  identifier,
		"password":                    password,
		"initial_device_display_name": defaultString(body.InitialDeviceDisplayName, "LifeRadar Matrix"),
	}
	raw, err := a.requestRaw(ctx, http.MethodPost, a.cfg.matrixHomeserverURL+"/_matrix/client/v3/login", requestBody, nil)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	accessToken, _ := payload["access_token"].(string)
	userID, _ := payload["user_id"].(string)
	deviceID, _ := payload["device_id"].(string)
	if accessToken == "" || userID == "" || deviceID == "" {
		return nil, &httpError{statusCode: http.StatusBadGateway, message: "Matrix homeserver did not return a complete session"}
	}
	a.resetMatrixLocalIdentityState()
	session := matrixSessionFile{
		AccessToken:  accessToken,
		UserID:       userID,
		DeviceID:     deviceID,
		Homeserver:   a.cfg.matrixHomeserverURL,
		RefreshToken: optionalString(payload["refresh_token"]),
		SavedAt:      time.Now().UTC().Format(time.RFC3339),
		ExpiresIn:    payload["expires_in_ms"],
	}
	if err := a.writeMatrixSession(session); err != nil {
		return nil, err
	}
	return map[string]any{
		"status":                "logged_in",
		"user_id":               userID,
		"device_id":             deviceID,
		"homeserver":            a.cfg.matrixHomeserverURL,
		"verification_required": true,
	}, nil
}

func (a *app) matrixListDevices(ctx context.Context, session matrixSessionFile) ([]map[string]any, error) {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(session.Homeserver, "/")+"/_matrix/client/v3/devices", nil)
	req.Header.Set("Authorization", "Bearer "+session.AccessToken)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, &httpError{statusCode: resp.StatusCode, message: extractMatrixError(body, "Could not load Matrix devices")}
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, err
	}
	items, _ := payload["devices"].([]any)
	devices := make([]map[string]any, 0, len(items))
	for _, raw := range items {
		device, _ := raw.(map[string]any)
		if device == nil {
			continue
		}
		deviceID, _ := device["device_id"].(string)
		devices = append(devices, map[string]any{
			"device_id":           deviceID,
			"display_name":        device["display_name"],
			"last_seen_ip":        device["last_seen_ip"],
			"last_seen_ts":        millisToRFC3339(device["last_seen_ts"]),
			"is_current":          deviceID == session.DeviceID,
			"is_verified":         nil,
			"supports_encryption": true,
		})
	}
	return devices, nil
}

func detectMatrixIdentifier(rawIdentifier, identifierKind string) (map[string]any, error) {
	identifier := strings.TrimSpace(rawIdentifier)
	if identifier == "" {
		return nil, &httpError{statusCode: http.StatusBadRequest, message: "Identifier is required"}
	}
	switch strings.ToLower(strings.TrimSpace(identifierKind)) {
	case "email":
		if !strings.Contains(identifier, "@") {
			return nil, &httpError{statusCode: http.StatusBadRequest, message: "Enter a valid email address"}
		}
		return map[string]any{"type": "m.id.thirdparty", "medium": "email", "address": strings.ToLower(identifier)}, nil
	case "matrix_id", "matrix", "username", "":
	default:
		return nil, &httpError{statusCode: http.StatusBadRequest, message: "Unsupported Matrix identifier type"}
	}
	if strings.Contains(identifier, "@") && !strings.HasPrefix(identifier, "@") {
		return map[string]any{"type": "m.id.thirdparty", "medium": "email", "address": strings.ToLower(identifier)}, nil
	}
	return map[string]any{"type": "m.id.user", "user": identifier}, nil
}

func cloneHeaders(src http.Header) http.Header {
	dst := make(http.Header, len(src))
	for key, values := range src {
		cp := make([]string, len(values))
		copy(cp, values)
		dst[key] = cp
	}
	return dst
}

func parseOptionalTime(raw string) *time.Time {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		return &t
	}
	if t, err := time.Parse("2006-01-02", raw); err == nil {
		utc := t.UTC()
		return &utc
	}
	return nil
}

func millisToRFC3339(value any) any {
	switch v := value.(type) {
	case float64:
		return time.UnixMilli(int64(v)).UTC().Format(time.RFC3339)
	case int64:
		return time.UnixMilli(v).UTC().Format(time.RFC3339)
	default:
		return nil
	}
}

func intQuery(r *http.Request, key string, fallback int) int {
	if raw := strings.TrimSpace(r.URL.Query().Get(key)); raw != "" {
		if v, err := strconv.Atoi(raw); err == nil {
			return v
		}
	}
	return fallback
}

func defaultString(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func optionalString(value any) *string {
	if s, ok := value.(string); ok && strings.TrimSpace(s) != "" {
		return &s
	}
	return nil
}

func extractMatrixError(body []byte, fallback string) string {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err == nil {
		if message, ok := payload["error"].(string); ok && strings.TrimSpace(message) != "" {
			return message
		}
	}
	text := strings.TrimSpace(string(body))
	if text != "" {
		return text
	}
	return fallback
}

func (a *app) methodNotAllowed(w http.ResponseWriter) {
	a.respondError(w, http.StatusMethodNotAllowed, "method not allowed")
}

func (a *app) respondJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func (a *app) respondError(w http.ResponseWriter, status int, detail string) {
	a.respondJSON(w, status, map[string]any{"detail": detail})
}

func (a *app) respondUpstreamError(w http.ResponseWriter, err error, fallback string) {
	if code, ok := statusCodeFromMatrixError(err); ok {
		a.respondError(w, code, err.Error())
		return
	}
	a.respondError(w, http.StatusBadGateway, fallback)
}

func statusCodeFromMatrixError(err error) (int, bool) {
	var httpErr *httpError
	if errors.As(err, &httpErr) {
		return httpErr.statusCode, true
	}
	return 0, false
}

type httpError struct {
	statusCode int
	message    string
}

func (e *httpError) Error() string {
	return e.message
}

const matrixVerificationHTML = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>LifeRadar Matrix Device Verification</title></head>
<body>
<main>
<h1>Matrix Device Verification</h1>
<p>Choose a trusted device and compare the emoji verification when prompted.</p>
<section>
<h2>Sign In</h2>
<label>Username</label>
<label>Email</label>
<label>Matrix ID</label>
</section>
<section>
<h2>Choose a trusted device</h2>
</section>
<section>
<h2>Compare These Emojis</h2>
</section>
</main>
</body>
</html>`
