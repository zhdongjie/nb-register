package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"

	"whatsappotprelay/pb"
)

const (
	defaultOTPPurpose = "local/gopay"
	maxPayloadBytes   = 64 * 1024
)

var (
	nonDigitRe         = regexp.MustCompile(`[^0-9]+`)
	keywordBeforeOTPRe = regexp.MustCompile(`(?is)(?:otp|one[-[:space:]]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)[^0-9]{0,80}([0-9]{4,8})`)
	otpBeforeKeywordRe = regexp.MustCompile(`(?is)(?:^|[^0-9])([0-9]{4,8})[^0-9]{0,80}(?:otp|one[-[:space:]]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)`)
	bareOTPRe          = regexp.MustCompile(`(?:^|[^0-9])([0-9]{4,8})(?:[^0-9]|$)`)
	otpRouteSegmentRe  = regexp.MustCompile(`^[A-Za-z0-9:_-]+$`)
)

type otpItem struct {
	OTP     string
	Source  string
	Purpose string
	Ts      int64
	Hint    string
}

type otpStore struct {
	mu         sync.Mutex
	items      []otpItem
	notify     chan struct{}
	maxItems   int
	ttlSeconds int64
}

func newOTPStore(maxItems, ttlSeconds int) *otpStore {
	if maxItems <= 0 {
		maxItems = 100
	}
	if ttlSeconds < 30 {
		ttlSeconds = 600
	}
	return &otpStore{
		notify:     make(chan struct{}),
		maxItems:   maxItems,
		ttlSeconds: int64(ttlSeconds),
	}
}

func (s *otpStore) submit(otp string, source string, purpose string, issuedAtUnix int64, hint string) error {
	code := cleanOTPCandidate(otp)
	if !regexp.MustCompile(`^[0-9]{4,8}$`).MatchString(code) {
		return errors.New("otp must be 4-8 digits")
	}
	if issuedAtUnix <= 0 {
		issuedAtUnix = time.Now().Unix()
	}
	item := otpItem{
		OTP:     code,
		Source:  truncate(strings.TrimSpace(source), 80, "webhook"),
		Purpose: strings.ToLower(truncate(strings.TrimSpace(purpose), 80, "")),
		Ts:      issuedAtUnix,
		Hint:    truncate(hint, 512, ""),
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.items = append(s.items, item)
	s.purgeLocked()
	close(s.notify)
	s.notify = make(chan struct{})
	return nil
}

func (s *otpStore) wait(ctx context.Context, purpose string, timeout time.Duration, issuedAfterUnix int64) (otpItem, bool) {
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	purpose = strings.ToLower(strings.TrimSpace(purpose))
	if purpose == "" {
		purpose = defaultOTPPurpose
	}

	for {
		s.mu.Lock()
		s.purgeLocked()
		for i, item := range s.items {
			if issuedAfterUnix > 0 && item.Ts < issuedAfterUnix {
				continue
			}
			if itemMatchesPurpose(item, purpose) {
				s.items = append(s.items[:i], s.items[i+1:]...)
				s.mu.Unlock()
				return item, true
			}
		}
		notify := s.notify
		s.mu.Unlock()

		select {
		case <-ctx.Done():
			return otpItem{}, false
		case <-notify:
		}
	}
}

func (s *otpStore) snapshot() []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.purgeLocked()
	out := make([]map[string]any, 0, len(s.items))
	for _, item := range s.items {
		out = append(out, map[string]any{
			"otp":     "<redacted>",
			"source":  item.Source,
			"purpose": item.Purpose,
			"ts":      item.Ts,
			"hint":    item.Hint,
		})
	}
	return out
}

func (s *otpStore) purgeLocked() {
	cutoff := time.Now().Unix() - s.ttlSeconds
	kept := s.items[:0]
	for _, item := range s.items {
		if item.Ts >= cutoff {
			kept = append(kept, item)
		}
	}
	s.items = kept
	if len(s.items) > s.maxItems {
		s.items = append([]otpItem(nil), s.items[len(s.items)-s.maxItems:]...)
	}
}

