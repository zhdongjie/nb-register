package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"dashboard/pb"
)

type server struct {
	accountClient      pb.AccountDatabaseServiceClient
	orchestratorClient pb.OrchestratorServiceClient
	emailClient        pb.EmailServiceClient
	db                 *sql.DB
	staticDir          string
	mailboxRegisterMu  sync.Mutex
	mailboxRegistering bool
}

type jobRow struct {
	JobID        string    `json:"job_id"`
	AccountID    string    `json:"account_id"`
	Action       string    `json:"action"`
	Status       string    `json:"status"`
	Recoverable  bool      `json:"recoverable"`
	Retryable    bool      `json:"retryable"`
	LastStep     string    `json:"last_step"`
	ErrorMessage string    `json:"error_message"`
	ResultJSON   string    `json:"result_json"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
	Steps        []stepRow `json:"steps,omitempty"`
}

type stepRow struct {
	JobID        string    `json:"job_id,omitempty"`
	StepName     string    `json:"step_name"`
	Status       string    `json:"status"`
	Recoverable  bool      `json:"recoverable"`
	Retryable    bool      `json:"retryable"`
	ErrorMessage string    `json:"error_message"`
	ResultJSON   string    `json:"result_json"`
	StartedAt    int64     `json:"started_at"`
	CompletedAt  int64     `json:"completed_at"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type createAccountRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type upsertMailboxRequest struct {
	MailboxID    string `json:"mailbox_id"`
	Email        string `json:"email"`
	Password     string `json:"password"`
	RefreshToken string `json:"refresh_token"`
	AccessToken  string `json:"access_token"`
	Status       string `json:"status"`
	LastError    string `json:"last_error"`
}

type mailboxOAuthRequest struct {
	EmailAddress string `json:"email_address"`
	OnlyMissing  bool   `json:"only_missing"`
	Limit        int32  `json:"limit"`
}

type submitJobOTPRequest struct {
	OTP string `json:"otp"`
}

type updateAccountRequest struct {
	SessionToken string `json:"session_token"`
	AccessToken  string `json:"access_token"`
}

func main() {
	ctx := context.Background()

	accountConn, err := grpc.NewClient(envDefault("ACCOUNT_DB_ADDR", "account-db:50051"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("connect account-db: %v", err)
	}
	defer accountConn.Close()

	orchestratorConn, err := grpc.NewClient(envDefault("ORCHESTRATOR_ADDR", "orchestrator:50051"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("connect orchestrator: %v", err)
	}
	defer orchestratorConn.Close()

	emailConn, err := grpc.NewClient(envDefault("EMAIL_ADDR", "outlook-imap-service:50051"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("connect email service: %v", err)
	}
	defer emailConn.Close()

	pg, err := sql.Open("pgx", envDefault("ORCHESTRATOR_PG_DSN", envDefault("PG_DSN", "")))
	if err != nil {
		log.Fatalf("open postgres: %v", err)
	}
	if err := pg.PingContext(ctx); err != nil {
		log.Fatalf("ping postgres: %v", err)
	}
	defer pg.Close()

	s := &server{
		accountClient:      pb.NewAccountDatabaseServiceClient(accountConn),
		orchestratorClient: pb.NewOrchestratorServiceClient(orchestratorConn),
		emailClient:        pb.NewEmailServiceClient(emailConn),
		db:                 pg,
		staticDir:          envDefault("STATIC_DIR", "web/dist"),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/api/accounts", s.handleAccounts)
	mux.HandleFunc("/api/accounts/", s.handleAccount)
	mux.HandleFunc("/api/mailboxes/register", s.handleMailboxRegister)
	mux.HandleFunc("/api/mailboxes/oauth", s.handleMailboxOAuth)
	mux.HandleFunc("/api/mailboxes", s.handleMailboxes)
	mux.HandleFunc("/api/jobs", s.handleJobs)
	mux.HandleFunc("/api/jobs/", s.handleJob)
	mux.HandleFunc("/api/workflows/register", s.handleRegister)
	mux.HandleFunc("/api/workflows/activate", s.handleActivate)
	mux.HandleFunc("/api/workflows/login", s.handleLogin)
	mux.HandleFunc("/api/workflows/probe-plus-trial", s.handleProbePlusTrial)
	mux.HandleFunc("/api/workflows/register-and-activate", s.handleRegisterAndActivate)
	mux.HandleFunc("/", s.handleStatic)

	addr := envDefault("LISTEN_ADDR", ":8080")
	log.Printf("dashboard listening on %s", addr)
	if err := http.ListenAndServe(addr, withCORS(mux)); err != nil {
		log.Fatal(err)
	}
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) handleAccounts(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		limit := int32(queryInt(r, "limit", 100))
		resp, err := s.accountClient.ListAccounts(r.Context(), &pb.ListAccountsRequest{
			Status: r.URL.Query().Get("status"),
			Limit:  limit,
		})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		accounts := resp.GetAccounts()
		if accounts == nil {
			accounts = []*pb.Account{}
		}
		writeJSON(w, http.StatusOK, accounts)
	case http.MethodPost:
		var req createAccountRequest
		if err := readJSON(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		accountID := randomID()
		email := strings.TrimSpace(req.Email)
		if email == "" {
			emailResp, err := s.emailClient.GetEmail(r.Context(), &pb.GetEmailRequest{})
			if err != nil {
				writeError(w, http.StatusBadGateway, err)
				return
			}
			email = emailResp.GetEmailAddress()
		}
		resp, err := s.accountClient.CreateAccount(r.Context(), &pb.CreateAccountRequest{Account: &pb.Account{
			AccountId: accountID,
			Email:     email,
			Password:  req.Password,
		}})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusCreated, resp.GetAccount())
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (s *server) handleMailboxes(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		limit := int32(queryInt(r, "limit", 100))
		resp, err := s.emailClient.ListMailboxes(r.Context(), &pb.ListEmailMailboxesRequest{
			Status: r.URL.Query().Get("status"),
			Limit:  limit,
		})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		mailboxes := resp.GetMailboxes()
		if mailboxes == nil {
			mailboxes = []*pb.EmailMailbox{}
		}
		writeJSON(w, http.StatusOK, mailboxes)
	case http.MethodPost:
		var req upsertMailboxRequest
		if err := readJSON(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		resp, err := s.emailClient.UpsertMailbox(r.Context(), &pb.UpsertEmailMailboxRequest{Mailbox: &pb.EmailMailbox{
			EmailAddress: req.Email,
			Password:     req.Password,
			RefreshToken: req.RefreshToken,
			AccessToken:  req.AccessToken,
			Status:       req.Status,
			LastError:    req.LastError,
			IsPrimary:    true,
			PrimaryEmail: req.Email,
		}})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusCreated, resp.GetMailbox())
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (s *server) handleMailboxRegister(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	s.mailboxRegisterMu.Lock()
	if s.mailboxRegistering {
		s.mailboxRegisterMu.Unlock()
		writeError(w, http.StatusConflict, errors.New("mailbox registration is already running"))
		return
	}
	s.mailboxRegistering = true
	s.mailboxRegisterMu.Unlock()

	go func() {
		defer func() {
			s.mailboxRegisterMu.Lock()
			s.mailboxRegistering = false
			s.mailboxRegisterMu.Unlock()
		}()
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(envInt("MAILBOX_REGISTER_TIMEOUT_SECONDS", 1800))*time.Second)
		defer cancel()
		resp, err := s.orchestratorClient.RegisterMailbox(ctx, &pb.RegisterMailboxRequest{})
		if err != nil {
			log.Printf("mailbox registration workflow failed to start: %v", err)
			return
		}
		if resp.GetErrorMessage() != "" {
			log.Printf("mailbox registration workflow job=%s failed: %s", resp.GetJobId(), resp.GetErrorMessage())
			return
		}
		log.Printf("mailbox registration workflow job=%s completed success=%v exit_code=%d", resp.GetJobId(), resp.GetSuccess(), resp.GetExitCode())
	}()

	writeJSON(w, http.StatusAccepted, map[string]any{
		"started": true,
		"backend": "outlook-register-service",
	})
}

func (s *server) handleMailboxOAuth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	var req mailboxOAuthRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if req.Limit <= 0 {
		req.Limit = 100
	}
	if strings.TrimSpace(req.EmailAddress) == "" {
		req.OnlyMissing = true
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	resp, err := s.orchestratorClient.RunMailboxOAuth(ctx, &pb.StartMailboxOAuthRequest{
		EmailAddress: strings.TrimSpace(req.EmailAddress),
		OnlyMissing:  req.OnlyMissing,
		Limit:        req.Limit,
	})
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	statusCode := http.StatusAccepted
	if !resp.GetStarted() || resp.GetErrorMessage() != "" {
		statusCode = http.StatusBadGateway
	}
	writeJSON(w, statusCode, map[string]any{
		"started":       resp.GetStarted(),
		"job_id":        resp.GetJobId(),
		"error_message": resp.GetErrorMessage(),
		"backend":       "outlook-register-service",
	})
}

func (s *server) handleAccount(w http.ResponseWriter, r *http.Request) {
	accountPath := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/accounts/"), "/")
	parts := strings.Split(accountPath, "/")
	accountID := parts[0]
	if accountID == "" {
		writeError(w, http.StatusBadRequest, errors.New("account_id is required"))
		return
	}
	if len(parts) > 1 {
		if len(parts) == 2 && parts[1] == "access-token" {
			s.handleAccountAccessToken(w, r, accountID)
			return
		}
		writeError(w, http.StatusNotFound, errors.New("account endpoint not found"))
		return
	}

	switch r.Method {
	case http.MethodGet:
		resp, err := s.accountClient.GetAccount(r.Context(), &pb.GetAccountRequest{AccountId: accountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp.GetAccount())
	case http.MethodPatch, http.MethodPut:
		var req updateAccountRequest
		if err := readJSON(r, &req); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		sessionToken := strings.TrimSpace(req.SessionToken)
		accessToken := strings.TrimSpace(req.AccessToken)
		if sessionToken == "" && accessToken == "" {
			writeError(w, http.StatusBadRequest, errors.New("session_token or access_token is required"))
			return
		}
		resp, err := s.accountClient.UpdateAccount(r.Context(), &pb.UpdateAccountRequest{Account: &pb.Account{
			AccountId:    accountID,
			SessionToken: sessionToken,
			AccessToken:  accessToken,
		}})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp.GetAccount())
	case http.MethodDelete:
		resp, err := s.accountClient.DeleteAccount(r.Context(), &pb.DeleteAccountRequest{AccountId: accountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func (s *server) handleAccountAccessToken(w http.ResponseWriter, r *http.Request, accountID string) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	accountResp, err := s.accountClient.GetAccount(ctx, &pb.GetAccountRequest{AccountId: accountID})
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	account := accountResp.GetAccount()
	if account == nil {
		writeError(w, http.StatusNotFound, errors.New("account not found"))
		return
	}
	sessionToken := strings.TrimSpace(account.GetSessionToken())
	if sessionToken == "" {
		writeError(w, http.StatusBadRequest, errors.New("session_token is required"))
		return
	}

	accessToken, err := fetchChatGPTAccessToken(ctx, sessionToken)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	updated, err := s.accountClient.UpdateAccount(ctx, &pb.UpdateAccountRequest{Account: &pb.Account{
		AccountId:   accountID,
		AccessToken: accessToken,
	}})
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, updated.GetAccount())
}

func fetchChatGPTAccessToken(ctx context.Context, sessionToken string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://chatgpt.com/api/auth/session", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Referer", "https://chatgpt.com/")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
	req.Header.Set("Cookie", "__Secure-next-auth.session-token="+strings.TrimSpace(sessionToken))

	resp, err := (&http.Client{Timeout: 25 * time.Second}).Do(req)
	if err != nil {
		return "", fmt.Errorf("fetch auth session: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("auth session returned status %d", resp.StatusCode)
	}

	var payload struct {
		AccessToken string `json:"accessToken"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", fmt.Errorf("decode auth session: %w", err)
	}
	accessToken := strings.TrimSpace(payload.AccessToken)
	if accessToken == "" {
		return "", errors.New("auth session did not return access token")
	}
	return accessToken, nil
}

func (s *server) handleJobs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	jobs, err := s.listJobs(r.Context(), r)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, jobs)
}

func (s *server) handleJob(w http.ResponseWriter, r *http.Request) {
	rest := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/jobs/"), "/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || strings.TrimSpace(parts[0]) == "" {
		writeError(w, http.StatusBadRequest, errors.New("job_id is required"))
		return
	}
	jobID := strings.TrimSpace(parts[0])

	if len(parts) > 1 {
		switch parts[1] {
		case "retry":
			if r.Method != http.MethodPost {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			s.retryJob(w, r, jobID)
			return
		case "otp":
			if r.Method != http.MethodPost {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			s.submitJobOTP(w, r, jobID)
			return
		default:
			writeError(w, http.StatusNotFound, fmt.Errorf("unsupported job action: %s", parts[1]))
			return
		}
	}

	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	job, err := s.getJob(r.Context(), jobID)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, job)
}

func (s *server) submitJobOTP(w http.ResponseWriter, r *http.Request, jobID string) {
	var req submitJobOTPRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}

	resp, err := s.orchestratorClient.SubmitRegistrationOtp(r.Context(), &pb.SubmitRegistrationOtpRequest{
		JobId: jobID,
		Otp:   req.OTP,
	})
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if resp.GetErrorMessage() != "" {
		writeError(w, http.StatusBadRequest, errors.New(resp.GetErrorMessage()))
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) retryJob(w http.ResponseWriter, r *http.Request, jobID string) {
	job, err := s.getJob(r.Context(), jobID)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	if !job.Retryable || !strings.HasPrefix(job.Status, "FAILED") {
		writeError(w, http.StatusConflict, errors.New("only retryable failed jobs can be retried"))
		return
	}
	if strings.TrimSpace(job.AccountID) == "" {
		writeError(w, http.StatusBadRequest, errors.New("job account_id is empty"))
		return
	}

	switch job.Action {
	case "REGISTER":
		resp, err := s.orchestratorClient.RegisterAccount(r.Context(), &pb.RegisterAccountRequest{AccountId: job.AccountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp)
	case "ACTIVATE":
		resp, err := s.orchestratorClient.ActivateAccount(r.Context(), &pb.ActivateAccountRequest{AccountId: job.AccountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp)
	case "PROBE_PLUS_TRIAL":
		resp, err := s.orchestratorClient.ProbePlusTrial(r.Context(), &pb.ProbePlusTrialRequest{AccountId: job.AccountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp)
	case "REGISTER_AND_ACTIVATE":
		resp, err := s.orchestratorClient.RegisterAndActivateAccount(r.Context(), &pb.RegisterAndActivateAccountRequest{AccountId: job.AccountID})
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, resp)
	default:
		writeError(w, http.StatusBadRequest, fmt.Errorf("unsupported job action: %s", job.Action))
	}
}

func (s *server) handleRegister(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req pb.RegisterAccountRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	resp, err := s.orchestratorClient.RegisterAccount(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) handleActivate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req pb.ActivateAccountRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	resp, err := s.orchestratorClient.ActivateAccount(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req pb.LoginAccountRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	resp, err := s.orchestratorClient.LoginAccount(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	statusCode := http.StatusAccepted
	if !resp.GetStarted() || resp.GetErrorMessage() != "" {
		statusCode = http.StatusBadGateway
	}
	writeJSON(w, statusCode, resp)
}

func (s *server) handleProbePlusTrial(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req pb.ProbePlusTrialRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	resp, err := s.orchestratorClient.ProbePlusTrial(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	statusCode := http.StatusAccepted
	if !resp.GetStarted() || resp.GetErrorMessage() != "" {
		statusCode = http.StatusBadGateway
	}
	writeJSON(w, statusCode, resp)
}

func (s *server) handleRegisterAndActivate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var req pb.RegisterAndActivateAccountRequest
	if err := readJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	resp, err := s.orchestratorClient.RegisterAndActivateAccount(r.Context(), &req)
	if err != nil {
		writeError(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) listJobs(ctx context.Context, r *http.Request) ([]jobRow, error) {
	limit := queryInt(r, "limit", 100)
	if limit <= 0 || limit > 500 {
		limit = 100
	}

	query := `SELECT id, account_id, action, status, recoverable, retryable, last_step, error_message, result_json, to_timestamp(created_at), to_timestamp(updated_at) FROM jobs WHERE 1=1`
	args := []any{}
	if value := strings.TrimSpace(r.URL.Query().Get("status")); value != "" {
		args = append(args, value)
		query += fmt.Sprintf(" AND status = $%d", len(args))
	}
	if value := strings.TrimSpace(r.URL.Query().Get("action")); value != "" {
		args = append(args, value)
		query += fmt.Sprintf(" AND action = $%d", len(args))
	}
	if value := strings.TrimSpace(r.URL.Query().Get("account_id")); value != "" {
		args = append(args, value)
		query += fmt.Sprintf(" AND account_id = $%d", len(args))
	}
	args = append(args, limit)
	query += fmt.Sprintf(" ORDER BY updated_at DESC LIMIT $%d", len(args))

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	jobs := []jobRow{}
	for rows.Next() {
		var job jobRow
		if err := rows.Scan(&job.JobID, &job.AccountID, &job.Action, &job.Status, &job.Recoverable, &job.Retryable, &job.LastStep, &job.ErrorMessage, &job.ResultJSON, &job.CreatedAt, &job.UpdatedAt); err != nil {
			return nil, err
		}
		jobs = append(jobs, job)
	}
	return jobs, rows.Err()
}

func (s *server) getJob(ctx context.Context, jobID string) (*jobRow, error) {
	row := s.db.QueryRowContext(ctx, `SELECT id, account_id, action, status, recoverable, retryable, last_step, error_message, result_json, to_timestamp(created_at), to_timestamp(updated_at) FROM jobs WHERE id = $1`, jobID)
	var job jobRow
	if err := row.Scan(&job.JobID, &job.AccountID, &job.Action, &job.Status, &job.Recoverable, &job.Retryable, &job.LastStep, &job.ErrorMessage, &job.ResultJSON, &job.CreatedAt, &job.UpdatedAt); err != nil {
		return nil, err
	}

	rows, err := s.db.QueryContext(ctx, `SELECT job_id, step_name, status, recoverable, retryable, error_message, result_json, started_at, completed_at, to_timestamp(created_at), to_timestamp(updated_at) FROM job_steps WHERE job_id = $1 ORDER BY started_at ASC, step_name ASC`, jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	for rows.Next() {
		var step stepRow
		if err := rows.Scan(&step.JobID, &step.StepName, &step.Status, &step.Recoverable, &step.Retryable, &step.ErrorMessage, &step.ResultJSON, &step.StartedAt, &step.CompletedAt, &step.CreatedAt, &step.UpdatedAt); err != nil {
			return nil, err
		}
		job.Steps = append(job.Steps, step)
	}
	return &job, rows.Err()
}

func (s *server) handleStatic(w http.ResponseWriter, r *http.Request) {
	path := filepath.Join(s.staticDir, filepath.Clean(r.URL.Path))
	if info, err := os.Stat(path); err == nil && !info.IsDir() {
		http.ServeFile(w, r, path)
		return
	}
	http.ServeFile(w, r, filepath.Join(s.staticDir, "index.html"))
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		w.Header().Set("Access-Control-Allow-Methods", "GET,POST,PATCH,PUT,DELETE,OPTIONS")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func readJSON(r *http.Request, dst any) error {
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(dst)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func queryInt(r *http.Request, key string, fallback int) int {
	value := strings.TrimSpace(r.URL.Query().Get(key))
	if value == "" {
		return fallback
	}
	n, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return n
}

func envDefault(key string, fallback string) string {
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
	if err != nil {
		return fallback
	}
	return n
}

func tailString(value string, limit int) string {
	if limit <= 0 || len(value) <= limit {
		return value
	}
	return value[len(value)-limit:]
}

func randomID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}
