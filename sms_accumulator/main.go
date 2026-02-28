// SMS/RCS message accumulator using Google Messages web pairing protocol.
// Pairs with Google Messages via QR code, receives all messages (both directions),
// stores in SQLite, and exposes an HTTP query API compatible with the Signal accumulator.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/rs/zerolog"
	"github.com/skip2/go-qrcode"
	"go.mau.fi/mautrix-gmessages/pkg/libgm"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/events"
	"go.mau.fi/mautrix-gmessages/pkg/libgm/gmproto"
)

const (
	dataDir       = "/data"
	authFile      = dataDir + "/auth.json"
	dbFile        = dataDir + "/sms_messages.db"
	listenAddr    = ":8082"
	retentionDays = 30
)

var (
	log    zerolog.Logger
	db     *sql.DB
	client *libgm.Client
	dbMu   sync.Mutex

	// participantID → phone number / name caches (populated from conversations)
	participantCache     = map[string]string{}
	participantNameCache = map[string]string{}
	participantCacheMu   sync.RWMutex
)

// --- Database ---

func initDB() {
	os.MkdirAll(dataDir, 0755)
	var err error
	db, err = sql.Open("sqlite3", dbFile+"?_journal_mode=WAL")
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to open database")
	}
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS messages (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			message_id TEXT UNIQUE,
			timestamp_ms INTEGER,
			sender TEXT,
			sender_name TEXT,
			message TEXT,
			direction TEXT,
			conversation_id TEXT,
			created_at TEXT DEFAULT CURRENT_TIMESTAMP
		);
		CREATE INDEX IF NOT EXISTS idx_ts ON messages(timestamp_ms);
		CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_id ON messages(message_id);
	`)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to init database")
	}
}

func storeMessage(msgID string, timestampMs int64, sender, senderName, message, direction, conversationID string) {
	dbMu.Lock()
	defer dbMu.Unlock()
	_, err := db.Exec(
		"INSERT OR IGNORE INTO messages (message_id, timestamp_ms, sender, sender_name, message, direction, conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
		msgID, timestampMs, sender, senderName, message, direction, conversationID,
	)
	if err != nil {
		log.Error().Err(err).Msg("Failed to store message")
		return
	}
	log.Info().Str("direction", direction).Str("sender", sender).Str("sender_name", senderName).
		Str("message", truncate(message, 80)).Msg("Stored message")
}

func cleanupOldMessages() {
	for {
		cutoffMs := time.Now().UTC().Add(-time.Duration(retentionDays) * 24 * time.Hour).UnixMilli()
		dbMu.Lock()
		res, err := db.Exec("DELETE FROM messages WHERE timestamp_ms < ?", cutoffMs)
		dbMu.Unlock()
		if err != nil {
			log.Error().Err(err).Msg("Cleanup error")
		} else if n, _ := res.RowsAffected(); n > 0 {
			log.Info().Int64("deleted", n).Msg("Cleaned up old messages")
		}
		time.Sleep(time.Hour)
	}
}

// --- Auth persistence ---

func saveAuth(ad *libgm.AuthData) {
	f, err := os.OpenFile(authFile, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0600)
	if err != nil {
		log.Error().Err(err).Msg("Failed to save auth")
		return
	}
	defer f.Close()
	json.NewEncoder(f).Encode(ad)
	log.Info().Msg("Auth data saved")
}

func loadAuth() (*libgm.AuthData, error) {
	f, err := os.Open(authFile)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var ad libgm.AuthData
	err = json.NewDecoder(f).Decode(&ad)
	return &ad, err
}

// --- Event handler ---

func handleEvent(rawEvt any) {
	switch evt := rawEvt.(type) {
	case *libgm.WrappedMessage:
		processMessage(evt)

	case *gmproto.Conversation:
		// Cache participant info from conversations
		cacheConversationParticipants(evt)

	case *events.ClientReady:
		log.Info().Str("session_id", evt.SessionID).Int("conversations", len(evt.Conversations)).Msg("Client ready")
		for _, conv := range evt.Conversations {
			cacheConversationParticipants(conv)
		}
		go backfillMessages(evt.Conversations)

	case *events.AuthTokenRefreshed:
		saveAuth(client.AuthData)

	case *events.PairSuccessful:
		log.Info().Str("phone_id", evt.PhoneID).Msg("Pairing successful")
		saveAuth(client.AuthData)

	case *events.ListenFatalError:
		log.Error().Err(evt.Error).Msg("Fatal error — need to re-pair")

	case *events.ListenTemporaryError:
		log.Warn().Err(evt.Error).Msg("Temporary error (will retry)")

	case *events.ListenRecovered:
		log.Info().Msg("Connection recovered")

	case *events.PhoneNotResponding:
		log.Warn().Msg("Phone not responding")

	case *events.PhoneRespondingAgain:
		log.Info().Msg("Phone responding again")

	case *gmproto.RevokePairData:
		log.Warn().Msg("Phone revoked pairing")
		os.Remove(authFile)
	}
}

func processMessage(evt *libgm.WrappedMessage) {
	// WrappedMessage embeds *gmproto.Message directly
	msg := evt.Message

	statusType := msg.GetMessageStatus().GetStatus()
	// Skip tombstone/system events and deletes
	if statusType >= 200 {
		return
	}

	direction := "unknown"
	if statusType >= 1 && statusType < 100 {
		direction = "outgoing"
	} else if statusType >= 100 && statusType < 200 {
		direction = "incoming"
	}

	// Timestamps in libgm are microseconds
	timestampMs := msg.GetTimestamp() / 1000

	var body string
	for _, info := range msg.GetMessageInfo() {
		switch data := info.GetData().(type) {
		case *gmproto.MessageInfo_MessageContent:
			body = data.MessageContent.GetContent()
		case *gmproto.MessageInfo_MediaContent:
			if body == "" {
				body = fmt.Sprintf("[media: %s]", data.MediaContent.GetMimeType())
			}
		}
	}
	if body == "" {
		return
	}

	// Try to resolve participant ID to phone number and name
	participantID := msg.GetParticipantID()
	sender := participantID
	senderName := ""

	participantCacheMu.RLock()
	if num, ok := participantCache[participantID]; ok {
		sender = num
	}
	if name, ok := participantNameCache[participantID]; ok {
		senderName = name
	}
	participantCacheMu.RUnlock()

	convID := msg.GetConversationID()
	msgID := msg.GetMessageID()

	storeMessage(msgID, timestampMs, sender, senderName, body, direction, convID)
}

func cacheConversationParticipants(conv *gmproto.Conversation) {
	participantCacheMu.Lock()
	defer participantCacheMu.Unlock()
	for _, p := range conv.GetParticipants() {
		if p.GetID() == nil {
			continue
		}
		// msg.GetParticipantID() returns the participantID string, not the phone number
		pid := p.GetID().GetParticipantID()
		if pid == "" {
			continue
		}
		// Resolve to phone number: prefer ID.Number, fall back to FormattedNumber
		if num := p.GetID().GetNumber(); num != "" {
			participantCache[pid] = num
		} else if fNum := p.GetFormattedNumber(); fNum != "" {
			participantCache[pid] = fNum
		}
		if name := p.GetFullName(); name != "" {
			participantNameCache[pid] = name
		}
		log.Debug().Str("participant_id", pid).
			Str("number", p.GetID().GetNumber()).
			Str("formatted", p.GetFormattedNumber()).
			Str("name", p.GetFullName()).
			Bool("is_me", p.GetIsMe()).
			Msg("Cached participant")
	}
}

// --- Backfill ---

func backfillMessages(conversations []*gmproto.Conversation) {
	cutoff := time.Now().Add(-time.Duration(retentionDays) * 24 * time.Hour).UnixMicro()
	total := 0
	for _, conv := range conversations {
		convID := conv.GetConversationID()
		// Skip conversations with no recent activity
		if conv.GetLastMessageTimestamp() < cutoff {
			continue
		}
		resp, err := client.FetchMessages(convID, 50, nil)
		if err != nil {
			log.Warn().Err(err).Str("conv", convID).Msg("Failed to fetch messages for backfill")
			continue
		}
		for _, msg := range resp.GetMessages() {
			if msg.GetTimestamp() < cutoff {
				continue
			}
			statusType := msg.GetMessageStatus().GetStatus()
			if statusType >= 200 {
				continue
			}
			direction := "unknown"
			if statusType >= 1 && statusType < 100 {
				direction = "outgoing"
			} else if statusType >= 100 && statusType < 200 {
				direction = "incoming"
			}
			timestampMs := msg.GetTimestamp() / 1000
			var body string
			for _, info := range msg.GetMessageInfo() {
				switch data := info.GetData().(type) {
				case *gmproto.MessageInfo_MessageContent:
					body = data.MessageContent.GetContent()
				case *gmproto.MessageInfo_MediaContent:
					if body == "" {
						body = fmt.Sprintf("[media: %s]", data.MediaContent.GetMimeType())
					}
				}
			}
			if body == "" {
				continue
			}
			pid := msg.GetParticipantID()
			sender := pid
			senderName := ""
			participantCacheMu.RLock()
			if num, ok := participantCache[pid]; ok {
				sender = num
			}
			if name, ok := participantNameCache[pid]; ok {
				senderName = name
			}
			participantCacheMu.RUnlock()
			storeMessage(msg.GetMessageID(), timestampMs, sender, senderName, body, direction, convID)
			total++
		}
	}
	log.Info().Int("messages", total).Msg("Backfill complete")
}

// --- HTTP API ---

func startHTTPServer() {
	mux := http.NewServeMux()
	mux.HandleFunc("/messages", handleMessages)
	mux.HandleFunc("/health", handleHealth)

	log.Info().Str("addr", listenAddr).Msg("Starting HTTP server")
	if err := http.ListenAndServe(listenAddr, mux); err != nil {
		log.Fatal().Err(err).Msg("HTTP server failed")
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func handleMessages(w http.ResponseWriter, r *http.Request) {
	hours, _ := strconv.ParseFloat(r.URL.Query().Get("hours"), 64)
	if hours <= 0 {
		hours = 24
	}
	sender := r.URL.Query().Get("sender")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit <= 0 {
		limit = 100
	}

	sinceMs := time.Now().UTC().Add(-time.Duration(hours * float64(time.Hour))).UnixMilli()

	query := "SELECT timestamp_ms, sender, sender_name, message, direction, conversation_id FROM messages WHERE timestamp_ms >= ?"
	params := []any{sinceMs}

	if sender != "" {
		query += " AND (sender LIKE ? OR sender_name LIKE ?)"
		params = append(params, "%"+sender+"%", "%"+sender+"%")
	}

	query += " ORDER BY timestamp_ms DESC LIMIT ?"
	params = append(params, limit)

	dbMu.Lock()
	rows, err := db.Query(query, params...)
	dbMu.Unlock()
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	defer rows.Close()

	type messageJSON struct {
		Timestamp      string `json:"timestamp"`
		Sender         string `json:"sender"`
		SenderName     string `json:"sender_name"`
		Message        string `json:"message"`
		Direction      string `json:"direction"`
		ConversationID string `json:"conversation_id"`
	}

	var messages []messageJSON
	for rows.Next() {
		var tsMs int64
		var m messageJSON
		rows.Scan(&tsMs, &m.Sender, &m.SenderName, &m.Message, &m.Direction, &m.ConversationID)
		m.Timestamp = time.UnixMilli(tsMs).UTC().Format(time.RFC3339)
		messages = append(messages, m)
	}
	if messages == nil {
		messages = []messageJSON{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(messages)
}

// --- QR Pairing ---

func doPairing(ctx context.Context) error {
	pairDone := make(chan struct{})
	callback := func(data *gmproto.PairedData) {
		close(pairDone)
	}
	client.PairCallback.Store(&callback)

	if _, err := client.FetchConfig(); err != nil {
		return fmt.Errorf("fetch config: %w", err)
	}

	qrURL, err := client.StartLogin()
	if err != nil {
		return fmt.Errorf("start login: %w", err)
	}
	printQR(qrURL)

	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-pairDone:
			log.Info().Msg("Pairing complete")
			return nil
		case <-ticker.C:
			qrURL, err = client.RefreshPhoneRelay()
			if err != nil {
				return fmt.Errorf("refresh QR: %w", err)
			}
			printQR(qrURL)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func printQR(url string) {
	qr, err := qrcode.New(url, qrcode.Medium)
	if err != nil {
		log.Error().Err(err).Msg("Failed to generate QR code")
		fmt.Printf("Scan this URL with Google Messages:\n%s\n", url)
		return
	}
	fmt.Println("\nScan this QR code with Google Messages → Device Pairing → QR Scanner:")
	fmt.Println(qr.ToSmallString(false))
}

// --- Main ---

func main() {
	output := zerolog.ConsoleWriter{Out: os.Stderr, TimeFormat: time.RFC3339}
	log = zerolog.New(output).With().Timestamp().Logger()

	initDB()
	go cleanupOldMessages()
	go startHTTPServer()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	authData, err := loadAuth()
	needsLogin := err != nil

	if needsLogin {
		authData = libgm.NewAuthData()
	}

	client = libgm.NewClient(authData, log)
	client.SetEventHandler(handleEvent)

	if needsLogin {
		if err := doPairing(ctx); err != nil {
			log.Fatal().Err(err).Msg("Pairing failed")
		}
		time.Sleep(2 * time.Second)
	}

	if _, err := client.FetchConfig(); err != nil {
		log.Warn().Err(err).Msg("Failed to fetch config")
	}
	if err := client.Connect(); err != nil {
		log.Fatal().Err(err).Msg("Failed to connect")
	}
	log.Info().Msg("Connected to Google Messages")

	// Backfill: list conversations, cache participants, fetch last 24h of messages
	go func() {
		time.Sleep(3 * time.Second) // wait for initial sync
		resp, err := client.ListConversations(100, gmproto.ListConversationsRequest_INBOX)
		if err != nil {
			log.Error().Err(err).Msg("Failed to list conversations for backfill")
			return
		}
		convs := resp.GetConversations()
		for _, conv := range convs {
			cacheConversationParticipants(conv)
		}
		log.Info().Int("conversations", len(convs)).Msg("Cached participants from conversations")
		backfillMessages(convs)
	}()

	// Wait for shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	<-sigCh

	log.Info().Msg("Shutting down")
	client.Disconnect()
	saveAuth(client.AuthData)
	db.Close()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