type otpService struct {
	pb.UnimplementedOtpServiceServer
	store *otpStore
}

func (s *otpService) WaitForOtp(ctx context.Context, req *pb.WaitForOtpRequest) (*pb.WaitForOtpResponse, error) {
	timeoutSeconds := req.GetTimeoutSeconds()
	if timeoutSeconds <= 0 {
		timeoutSeconds = 60
	}
	purpose := strings.TrimSpace(req.GetPurpose())
	if purpose == "" {
		purpose = defaultOTPPurpose
	}
	log.Printf("[otp-relay] WaitForOtp purpose=%s timeout=%ds issued_after=%d", purpose, timeoutSeconds, req.GetIssuedAfterUnix())
	item, ok := s.store.wait(ctx, purpose, time.Duration(timeoutSeconds)*time.Second, req.GetIssuedAfterUnix())
	if !ok {
		return &pb.WaitForOtpResponse{
			Found:        false,
			ErrorMessage: fmt.Sprintf("timeout waiting for OTP after %ds", timeoutSeconds),
		}, nil
	}
	log.Printf("[otp-relay] OTP served source=%s purpose=%s", item.Source, purpose)
	return &pb.WaitForOtpResponse{Found: true, Otp: item.OTP, Source: item.Source}, nil
}

func newHTTPHandler(store *otpStore) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		switch path {
		case "/health", "/healthz":
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "cached": len(store.snapshot())})
			return
		case "/debug/cache":
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "items": store.snapshot()})
			return
		}
		purpose, ok := otpPurposeFromPath(path)
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "not found"})
			return
		}
		if r.Method != http.MethodPost && r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"ok": false, "error": "method not allowed"})
			return
		}
		handleSubmit(w, r, store, purpose)
	})
	return mux
}

func handleSubmit(w http.ResponseWriter, r *http.Request, store *otpStore, purpose string) {
	payload, err := requestPayload(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	code, payloadTS := extractOTPFromPayload(payload)
	source := payloadSource(payload, r.Header)
	if code == "" {
		log.Printf("[otp-relay] notification accepted without OTP source=%s", truncate(source, 80, "webhook"))
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "accepted": false, "message": "otp not found"})
		return
	}
	if isForwarderTestOTP(payload, code, source) {
		log.Printf("[otp-relay] test OTP ignored source=%s", truncate(source, 80, "webhook"))
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "accepted": false, "message": "test otp ignored"})
		return
	}
	if err := store.submit(code, source, purpose, payloadTS, payloadHint(payload)); err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	if payloadTS <= 0 {
		payloadTS = time.Now().Unix()
	}
	log.Printf("[otp-relay] OTP accepted source=%s ts=%d", truncate(source, 80, "webhook"), payloadTS)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "accepted": true})
}

func requestPayload(r *http.Request) (any, error) {
	if r.Method == http.MethodGet {
		return valuesToMap(r.URL.Query()), nil
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxPayloadBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxPayloadBytes {
		return nil, errors.New("payload too large")
	}
	raw := strings.TrimSpace(string(body))
	if raw == "" {
		return map[string]any{}, nil
	}
	contentType := strings.ToLower(r.Header.Get("Content-Type"))
	if strings.Contains(contentType, "json") {
		var payload any
		decoder := json.NewDecoder(strings.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&payload); err != nil {
			return raw, nil
		}
		return payload, nil
	}
	if strings.Contains(contentType, "x-www-form-urlencoded") {
		values, err := url.ParseQuery(raw)
		if err == nil {
			return valuesToMap(values), nil
		}
	}
	return raw, nil
}

func isForwarderTestOTP(payload any, code string, source string) bool {
	if code != "123456" || strings.ToLower(strings.TrimSpace(source)) != "whatsapp" {
		return false
	}
	obj, ok := payload.(map[string]any)
	if !ok {
		return false
	}
	if len(obj) > 2 {
		return false
	}
	if _, ok := obj["otp"]; !ok {
		return false
	}
	if _, ok := obj["source"]; !ok {
		return false
	}
	return true
}

func valuesToMap(values url.Values) map[string]any {
	out := make(map[string]any, len(values))
	for key, vals := range values {
		if len(vals) > 0 {
			out[key] = vals[0]
		}
	}
	return out
}

func extractOTPFromPayload(payload any) (string, int64) {
	code, ts, ok := walkPayload(payload, 0)
	if !ok {
		return "", 0
	}
	return code, ts
}

func walkPayload(payload any, inheritedTS int64) (string, int64, bool) {
	switch v := payload.(type) {
	case map[string]any:
		ts := dictTimestamp(v)
		if ts == 0 {
			ts = inheritedTS
		}
		pieces := payloadMessagePieces(v)
		if len(pieces) > 0 {
			if code := extractOTPFromText(strings.Join(pieces, " ")); code != "" {
				return code, ts, true
			}
		}
		for _, child := range v {
			if code, childTS, ok := walkPayload(child, ts); ok {
				return code, childTS, true
			}
		}
	case []any:
		for _, child := range v {
			if code, ts, ok := walkPayload(child, inheritedTS); ok {
				return code, ts, true
			}
		}
	case string:
		if code := extractOTPFromText(v); code != "" {
			return code, inheritedTS, true
		}
	}
	return "", 0, false
}

func payloadMessagePieces(obj map[string]any) []string {
	keys := []string{"otp", "code", "body", "message", "text", "content", "caption", "raw", "notification"}
	pieces := make([]string, 0, len(keys))
	for _, key := range keys {
		value, ok := obj[key]
		if !ok {
			continue
		}
		switch v := value.(type) {
		case map[string]any:
			for _, nestedKey := range []string{"body", "text", "message"} {
				if nested, ok := v[nestedKey]; ok {
					pieces = append(pieces, fmt.Sprint(nested))
					break
				}
			}
		case []any:
			for _, item := range v {
				pieces = append(pieces, fmt.Sprint(item))
			}
		default:
			pieces = append(pieces, fmt.Sprint(v))
		}
	}
	return pieces
}

func extractOTPFromText(text string) string {
	if strings.TrimSpace(text) == "" {
		return ""
	}
	for _, re := range []*regexp.Regexp{keywordBeforeOTPRe, otpBeforeKeywordRe, bareOTPRe} {
		matches := re.FindAllStringSubmatch(text, -1)
		for i := len(matches) - 1; i >= 0; i-- {
			for j := len(matches[i]) - 1; j >= 1; j-- {
				if code := cleanOTPCandidate(matches[i][j]); code != "" {
					return code
				}
			}
		}
	}
	return ""
}

func cleanOTPCandidate(value any) string {
	code := nonDigitRe.ReplaceAllString(fmt.Sprint(value), "")
	if len(code) >= 4 && len(code) <= 8 {
		return code
	}
	return ""
}

func otpPurposeFromPath(path string) (string, bool) {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) < 2 {
		return "", false
	}
	if !validOTPSourceSegment(parts[0]) {
		return "", false
	}
	for _, part := range parts {
		if !otpRouteSegmentRe.MatchString(part) {
			return "", false
		}
	}
	return strings.ToLower(strings.Join(parts, "/")), true
}

func validOTPSourceSegment(source string) bool {
	source = strings.ToLower(strings.TrimSpace(source))
	if source == "local" {
		return true
	}
	if !strings.HasPrefix(source, "tg:") {
		return false
	}
	userID := strings.TrimPrefix(source, "tg:")
	if userID == "" {
		return false
	}
	for _, r := range userID {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func dictTimestamp(obj map[string]any) int64 {
	for _, key := range []string{
		"issued_at_unix",
		"timestamp",
		"timestamp_unix",
		"ts",
		"time",
		"date",
		"received_at",
		"received_at_unix",
		"postTime",
		"when",
	} {
		if value, ok := obj[key]; ok {
			if ts := parsePayloadTimestamp(value); ts > 0 {
				return ts
			}
		}
	}
	return 0
}

func parsePayloadTimestamp(value any) int64 {
	switch v := value.(type) {
	case nil:
		return 0
	case json.Number:
		if f, err := v.Float64(); err == nil {
			return normalizeUnixTimestamp(f)
		}
	case int:
		return normalizeUnixTimestamp(float64(v))
	case int64:
		return normalizeUnixTimestamp(float64(v))
	case float64:
		return normalizeUnixTimestamp(v)
	case string:
		text := strings.TrimSpace(v)
		if text == "" {
			return 0
		}
		if f, err := strconv.ParseFloat(text, 64); err == nil {
			return normalizeUnixTimestamp(f)
		}
		if ts, err := time.Parse(time.RFC3339, text); err == nil {
			return ts.Unix()
		}
	}
	return 0
}

func normalizeUnixTimestamp(ts float64) int64 {
	if ts > 1_000_000_000_000 {
		ts /= 1000
	}
	if ts >= 946684800 && ts <= 4102444800 {
		return int64(ts)
	}
	return 0
}

func payloadSource(payload any, headers http.Header) string {
	if obj, ok := payload.(map[string]any); ok {
		for _, key := range []string{"source", "from", "sender", "app", "appName", "packageName", "title"} {
			value := strings.TrimSpace(fmt.Sprint(obj[key]))
			if value != "" && value != "<nil>" {
				return value
			}
		}
	}
	return strings.TrimSpace(headers.Get("User-Agent"))
}

func payloadHint(payload any) string {
	switch v := payload.(type) {
	case string:
		return truncate(v, 512, "")
	default:
		b, err := json.Marshal(payload)
		if err == nil {
			return truncate(string(b), 512, "")
		}
		return truncate(fmt.Sprint(payload), 512, "")
	}
}

func itemMatchesPurpose(item otpItem, purpose string) bool {
	purpose = strings.ToLower(strings.TrimSpace(purpose))
	if purpose == "" || purpose == "any" {
		return true
	}
	itemPurpose := strings.ToLower(strings.TrimSpace(item.Purpose))
	if itemPurpose == "any" {
		return true
	}
	return itemPurpose == purpose
}

func writeJSON(w http.ResponseWriter, status int, payload map[string]any) {
	data, err := json.Marshal(payload)
	if err != nil {
		status = http.StatusInternalServerError
		data = []byte(`{"ok":false,"error":"json encode failed"}`)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(data)
}

func envDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func envInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	n, err := strconv.Atoi(value)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func truncate(value string, max int, fallback string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return fallback
	}
	if len(value) > max {
		return value[:max]
	}
	return value
}

func serve() error {
	store := newOTPStore(envInt("OTP_RELAY_MAX_ITEMS", 100), envInt("OTP_RELAY_TTL_SECONDS", 600))
	grpcListen := envDefault("OTP_RELAY_GRPC_LISTEN", ":50051")
	httpListen := envDefault("OTP_RELAY_HTTP_LISTEN", "0.0.0.0:8081")

	lis, err := net.Listen("tcp", grpcListen)
	if err != nil {
		return fmt.Errorf("listen grpc %s: %w", grpcListen, err)
	}
	grpcServer := grpc.NewServer()
	pb.RegisterOtpServiceServer(grpcServer, &otpService{store: store})

	httpServer := &http.Server{
		Addr:              httpListen,
		Handler:           newHTTPHandler(store),
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 2)
	go func() {
		log.Printf("[otp-relay] gRPC listening on %s", grpcListen)
		errCh <- grpcServer.Serve(lis)
	}()
	go func() {
		log.Printf("[otp-relay] HTTP listening on %s", httpListen)
		err := httpServer.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errCh <- err
	}()
	return <-errCh
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if err := serve(); err != nil {
		log.Fatal(err)
	}
}
