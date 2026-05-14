package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	temporalclient "go.temporal.io/sdk/client"
	"go.temporal.io/sdk/testsuite"
	temporalworker "go.temporal.io/sdk/worker"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"orchestrator/db"
	"orchestrator/pb"
)

const (
	actionRegister            = "REGISTER"
	actionActivate            = "ACTIVATE"
	actionAutopay             = "AUTOPAY"
	actionGoPayCycle          = "GOPAY_CYCLE"
	actionProbeAccount        = "PROBE_ACCOUNT"
	actionLoginSession        = "LOGIN_SESSION"
	actionRegisterAndActivate = "REGISTER_AND_ACTIVATE"
	actionRegisterMailbox     = "REGISTER_MAILBOX"
	actionMailboxOAuth        = "MAILBOX_OAUTH"

	statusCreated           = "CREATED"
	statusRunning           = "RUNNING"
	statusSucceeded         = "SUCCEEDED"
	statusFailedRecoverable = "FAILED_RECOVERABLE"
	statusFailedRetryable   = "FAILED_RETRYABLE"
	statusFailedFinal       = "FAILED_FINAL"

	accountStatusRegistered        = "REGISTERED"
	accountStatusActivated         = "ACTIVATED"
	accountStatusUserAlreadyExists = "USER_ALREADY_EXISTS"

	emailStatusAvailable         = "AVAILABLE"
	emailStatusRegistered        = "REGISTERED"
	emailStatusOAuthPending      = "OAUTH_PENDING"
	emailStatusUserAlreadyExists = "USER_ALREADY_EXISTS"
	emailStatusRegistrationFail  = "REGISTRATION_FAILED"
	emailStatusAuthFailed        = "AUTH_FAILED"
	emailStatusNeedsManualVerify = "NEEDS_MANUAL_VERIFICATION"

	emailAuthStatusAuthorized        = "AUTHORIZED"
	emailAuthStatusOAuthPending      = "OAUTH_PENDING"
	emailAuthStatusAuthFailed        = "AUTH_FAILED"
	emailAuthStatusNeedsManualVerify = "NEEDS_MANUAL_VERIFICATION"

	stepRegisterAccount       = "register_account"
	stepEnsureLogon           = "ensure_logon"
	stepGoPayCycleLogin       = "gopay_cycle_login"
	stepGoPayCycleChangePhone = "gopay_cycle_change_phone"
	stepGoPayCycleDeactivate  = "gopay_cycle_deactivate"
	stepGoPayCycleSignup      = "gopay_cycle_signup"
	stepGoPayCycleCreatePin   = "gopay_cycle_create_pin"
	stepGoPayPayment          = "gopay_payment"
	stepProbePlusTrial        = "probe_plus_trial"
	stepProbeTier             = "probe_tier"
	stepLoginSession          = "login_session"
	stepRegisterMailbox       = "register_mailbox"
	stepMailboxOAuth          = "mailbox_oauth"

	registrationOTPParam            = "registration_otp"
	registrationOTPSubmittedAtParam = "registration_otp_submitted_at_unix"
	paymentOTPParam                 = "payment_otp"
	paymentOTPSubmittedAtParam      = "payment_otp_submitted_at_unix"
	paymentManualConfirmParam       = "payment_manual_confirmed"
	paymentManualConfirmAtParam     = "payment_manual_confirmed_at_unix"
	goPayCycleStateKey              = "default"
)

type orchestratorServer struct {
	pb.UnimplementedOrchestratorServiceServer
	db                                *gorm.DB
	accountClient                     pb.AccountDatabaseServiceClient
	browserClient                     pb.BrowserRegistrationClient
	paymentClient                     pb.PaymentServiceClient
	cycleClient                       pb.GopayCycleServiceClient
	smsClient                         pb.SMSServiceClient
	emailClient                       pb.EmailServiceClient
	mailboxRegisterClient             pb.MailboxRegistrationServiceClient
	otpAddr                           string
	otpTimeout                        int32
	regOTPTimeout                     int32
	changePhoneMaxFailures            int
	changePhoneDisabled               bool
	changePhoneOTPWaitSeconds         int32
	changePhoneOTPRetryAttempts       int
	changePhoneGetNumberRetryDelay    time.Duration
	changePhoneSMSCancelTimeout       time.Duration
	changePhoneSMSCancelRetryInterval time.Duration
	temporal                          temporalclient.Client
	taskQueue                         string
}

type registrationOTPResult struct {
	Code   string
	Source string
}

type paymentOTPResult struct {
	Code   string
	Source string
}

func (s *orchestratorServer) createAccount(ctx context.Context, account *pb.Account) (*pb.Account, error) {
	resp, err := s.accountClient.CreateAccount(ctx, &pb.CreateAccountRequest{Account: account})
	if err != nil {
		return nil, fmt.Errorf("create account: %w", err)
	}
	if resp.GetAccount() == nil || resp.GetAccount().GetAccountId() == "" {
		return nil, fmt.Errorf("account-db returned empty account")
	}
	return resp.GetAccount(), nil
}

func (s *orchestratorServer) getAccount(ctx context.Context, accountID string) (*pb.Account, error) {
	resp, err := s.accountClient.GetAccount(ctx, &pb.GetAccountRequest{AccountId: accountID})
	if err != nil {
		return nil, err
	}
	if resp.GetAccount() == nil {
		return nil, fmt.Errorf("account not found: %s", accountID)
	}
	return resp.GetAccount(), nil
}

func (s *orchestratorServer) updateAccount(ctx context.Context, account *pb.Account) error {
	_, err := s.accountClient.UpdateAccount(ctx, &pb.UpdateAccountRequest{Account: account})
	return err
}

func (s *orchestratorServer) createJob(ctx context.Context, accountID, action string, params map[string]string) (*db.Job, error) {
	job := &db.Job{
		ID:        uuid.NewString(),
		AccountID: accountID,
		Action:    action,
		Status:    statusCreated,
	}

	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(job).Error; err != nil {
			return err
		}
		return upsertJobParams(ctx, tx, job.ID, params)
	})
	if err != nil {
		return nil, err
	}
	return job, nil
}

func upsertJobParams(ctx context.Context, tx *gorm.DB, jobID string, params map[string]string) error {
	if len(params) == 0 {
		return nil
	}

	rows := make([]db.JobParam, 0, len(params))
	for key, value := range params {
		key = strings.TrimSpace(key)
		if key == "" || value == "" {
			continue
		}
		rows = append(rows, db.JobParam{JobID: jobID, Key: key, Value: value})
	}
	if len(rows) == 0 {
		return nil
	}

	return tx.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "job_id"}, {Name: "key"}},
		DoUpdates: clause.AssignmentColumns([]string{"value", "updated_at"}),
	}).Create(&rows).Error
}

func (s *orchestratorServer) setJobParams(ctx context.Context, jobID string, params map[string]string) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		return upsertJobParams(ctx, tx, jobID, params)
	})
}

func (s *orchestratorServer) getJobParam(ctx context.Context, jobID, key string) (string, bool, error) {
	var param db.JobParam
	result := s.db.WithContext(ctx).
		Where("job_id = ? AND key = ?", jobID, key).
		Limit(1).
		Find(&param)
	if result.Error != nil {
		return "", false, result.Error
	}
	if result.RowsAffected == 0 {
		return "", false, nil
	}
	return param.Value, true, nil
}

func (s *orchestratorServer) deleteJobParam(ctx context.Context, jobID, key string) error {
	return s.db.WithContext(ctx).Delete(&db.JobParam{}, "job_id = ? AND key = ?", jobID, key).Error
}

func (s *orchestratorServer) updateJob(ctx context.Context, jobID, statusValue, errorMessage string, result any) {
	updates := map[string]any{
		"status":        statusValue,
		"recoverable":   statusValue == statusFailedRecoverable,
		"retryable":     statusValue == statusFailedRetryable,
		"error_message": errorMessage,
	}
	if result != nil {
		if b, err := json.Marshal(result); err == nil {
			updates["result_json"] = string(b)
		}
	}
	if err := s.db.WithContext(ctx).Model(&db.Job{}).Where("id = ?", jobID).Updates(updates).Error; err != nil {
		log.Printf("[orchestrator] update job failed job=%s: %v", jobID, err)
	}
}

func (s *orchestratorServer) getJob(ctx context.Context, jobID string) (*db.Job, error) {
	var job db.Job
	if err := s.db.WithContext(ctx).First(&job, "id = ?", jobID).Error; err != nil {
		return nil, err
	}
	return &job, nil
}

func (s *orchestratorServer) runAtomicStep(ctx context.Context, jobID, stepName string, recoverable bool, retryable bool, fn func() (any, error)) (any, error) {
	startedAt := time.Now().Unix()
	start := db.JobStep{
		JobID:       jobID,
		StepName:    stepName,
		Status:      statusRunning,
		Recoverable: recoverable,
		Retryable:   retryable,
		StartedAt:   startedAt,
	}

	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Clauses(clause.OnConflict{
			Columns: []clause.Column{{Name: "job_id"}, {Name: "step_name"}},
			DoUpdates: clause.Assignments(map[string]any{
				"status":        statusRunning,
				"recoverable":   recoverable,
				"retryable":     retryable,
				"error_message": "",
				"result_json":   "",
				"started_at":    startedAt,
				"completed_at":  int64(0),
			}),
		}).Create(&start).Error; err != nil {
			return err
		}
		return tx.Model(&db.Job{}).Where("id = ?", jobID).Updates(map[string]any{
			"status":        statusRunning,
			"recoverable":   false,
			"retryable":     false,
			"last_step":     stepName,
			"error_message": "",
		}).Error
	})
	if err != nil {
		return nil, err
	}

	result, stepErr := fn()
	completedAt := time.Now().Unix()
	resultJSON := marshalStepResult(jobID, stepName, result)
	statusValue := statusSucceeded
	errorMessage := ""
	if stepErr != nil {
		statusValue = failedStatus(recoverable, retryable)
		errorMessage = stepErr.Error()
	}

	updates := map[string]any{
		"status":        statusValue,
		"recoverable":   recoverable,
		"retryable":     retryable,
		"error_message": errorMessage,
		"result_json":   resultJSON,
		"completed_at":  completedAt,
	}
	if err := s.db.WithContext(ctx).Model(&db.JobStep{}).
		Where("job_id = ? AND step_name = ?", jobID, stepName).
		Updates(updates).Error; err != nil {
		log.Printf("[orchestrator] update step failed job=%s step=%s: %v", jobID, stepName, err)
	}

	if stepErr != nil {
		if err := s.db.WithContext(ctx).Model(&db.Job{}).Where("id = ?", jobID).Updates(map[string]any{
			"status":        statusValue,
			"recoverable":   recoverable,
			"retryable":     retryable,
			"last_step":     stepName,
			"error_message": errorMessage,
		}).Error; err != nil {
			log.Printf("[orchestrator] update failed job failed job=%s step=%s: %v", jobID, stepName, err)
		}
		return result, stepErr
	}

	return result, nil
}

func (s *orchestratorServer) updateRunningStepData(ctx context.Context, jobID, stepName string, result any) {
	resultJSON := marshalStepResult(jobID, stepName, result)
	if resultJSON == "" {
		return
	}
	if err := s.db.WithContext(ctx).Model(&db.JobStep{}).
		Where("job_id = ? AND step_name = ? AND status = ?", jobID, stepName, statusRunning).
		Update("result_json", resultJSON).Error; err != nil {
		log.Printf("[orchestrator] update running step data failed job=%s step=%s: %v", jobID, stepName, err)
	}
}

func failedStatus(recoverable bool, retryable bool) string {
	if recoverable {
		return statusFailedRecoverable
	}
	if retryable {
		return statusFailedRetryable
	}
	return statusFailedFinal
}

func marshalStepResult(jobID, stepName string, result any) string {
	if result == nil {
		return ""
	}
	b, err := json.Marshal(result)
	if err != nil {
		log.Printf("[orchestrator] marshal step result failed job=%s step=%s: %v", jobID, stepName, err)
		return ""
	}
	return string(b)
}

func (s *orchestratorServer) register(ctx context.Context, jobID string, account *pb.Account) (result *pb.RegisterResponse, data map[string]any, err error) {
	data = map[string]any{
		"account_id": account.GetAccountId(),
		"email":      account.GetEmail(),
	}

	startResp, err := s.browserClient.StartRegister(ctx, &pb.RegisterRequest{
		JobId:         jobID,
		AssignedEmail: account.GetEmail(),
		Password:      account.GetPassword(),
		FirstName:     account.GetFirstName(),
		LastName:      account.GetLastName(),
		Birthday:      account.GetDob(),
	})
	data["browser_start"] = browserStartData(startResp)
	if err != nil {
		return nil, data, err
	}
	if startResp == nil {
		return nil, data, fmt.Errorf("browser start returned empty response")
	}
	if !startResp.GetSuccess() {
		return nil, data, fmt.Errorf("browser start failed: %s", startResp.GetErrorMessage())
	}

	flowID := startResp.GetFlowId()
	completed := false
	defer func() {
		if flowID != "" && !completed {
			cancelCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			cancelResp, cancelErr := s.browserClient.CancelRegister(cancelCtx, &pb.CancelRegisterRequest{FlowId: flowID})
			data["cleanup"] = cleanupDataFromBrowser(cancelResp, cancelErr)
		}
	}()

	if !startResp.GetOtpRequired() {
		result = startResp.GetResult()
		data["browser_complete"] = registerResultData(result)
		if result == nil {
			return nil, data, fmt.Errorf("browser completed without result")
		}
		if !result.GetSuccess() {
			return nil, data, fmt.Errorf("browser failed: %s", result.GetErrorMessage())
		}
		completed = true
		return result, data, nil
	}

	otpIssuedAfterUnix := startResp.GetOtpIssuedAfterUnix()
	otpTimeout := s.registrationOtpTimeout()
	otp, err := s.waitForRegistrationOtp(ctx, jobID, account.GetEmail(), otpTimeout, otpIssuedAfterUnix)
	data["registration_otp"] = map[string]any{
		"email":              account.GetEmail(),
		"timeout_seconds":    otpTimeout,
		"issued_after_unix":  otpIssuedAfterUnix,
		"found":              err == nil,
		"source":             otp.Source,
		"manual_allowed":     true,
		"otp_value_recorded": false,
	}
	if err != nil {
		return nil, data, err
	}

	result, err = s.browserClient.CompleteRegister(ctx, &pb.CompleteRegisterRequest{FlowId: flowID, Otp: otp.Code})
	data["browser_complete"] = registerResultData(result)
	if err != nil {
		return nil, data, err
	}
	if result == nil {
		return nil, data, fmt.Errorf("browser complete returned empty response")
	}
	if !result.GetSuccess() {
		return nil, data, fmt.Errorf("browser complete failed: %s", result.GetErrorMessage())
	}
	completed = true
	return result, data, nil
}

func (s *orchestratorServer) loginSession(ctx context.Context, jobID string, account *pb.Account) (result *pb.RegisterResponse, data map[string]any, err error) {
	data = map[string]any{
		"account_id": account.GetAccountId(),
		"email":      account.GetEmail(),
	}

	startResp, err := s.browserClient.StartLogin(ctx, &pb.RegisterRequest{
		JobId:         jobID,
		AssignedEmail: account.GetEmail(),
		Password:      account.GetPassword(),
	})
	data["browser_start"] = browserStartData(startResp)
	if err != nil {
		return nil, data, err
	}
	if startResp == nil {
		return nil, data, fmt.Errorf("browser login start returned empty response")
	}
	if !startResp.GetSuccess() {
		return nil, data, fmt.Errorf("browser login start failed: %s", startResp.GetErrorMessage())
	}

	flowID := startResp.GetFlowId()
	completed := false
	defer func() {
		if flowID != "" && !completed {
			cancelCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			cancelResp, cancelErr := s.browserClient.CancelLogin(cancelCtx, &pb.CancelRegisterRequest{FlowId: flowID})
			data["cleanup"] = cleanupDataFromBrowser(cancelResp, cancelErr)
		}
	}()

	if !startResp.GetOtpRequired() {
		result = startResp.GetResult()
		data["browser_complete"] = registerResultData(result)
		if result == nil {
			return nil, data, fmt.Errorf("browser login completed without result")
		}
		if !result.GetSuccess() {
			return nil, data, fmt.Errorf("browser login failed: %s", result.GetErrorMessage())
		}
		completed = true
		return result, data, nil
	}

	otpIssuedAfterUnix := startResp.GetOtpIssuedAfterUnix()
	otpTimeout := s.registrationOtpTimeout()
	otp, err := s.waitForRegistrationOtp(ctx, jobID, account.GetEmail(), otpTimeout, otpIssuedAfterUnix)
	data["login_otp"] = map[string]any{
		"email":              account.GetEmail(),
		"timeout_seconds":    otpTimeout,
		"issued_after_unix":  otpIssuedAfterUnix,
		"found":              err == nil,
		"source":             otp.Source,
		"manual_allowed":     true,
		"otp_value_recorded": false,
	}
	if err != nil {
		return nil, data, err
	}

	result, err = s.browserClient.CompleteLogin(ctx, &pb.CompleteRegisterRequest{FlowId: flowID, Otp: otp.Code})
	data["browser_complete"] = registerResultData(result)
	if err != nil {
		return nil, data, err
	}
	if result == nil {
		return nil, data, fmt.Errorf("browser login complete returned empty response")
	}
	if !result.GetSuccess() {
		return nil, data, fmt.Errorf("browser login complete failed: %s", result.GetErrorMessage())
	}
	completed = true
	return result, data, nil
}

func (s *orchestratorServer) waitForRegistrationOtp(ctx context.Context, jobID, email string, timeoutSeconds int32, issuedAfterUnix int64) (registrationOTPResult, error) {
	if timeoutSeconds <= 0 {
		timeoutSeconds = 120
	}
	defer func() {
		_ = s.deleteJobParam(context.Background(), jobID, registrationOTPParam)
		_ = s.deleteJobParam(context.Background(), jobID, registrationOTPSubmittedAtParam)
	}()

	type emailOTPResult struct {
		code string
		err  error
	}

	emailCtx, cancelEmail := context.WithCancel(ctx)
	defer cancelEmail()

	emailCh := make(chan emailOTPResult, 1)
	go func() {
		reqCtx, cancel := context.WithTimeout(emailCtx, time.Duration(timeoutSeconds+5)*time.Second)
		defer cancel()
		resp, err := s.emailClient.WaitForEmail(reqCtx, &pb.WaitForEmailRequest{
			EmailAddress:    email,
			TimeoutSeconds:  timeoutSeconds,
			IssuedAfterUnix: issuedAfterUnix,
		})
		if err != nil {
			emailCh <- emailOTPResult{err: err}
			return
		}
		if resp == nil {
			emailCh <- emailOTPResult{err: fmt.Errorf("email service returned empty otp response")}
			return
		}
		if resp.GetFound() && strings.TrimSpace(resp.GetContentExtracted()) != "" {
			emailCh <- emailOTPResult{code: strings.TrimSpace(resp.GetContentExtracted())}
			return
		}
		emailCh <- emailOTPResult{err: fmt.Errorf("email otp not found")}
	}()

	deadline := time.NewTimer(time.Duration(timeoutSeconds) * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	var lastErr error
	for {
		select {
		case emailResult := <-emailCh:
			if strings.TrimSpace(emailResult.code) != "" {
				return registrationOTPResult{Code: strings.TrimSpace(emailResult.code), Source: "email"}, nil
			}
			if emailResult.err != nil {
				lastErr = emailResult.err
			}
			emailCh = nil
		case <-ticker.C:
			code, found, err := s.consumeManualRegistrationOtp(ctx, jobID, issuedAfterUnix)
			if err != nil {
				lastErr = err
				continue
			}
			if found {
				return registrationOTPResult{Code: code, Source: "manual"}, nil
			}
		case <-deadline.C:
			if lastErr != nil {
				return registrationOTPResult{}, fmt.Errorf("registration otp not received after %ds: %w", timeoutSeconds, lastErr)
			}
			return registrationOTPResult{}, fmt.Errorf("registration otp not received after %ds", timeoutSeconds)
		case <-ctx.Done():
			return registrationOTPResult{}, ctx.Err()
		}
	}
}

func (s *orchestratorServer) consumeManualRegistrationOtp(ctx context.Context, jobID string, issuedAfterUnix int64) (string, bool, error) {
	value, found, err := s.getJobParam(ctx, jobID, registrationOTPParam)
	if err != nil || !found {
		return "", false, err
	}
	if !manualOTPSubmittedAfter(ctx, s, jobID, registrationOTPParam, registrationOTPSubmittedAtParam, issuedAfterUnix) {
		return "", false, nil
	}
	code := normalizeOTP(value)
	if code == "" {
		return "", false, s.deleteJobParam(ctx, jobID, registrationOTPParam)
	}
	if err := s.deleteJobParam(ctx, jobID, registrationOTPParam); err != nil {
		return "", false, err
	}
	_ = s.deleteJobParam(ctx, jobID, registrationOTPSubmittedAtParam)
	return code, true, nil
}

func (s *orchestratorServer) pay(ctx context.Context, jobID string, account *pb.Account, sessionToken, accessToken string, useCycleToken bool, tokenization string) (result *pb.GoPayResponse, data map[string]any, err error) {
	if sessionToken == "" {
		sessionToken = account.GetSessionToken()
	}
	if accessToken == "" {
		accessToken = account.GetAccessToken()
	}
	tokenization = strings.TrimSpace(tokenization)

	data = map[string]any{
		"account_id":             account.GetAccountId(),
		"session_token_present":  sessionToken != "",
		"access_token_present":   accessToken != "",
		"used_cycle_token":       useCycleToken,
		"tokenization":           tokenization,
		"otp_value_recorded":     false,
		"payment_result_present": false,
	}
	if !useCycleToken && sessionToken == "" && accessToken == "" {
		return nil, data, fmt.Errorf("session_token or access_token is required")
	}
	if useCycleToken {
		data["cycle_balance_check"] = map[string]any{
			"required_before_payment": true,
		}
		if err := s.waitForCycleMinBalance(ctx); err != nil {
			data["cycle_balance_check"] = map[string]any{
				"required_before_payment": true,
				"ready":                   false,
				"error_message":           err.Error(),
			}
			return nil, data, err
		}
		data["cycle_balance_check"] = map[string]any{
			"required_before_payment": true,
			"ready":                   true,
		}
	}

	started, err := s.paymentClient.StartGoPay(ctx, &pb.StartGoPayRequest{
		SessionToken:  sessionToken,
		AccessToken:   accessToken,
		UseCycleToken: useCycleToken,
		Tokenization:  tokenization,
	})
	data["payment_start"] = paymentStartData(started)
	if err != nil {
		return nil, data, err
	}
	if started == nil {
		return nil, data, fmt.Errorf("payment start returned empty response")
	}
	if !started.GetSuccess() {
		return nil, data, fmt.Errorf("payment start failed: %s", started.GetErrorMessage())
	}

	flowID := started.GetFlowId()
	completed := false
	defer func() {
		if flowID != "" && !completed {
			cancelCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			cancelResp, cancelErr := s.paymentClient.CancelGoPay(cancelCtx, &pb.CancelGoPayRequest{FlowId: flowID})
			data["cleanup"] = cleanupDataFromPayment(cancelResp, cancelErr)
		}
	}()

	otp, err := s.waitForPaymentOtp(ctx, jobID, started.GetIssuedAfterUnix())
	data["payment_otp"] = map[string]any{
		"timeout_seconds":    s.paymentOtpTimeout(),
		"issued_after_unix":  started.GetIssuedAfterUnix(),
		"found":              err == nil,
		"source":             otp.Source,
		"manual_allowed":     true,
		"otp_value_recorded": false,
	}
	if err != nil {
		return nil, data, err
	}

	result, err = s.paymentClient.CompleteGoPay(ctx, &pb.CompleteGoPayRequest{FlowId: flowID, Otp: otp.Code})
	data["payment_complete"] = paymentResultData(result)
	data["payment_result_present"] = result != nil
	if err != nil {
		return nil, data, err
	}
	if result == nil {
		return nil, data, fmt.Errorf("payment complete returned empty response")
	}
	if !result.GetSuccess() {
		return nil, data, fmt.Errorf("payment complete failed: %s", result.GetErrorMessage())
	}
	if result.GetAwaitingManualConfirmation() {
		issuedAfterUnix := time.Now().Unix()
		data["manual_payment_confirmation"] = map[string]any{
			"required":          true,
			"issued_after_unix": issuedAfterUnix,
			"confirmed":         false,
		}
		s.updateRunningStepData(ctx, jobID, stepGoPayPayment, data)

		if err := s.waitForManualPaymentConfirmation(ctx, jobID, issuedAfterUnix); err != nil {
			data["manual_payment_confirmation"] = map[string]any{
				"required":          true,
				"issued_after_unix": issuedAfterUnix,
				"confirmed":         false,
				"error_message":     err.Error(),
			}
			return nil, data, err
		}
		data["manual_payment_confirmation"] = map[string]any{
			"required":          true,
			"issued_after_unix": issuedAfterUnix,
			"confirmed":         true,
		}

		result, err = s.paymentClient.ConfirmGoPayPayment(ctx, &pb.ConfirmGoPayPaymentRequest{FlowId: flowID})
		data["payment_confirm"] = paymentResultData(result)
		data["payment_result_present"] = result != nil
		if err != nil {
			return nil, data, err
		}
		if result == nil {
			return nil, data, fmt.Errorf("payment confirm returned empty response")
		}
		if !result.GetSuccess() {
			return nil, data, fmt.Errorf("payment confirm failed: %s", result.GetErrorMessage())
		}
	}
	completed = true

	return result, data, nil
}

func (s *orchestratorServer) waitForPaymentOtp(ctx context.Context, jobID string, issuedAfterUnix int64) (paymentOTPResult, error) {
	addr := s.otpAddr
	if addr == "" {
		addr = "whatsapp-otp-relay:50051"
	}
	timeoutSeconds := s.paymentOtpTimeout()
	defer func() {
		_ = s.deleteJobParam(context.Background(), jobID, paymentOTPParam)
		_ = s.deleteJobParam(context.Background(), jobID, paymentOTPSubmittedAtParam)
	}()

	type otpServiceResult struct {
		code string
		err  error
	}

	otpCtx, cancelOTP := context.WithCancel(ctx)
	defer cancelOTP()

	otpCh := make(chan otpServiceResult, 1)
	go func() {
		conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			otpCh <- otpServiceResult{err: err}
			return
		}
		defer conn.Close()

		reqCtx, cancel := context.WithTimeout(otpCtx, time.Duration(timeoutSeconds+10)*time.Second)
		defer cancel()

		resp, err := pb.NewOtpServiceClient(conn).WaitForOtp(reqCtx, &pb.WaitForOtpRequest{
			Purpose:         "gopay",
			TimeoutSeconds:  timeoutSeconds,
			IssuedAfterUnix: issuedAfterUnix,
		})
		if err != nil {
			otpCh <- otpServiceResult{err: fmt.Errorf("otp not received after %ds: %w", timeoutSeconds, err)}
			return
		}
		if resp.GetFound() && strings.TrimSpace(resp.GetOtp()) != "" {
			otpCh <- otpServiceResult{code: normalizeOTP(resp.GetOtp())}
			return
		}
		lastErr := resp.GetErrorMessage()
		if lastErr == "" {
			lastErr = "otp not found"
		}
		otpCh <- otpServiceResult{err: fmt.Errorf("otp not received after %ds: %s", timeoutSeconds, lastErr)}
	}()

	deadline := time.NewTimer(time.Duration(timeoutSeconds) * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	var lastErr error
	for {
		select {
		case otpResult := <-otpCh:
			if strings.TrimSpace(otpResult.code) != "" {
				return paymentOTPResult{Code: normalizeOTP(otpResult.code), Source: "webhook"}, nil
			}
			if otpResult.err != nil {
				lastErr = otpResult.err
			}
			otpCh = nil
		case <-ticker.C:
			code, found, err := s.consumeManualPaymentOtp(ctx, jobID, issuedAfterUnix)
			if err != nil {
				lastErr = err
				continue
			}
			if found {
				return paymentOTPResult{Code: code, Source: "manual"}, nil
			}
		case <-deadline.C:
			if lastErr != nil {
				return paymentOTPResult{}, fmt.Errorf("payment otp not received after %ds: %w", timeoutSeconds, lastErr)
			}
			return paymentOTPResult{}, fmt.Errorf("payment otp not received after %ds", timeoutSeconds)
		case <-ctx.Done():
			return paymentOTPResult{}, ctx.Err()
		}
	}
}

func (s *orchestratorServer) waitForManualPaymentConfirmation(ctx context.Context, jobID string, issuedAfterUnix int64) error {
	defer func() {
		_ = s.deleteJobParam(context.Background(), jobID, paymentManualConfirmParam)
		_ = s.deleteJobParam(context.Background(), jobID, paymentManualConfirmAtParam)
	}()

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			confirmed, err := s.consumeManualPaymentConfirmation(ctx, jobID, issuedAfterUnix)
			if err != nil {
				return err
			}
			if confirmed {
				return nil
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (s *orchestratorServer) consumeManualPaymentConfirmation(ctx context.Context, jobID string, issuedAfterUnix int64) (bool, error) {
	value, found, err := s.getJobParam(ctx, jobID, paymentManualConfirmParam)
	if err != nil || !found {
		return false, err
	}
	if !manualParamSubmittedAfter(ctx, s, jobID, paymentManualConfirmParam, paymentManualConfirmAtParam, issuedAfterUnix) {
		return false, nil
	}
	confirmed, err := strconv.ParseBool(strings.TrimSpace(value))
	if err != nil || !confirmed {
		if err := s.deleteJobParam(ctx, jobID, paymentManualConfirmParam); err != nil {
			return false, err
		}
		_ = s.deleteJobParam(ctx, jobID, paymentManualConfirmAtParam)
		return false, nil
	}
	if err := s.deleteJobParam(ctx, jobID, paymentManualConfirmParam); err != nil {
		return false, err
	}
	_ = s.deleteJobParam(ctx, jobID, paymentManualConfirmAtParam)
	return true, nil
}

func (s *orchestratorServer) consumeManualPaymentOtp(ctx context.Context, jobID string, issuedAfterUnix int64) (string, bool, error) {
	value, found, err := s.getJobParam(ctx, jobID, paymentOTPParam)
	if err != nil || !found {
		return "", false, err
	}
	if !manualParamSubmittedAfter(ctx, s, jobID, paymentOTPParam, paymentOTPSubmittedAtParam, issuedAfterUnix) {
		return "", false, nil
	}
	code := normalizeOTP(value)
	if code == "" {
		return "", false, s.deleteJobParam(ctx, jobID, paymentOTPParam)
	}
	if err := s.deleteJobParam(ctx, jobID, paymentOTPParam); err != nil {
		return "", false, err
	}
	_ = s.deleteJobParam(ctx, jobID, paymentOTPSubmittedAtParam)
	return code, true, nil
}

type jobParamStore interface {
	getJobParam(context.Context, string, string) (string, bool, error)
	deleteJobParam(context.Context, string, string) error
}

func manualOTPSubmittedAfter(ctx context.Context, store jobParamStore, jobID, otpParam, submittedAtParam string, issuedAfterUnix int64) bool {
	return manualParamSubmittedAfter(ctx, store, jobID, otpParam, submittedAtParam, issuedAfterUnix)
}

func manualParamSubmittedAfter(ctx context.Context, store jobParamStore, jobID, valueParam, submittedAtParam string, issuedAfterUnix int64) bool {
	if issuedAfterUnix <= 0 {
		return true
	}
	submittedAtValue, found, err := store.getJobParam(ctx, jobID, submittedAtParam)
	if err != nil || !found {
		_ = store.deleteJobParam(ctx, jobID, valueParam)
		_ = store.deleteJobParam(ctx, jobID, submittedAtParam)
		return false
	}
	submittedAt, err := strconv.ParseInt(strings.TrimSpace(submittedAtValue), 10, 64)
	if err != nil || submittedAt < issuedAfterUnix {
		_ = store.deleteJobParam(ctx, jobID, valueParam)
		_ = store.deleteJobParam(ctx, jobID, submittedAtParam)
		return false
	}
	return true
}

func (s *orchestratorServer) RegisterAccount(ctx context.Context, req *pb.RegisterAccountRequest) (*pb.RegisterAccountResponse, error) {
	jobID := uuid.NewString()
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		accountID = uuid.NewString()
	}
	var result RegisterAccountWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("register-"+jobID), RegisterAccountWorkflow, RegisterAccountWorkflowInput{
		JobID: jobID,
		Account: AccountSpec{
			AccountID: accountID,
			Email:     req.GetEmail(),
			Password:  req.GetPassword(),
		},
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.RegisterAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.RegisterAccountResponse{
		JobId:             result.JobID,
		SessionToken:      result.SessionToken,
		AccessToken:       result.AccessToken,
		PlusTrialEligible: result.PlusTrialEligible,
		ErrorMessage:      result.ErrorMessage,
		CheckoutUrl:       result.CheckoutURL,
	}, nil
}

func (s *orchestratorServer) ActivateAccount(ctx context.Context, req *pb.ActivateAccountRequest) (*pb.ActivateAccountResponse, error) {
	jobID := uuid.NewString()
	var result ActivateAccountWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("activate-"+jobID), ActivateAccountWorkflow, ActivateAccountWorkflowInput{
		JobID:       jobID,
		AccountID:   strings.TrimSpace(req.GetAccountId()),
		SourceJobID: req.GetJobId(),
		Action:      actionActivate,
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.ActivateAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.ActivateAccountResponse{
		JobId:        result.JobID,
		Success:      result.Success,
		ErrorMessage: result.ErrorMessage,
		ChargeRef:    result.ChargeRef,
		SnapToken:    result.SnapToken,
	}, nil
}

func (s *orchestratorServer) AutopayAccount(ctx context.Context, req *pb.ActivateAccountRequest) (*pb.ActivateAccountResponse, error) {
	jobID := uuid.NewString()
	var result AutoPayWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("autopay-"+jobID), AutoPayWorkflow, AutoPayWorkflowInput{
		JobID:       jobID,
		AccountID:   strings.TrimSpace(req.GetAccountId()),
		SourceJobID: req.GetJobId(),
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.ActivateAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.ActivateAccountResponse{
		JobId:        result.JobID,
		Success:      result.Success,
		ErrorMessage: result.ErrorMessage,
		ChargeRef:    result.ChargeRef,
		SnapToken:    result.SnapToken,
	}, nil
}

func (s *orchestratorServer) LoginAccount(ctx context.Context, req *pb.LoginAccountRequest) (*pb.LoginAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		return &pb.LoginAccountResponse{ErrorMessage: "account_id is required"}, nil
	}
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("login-session-"+jobID), LoginSessionWorkflow, LoginSessionWorkflowInput{
		JobID:     jobID,
		AccountID: accountID,
	})
	if err != nil {
		return &pb.LoginAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.LoginAccountResponse{JobId: jobID, Started: true}, nil
}

func (s *orchestratorServer) ProbeAccount(ctx context.Context, req *pb.ProbeAccountRequest) (*pb.ProbeAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		return &pb.ProbeAccountResponse{ErrorMessage: "account_id is required"}, nil
	}
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("probe-"+jobID), ProbeAccountWorkflow, ProbeAccountWorkflowInput{
		JobID:     jobID,
		AccountID: accountID,
	})
	if err != nil {
		return &pb.ProbeAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.ProbeAccountResponse{JobId: jobID, Started: true}, nil
}

func (s *orchestratorServer) RunGoPayCycle(ctx context.Context, req *pb.GoPayCycleRequest) (*pb.GoPayCycleResponse, error) {
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("gopay-cycle-"+jobID), GoPayCycleWorkflow, GoPayCycleWorkflowInput{
		JobID: jobID,
	})
	if err != nil {
		return &pb.GoPayCycleResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.GoPayCycleResponse{JobId: jobID, Started: true}, nil
}

func (s *orchestratorServer) RegisterAndActivateAccount(ctx context.Context, req *pb.RegisterAndActivateAccountRequest) (*pb.RegisterAndActivateAccountResponse, error) {
	jobID := uuid.NewString()
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		accountID = uuid.NewString()
	}
	var result RegisterAndActivateWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("register-activate-"+jobID), RegisterAndActivateWorkflow, RegisterAndActivateWorkflowInput{
		JobID: jobID,
		Account: AccountSpec{
			AccountID: accountID,
			Email:     req.GetEmail(),
			Password:  req.GetPassword(),
		},
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.RegisterAndActivateAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.RegisterAndActivateAccountResponse{
		JobId:             result.JobID,
		SessionToken:      result.SessionToken,
		AccessToken:       result.AccessToken,
		PlusTrialEligible: result.PlusTrialEligible,
		CheckoutUrl:       result.CheckoutURL,
		ActivationSuccess: result.ActivationSuccess,
		ErrorMessage:      result.ErrorMessage,
		ChargeRef:         result.ChargeRef,
		SnapToken:         result.SnapToken,
	}, nil
}

func (s *orchestratorServer) RegisterMailbox(ctx context.Context, req *pb.RegisterMailboxRequest) (*pb.RegisterMailboxResponse, error) {
	jobID := uuid.NewString()
	var result RegisterMailboxWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("register-mailbox-"+jobID), RegisterMailboxWorkflow, RegisterMailboxWorkflowInput{
		JobID:      jobID,
		ImportOnly: req.GetImportOnly(),
		AutoOAuth:  !req.GetImportOnly() && envBool("OUTLOOK_REGISTER_ENABLE_OAUTH2", true),
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.RegisterMailboxResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	mailboxes := make([]*pb.RegisteredMailbox, 0, len(result.Mailboxes))
	for _, mailbox := range result.Mailboxes {
		mailboxes = append(mailboxes, &pb.RegisteredMailbox{
			EmailAddress: mailbox.EmailAddress,
			Status:       mailbox.Status,
		})
	}
	return &pb.RegisterMailboxResponse{
		JobId:        result.JobID,
		Success:      result.Success,
		ExitCode:     result.ExitCode,
		ErrorMessage: result.ErrorMessage,
		Mailboxes:    mailboxes,
	}, nil
}

func (s *orchestratorServer) RunMailboxOAuth(ctx context.Context, req *pb.StartMailboxOAuthRequest) (*pb.StartMailboxOAuthResponse, error) {
	jobID := uuid.NewString()
	limit := req.GetLimit()
	if limit <= 0 {
		limit = 100
	}
	onlyMissing := req.GetOnlyMissing()
	if strings.TrimSpace(req.GetEmailAddress()) == "" {
		onlyMissing = true
	}
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions("mailbox-oauth-"+jobID), MailboxOAuthWorkflow, MailboxOAuthWorkflowInput{
		JobID:        jobID,
		EmailAddress: strings.TrimSpace(req.GetEmailAddress()),
		OnlyMissing:  onlyMissing,
		Limit:        limit,
	})
	if err != nil {
		return &pb.StartMailboxOAuthResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.StartMailboxOAuthResponse{JobId: jobID, Started: true}, nil
}

func (s *orchestratorServer) SubmitRegistrationOtp(ctx context.Context, req *pb.SubmitRegistrationOtpRequest) (*pb.SubmitRegistrationOtpResponse, error) {
	otp := normalizeOTP(req.GetOtp())
	if otp == "" {
		return &pb.SubmitRegistrationOtpResponse{Success: false, ErrorMessage: "otp is required"}, nil
	}

	jobID, otpParam, submittedAtParam, otpKind, err := s.resolveManualOTPJob(ctx, strings.TrimSpace(req.GetJobId()), strings.TrimSpace(req.GetAccountId()))
	if err != nil {
		return &pb.SubmitRegistrationOtpResponse{Success: false, ErrorMessage: err.Error()}, nil
	}

	if err := s.setJobParams(ctx, jobID, map[string]string{
		otpParam:         otp,
		submittedAtParam: strconv.FormatInt(time.Now().Unix(), 10),
	}); err != nil {
		return &pb.SubmitRegistrationOtpResponse{Success: false, JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	log.Printf("[orchestrator] %s otp submitted job=%s source=manual", otpKind, jobID)
	return &pb.SubmitRegistrationOtpResponse{Success: true, JobId: jobID}, nil
}

func (s *orchestratorServer) SubmitPaymentConfirmation(ctx context.Context, req *pb.SubmitPaymentConfirmationRequest) (*pb.SubmitPaymentConfirmationResponse, error) {
	jobID, err := s.resolveManualPaymentConfirmationJob(ctx, strings.TrimSpace(req.GetJobId()), strings.TrimSpace(req.GetAccountId()))
	if err != nil {
		return &pb.SubmitPaymentConfirmationResponse{Success: false, ErrorMessage: err.Error()}, nil
	}

	if err := s.setJobParams(ctx, jobID, map[string]string{
		paymentManualConfirmParam:   "true",
		paymentManualConfirmAtParam: strconv.FormatInt(time.Now().Unix(), 10),
	}); err != nil {
		return &pb.SubmitPaymentConfirmationResponse{Success: false, JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	log.Printf("[orchestrator] payment manual confirmation submitted job=%s", jobID)
	return &pb.SubmitPaymentConfirmationResponse{Success: true, JobId: jobID}, nil
}

func (s *orchestratorServer) resolveManualPaymentConfirmationJob(ctx context.Context, jobID, accountID string) (string, error) {
	if jobID != "" {
		var job db.Job
		err := s.db.WithContext(ctx).First(&job, "id = ?", jobID).Error
		if err != nil {
			if err == gorm.ErrRecordNotFound {
				return "", fmt.Errorf("job not found: %s", jobID)
			}
			return "", err
		}
		if err := validateManualPaymentConfirmationJob(&job); err != nil {
			return "", err
		}
		return job.ID, nil
	}
	if accountID == "" {
		return "", fmt.Errorf("job_id or account_id is required")
	}

	var job db.Job
	err := s.db.WithContext(ctx).
		Where("account_id = ? AND action IN ? AND status = ? AND last_step = ?", accountID, []string{actionActivate, actionAutopay, actionRegisterAndActivate}, statusRunning, stepGoPayPayment).
		Order("updated_at DESC").
		First(&job).Error
	if err != nil {
		if err == gorm.ErrRecordNotFound {
			return "", fmt.Errorf("running payment-confirming job not found for account %s", accountID)
		}
		return "", err
	}
	return job.ID, nil
}

func validateManualPaymentConfirmationJob(job *db.Job) error {
	if job == nil {
		return fmt.Errorf("job is required")
	}
	if job.Status != statusRunning {
		return fmt.Errorf("job is not running: %s", job.Status)
	}
	if job.LastStep != stepGoPayPayment {
		return fmt.Errorf("job is not waiting for payment confirmation: %s", job.LastStep)
	}
	switch job.Action {
	case actionActivate, actionAutopay, actionRegisterAndActivate:
		return nil
	default:
		return fmt.Errorf("job does not accept payment confirmation: %s", job.Action)
	}
}

func (s *orchestratorServer) resolveManualOTPJob(ctx context.Context, jobID, accountID string) (string, string, string, string, error) {
	if jobID != "" {
		var job db.Job
		err := s.db.WithContext(ctx).First(&job, "id = ?", jobID).Error
		if err != nil {
			if err == gorm.ErrRecordNotFound {
				return "", "", "", "", fmt.Errorf("job not found: %s", jobID)
			}
			return "", "", "", "", err
		}
		if job.Status != statusRunning {
			return "", "", "", "", fmt.Errorf("job is not running: %s", job.Status)
		}
		otpParam, submittedAtParam, otpKind, err := s.manualOTPParamsForJob(ctx, &job)
		if err != nil {
			return "", "", "", "", err
		}
		return jobID, otpParam, submittedAtParam, otpKind, nil
	}
	if accountID == "" {
		return "", "", "", "", fmt.Errorf("job_id or account_id is required")
	}

	var job db.Job
	err := s.db.WithContext(ctx).
		Where("account_id = ? AND action IN ? AND status = ?", accountID, []string{actionRegister, actionActivate, actionAutopay, actionGoPayCycle, actionRegisterAndActivate, actionLoginSession}, statusRunning).
		Order("updated_at DESC").
		First(&job).Error
	if err != nil {
		if err == gorm.ErrRecordNotFound {
			return "", "", "", "", fmt.Errorf("running otp-accepting job not found for account %s", accountID)
		}
		return "", "", "", "", err
	}
	otpParam, submittedAtParam, otpKind, err := s.manualOTPParamsForJob(ctx, &job)
	if err != nil {
		return "", "", "", "", err
	}
	return job.ID, otpParam, submittedAtParam, otpKind, nil
}

func (s *orchestratorServer) manualOTPParamsForJob(ctx context.Context, job *db.Job) (string, string, string, error) {
	if job != nil && job.Action == actionRegisterAndActivate && job.LastStep == stepRegisterAccount && job.ID != "" && s != nil && s.db != nil {
		var step db.JobStep
		err := s.db.WithContext(ctx).First(&step, "job_id = ? AND step_name = ?", job.ID, stepRegisterAccount).Error
		if err == nil && step.Status == statusSucceeded {
			return paymentOTPParam, paymentOTPSubmittedAtParam, "payment", nil
		}
		if err != nil && err != gorm.ErrRecordNotFound {
			return "", "", "", err
		}
	}
	return manualOTPParamsForJobSnapshot(job)
}

func manualOTPParamsForJobSnapshot(job *db.Job) (string, string, string, error) {
	if job == nil {
		return "", "", "", fmt.Errorf("job is required")
	}
	switch job.Action {
	case actionRegister, actionLoginSession:
		return registrationOTPParam, registrationOTPSubmittedAtParam, "registration", nil
	case actionActivate, actionAutopay, actionGoPayCycle:
		return paymentOTPParam, paymentOTPSubmittedAtParam, "payment", nil
	case actionRegisterAndActivate:
		if job.LastStep == stepEnsureLogon || job.LastStep == stepGoPayPayment {
			return paymentOTPParam, paymentOTPSubmittedAtParam, "payment", nil
		}
		return registrationOTPParam, registrationOTPSubmittedAtParam, "registration", nil
	default:
		return "", "", "", fmt.Errorf("job does not accept otp: %s", job.Action)
	}
}

func (s *orchestratorServer) GetJob(ctx context.Context, req *pb.GetJobRequest) (*pb.GetJobResponse, error) {
	jobID := strings.TrimSpace(req.GetJobId())
	if jobID == "" {
		return &pb.GetJobResponse{ErrorMessage: "job_id is required"}, nil
	}

	job, err := s.getJob(ctx, jobID)
	if err != nil {
		return &pb.GetJobResponse{ErrorMessage: err.Error()}, nil
	}

	var steps []db.JobStep
	if err := s.db.WithContext(ctx).Where("job_id = ?", jobID).Order("started_at ASC, step_name ASC").Find(&steps).Error; err != nil {
		return &pb.GetJobResponse{ErrorMessage: err.Error()}, nil
	}

	return &pb.GetJobResponse{Job: jobToProto(job, steps)}, nil
}

func (s *orchestratorServer) RequestAccount(ctx context.Context, req *pb.RequestAccountRequest) (*pb.RequestAccountResponse, error) {
	resp, err := s.RegisterAccount(ctx, &pb.RegisterAccountRequest{})
	if err != nil {
		return nil, err
	}

	return &pb.RequestAccountResponse{
		JobId:             resp.JobId,
		SessionToken:      resp.SessionToken,
		AccessToken:       resp.AccessToken,
		PlusTrialEligible: resp.PlusTrialEligible,
		ErrorMessage:      resp.ErrorMessage,
		CheckoutUrl:       resp.CheckoutUrl,
	}, nil
}

func jobToProto(job *db.Job, steps []db.JobStep) *pb.Job {
	if job == nil {
		return nil
	}
	out := &pb.Job{
		JobId:        job.ID,
		AccountId:    job.AccountID,
		Action:       job.Action,
		Status:       job.Status,
		Recoverable:  job.Recoverable,
		Retryable:    job.Retryable,
		LastStep:     job.LastStep,
		ErrorMessage: job.ErrorMessage,
		ResultJson:   job.ResultJSON,
		CreatedAt:    job.CreatedAt,
		UpdatedAt:    job.UpdatedAt,
		Steps:        make([]*pb.JobStep, 0, len(steps)),
	}
	for i := range steps {
		out.Steps = append(out.Steps, &pb.JobStep{
			StepName:     steps[i].StepName,
			Status:       steps[i].Status,
			Recoverable:  steps[i].Recoverable,
			Retryable:    steps[i].Retryable,
			ErrorMessage: steps[i].ErrorMessage,
			ResultJson:   steps[i].ResultJSON,
			StartedAt:    steps[i].StartedAt,
			CompletedAt:  steps[i].CompletedAt,
		})
	}
	return out
}

func browserStartData(resp *pb.StartRegisterResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present": true,
		"success":          resp.GetSuccess(),
		"error_message":    resp.GetErrorMessage(),
		"flow_id":          resp.GetFlowId(),
		"otp_required":     resp.GetOtpRequired(),
		"otp_issued_after": resp.GetOtpIssuedAfterUnix(),
		"result":           registerResultData(resp.GetResult()),
	}
}

func registerResultData(resp *pb.RegisterResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present":         true,
		"success":                  resp.GetSuccess(),
		"error_message":            resp.GetErrorMessage(),
		"session_token_present":    resp.GetSessionToken() != "",
		"access_token_present":     resp.GetAccessToken() != "",
		"device_id_present":        resp.GetDeviceId() != "",
		"plus_trial_eligible":      resp.GetPlusTrialEligible(),
		"plus_trial_checked":       resp.GetPlusTrialChecked(),
		"checkout_url_present":     resp.GetCheckoutUrl() != "",
		"sensitive_values_stored":  false,
		"credential_values_stored": false,
	}
}

func paymentStartData(resp *pb.StartGoPayResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present":   true,
		"success":            resp.GetSuccess(),
		"error_message":      resp.GetErrorMessage(),
		"flow_id":            resp.GetFlowId(),
		"snap_token_present": resp.GetSnapToken() != "",
		"issued_after_unix":  resp.GetIssuedAfterUnix(),
		"expires_at_unix":    resp.GetExpiresAtUnix(),
	}
}

func plusTrialProbeData(resp *pb.ProbePlusTrialPaymentResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present":     true,
		"success":              resp.GetSuccess(),
		"error_message":        resp.GetErrorMessage(),
		"checked":              resp.GetChecked(),
		"plus_trial_eligible":  resp.GetPlusTrialEligible(),
		"plus_active":          resp.GetPlusActive(),
		"plan_type":            resp.GetPlanType(),
		"tier":                 normalizeTier(resp.GetPlanType()),
		"amount":               resp.GetAmount(),
		"currency":             resp.GetCurrency(),
		"source":               resp.GetSource(),
		"checkout_url_present": resp.GetCheckoutUrl() != "",
	}
}

func tierProbeData(resp *pb.ProbeTierPaymentResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present": true,
		"success":          resp.GetSuccess(),
		"error_message":    resp.GetErrorMessage(),
		"checked":          resp.GetChecked(),
		"tier":             resp.GetTier(),
		"plus_active":      resp.GetPlusActive(),
		"source":           resp.GetSource(),
	}
}

func paymentResultData(resp *pb.GoPayResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present":             true,
		"success":                      resp.GetSuccess(),
		"error_message":                resp.GetErrorMessage(),
		"charge_ref":                   resp.GetChargeRef(),
		"snap_token_present":           resp.GetSnapToken() != "",
		"awaiting_manual_confirmation": resp.GetAwaitingManualConfirmation(),
		"deeplink_url":                 resp.GetDeeplinkUrl(),
		"qr_code_url":                  resp.GetQrCodeUrl(),
		"finish_redirect_url":          resp.GetFinishRedirectUrl(),
		"finish_200_redirect_url":      resp.GetFinish_200RedirectUrl(),
	}
}

func cleanupData(success bool, errorMessage string, err error) map[string]any {
	data := map[string]any{
		"called":        true,
		"success":       success,
		"error_message": errorMessage,
	}
	if err != nil {
		data["rpc_error"] = err.Error()
	}
	return data
}

func cleanupDataFromBrowser(resp *pb.CancelRegisterResponse, err error) map[string]any {
	if resp == nil {
		return cleanupData(false, "", err)
	}
	return cleanupData(resp.GetSuccess(), resp.GetErrorMessage(), err)
}

func cleanupDataFromPayment(resp *pb.CancelGoPayResponse, err error) map[string]any {
	if resp == nil {
		return cleanupData(false, "", err)
	}
	return cleanupData(resp.GetSuccess(), resp.GetErrorMessage(), err)
}

func (s *orchestratorServer) paymentOtpTimeout() int32 {
	if s.otpTimeout <= 0 {
		return 60
	}
	return s.otpTimeout
}

func (s *orchestratorServer) registrationOtpTimeout() int32 {
	if s.regOTPTimeout <= 0 {
		return 120
	}
	return s.regOTPTimeout
}

func normalizeOTP(value string) string {
	replacer := strings.NewReplacer(" ", "", "\t", "", "\n", "", "\r", "", "-", "")
	return strings.TrimSpace(replacer.Replace(value))
}

func normalizeTier(tier string) string {
	return strings.ToLower(strings.TrimSpace(tier))
}

func main() {
	log.Println("Initializing Orchestrator API...")

	browserAddr := envDefault("BROWSER_ADDR", "browser-reg:50051")
	browserConn, err := grpc.NewClient(browserAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to browser service: %v", err)
	}
	defer browserConn.Close()

	paymentAddr := envDefault("PAYMENT_ADDR", "host.docker.internal:50051")
	paymentConn, err := grpc.NewClient(paymentAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to payment service: %v", err)
	}
	defer paymentConn.Close()

	cycleAddr := envDefault("GOPAY_CYCLE_ADDR", "gopay-cycle:50051")
	cycleConn, err := grpc.NewClient(cycleAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to gopay-cycle service: %v", err)
	}
	defer cycleConn.Close()

	smsAddr := envDefault("SMS_SERVICE_ADDR", "sms-service:50051")
	smsConn, err := grpc.NewClient(smsAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to sms-service: %v", err)
	}
	defer smsConn.Close()

	accountDBAddr := envDefault("ACCOUNT_DB_ADDR", "account-db:50051")
	accountDBConn, err := grpc.NewClient(accountDBAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to account-db service: %v", err)
	}
	defer accountDBConn.Close()

	emailAddr := envDefault("EMAIL_ADDR", "outlook-imap-service:50051")
	emailConn, err := grpc.NewClient(emailAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to email service: %v", err)
	}
	defer emailConn.Close()

	mailboxRegisterAddr := envDefault("MAILBOX_REGISTER_ADDR", "outlook-register-service:50051")
	mailboxRegisterConn, err := grpc.NewClient(mailboxRegisterAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to mailbox registration service: %v", err)
	}
	defer mailboxRegisterConn.Close()

	temporalNamespace := envDefault("TEMPORAL_NAMESPACE", "default")
	temporalClient, closeTemporal, err := newTemporalClient(temporalNamespace)
	if err != nil {
		log.Fatalf("Failed to connect to Temporal: %v", err)
	}
	defer closeTemporal()

	listenAddr := envDefault("LISTEN_ADDR", ":50051")
	lis, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	server := &orchestratorServer{
		db:                                db.InitDB(),
		accountClient:                     pb.NewAccountDatabaseServiceClient(accountDBConn),
		browserClient:                     pb.NewBrowserRegistrationClient(browserConn),
		paymentClient:                     pb.NewPaymentServiceClient(paymentConn),
		cycleClient:                       pb.NewGopayCycleServiceClient(cycleConn),
		smsClient:                         pb.NewSMSServiceClient(smsConn),
		emailClient:                       pb.NewEmailServiceClient(emailConn),
		mailboxRegisterClient:             pb.NewMailboxRegistrationServiceClient(mailboxRegisterConn),
		otpAddr:                           envDefault("GOPAY_OTP_SERVICE_ADDR", envDefault("OTP_ADDR", "whatsapp-otp-relay:50051")),
		otpTimeout:                        envInt32("GOPAY_OTP_TIMEOUT_SECONDS", 60),
		regOTPTimeout:                     envInt32("REGISTRATION_OTP_TIMEOUT_SECONDS", 180),
		changePhoneMaxFailures:            envInt("GOPAY_CHANGE_PHONE_MAX_FAILURES", defaultChangePhoneMaxFailures),
		changePhoneDisabled:               envBool("GOPAY_CHANGE_PHONE_DISABLED", false),
		changePhoneOTPWaitSeconds:         envInt32("GOPAY_CHANGE_PHONE_OTP_WAIT_SECONDS", defaultChangePhoneOTPWaitSeconds),
		changePhoneOTPRetryAttempts:       envIntNonNegative("GOPAY_CHANGE_PHONE_OTP_RETRY_ATTEMPTS", defaultChangePhoneOTPRetryAttempts),
		changePhoneGetNumberRetryDelay:    envNonNegativeDurationSeconds("GOPAY_CHANGE_PHONE_GET_NUMBER_RETRY_SECONDS", defaultChangePhoneGetNumberRetryDelay),
		changePhoneSMSCancelTimeout:       envPositiveDurationSeconds("GOPAY_CHANGE_PHONE_SMS_CANCEL_TIMEOUT_SECONDS", defaultChangePhoneSMSCancelTimeout),
		changePhoneSMSCancelRetryInterval: envPositiveDurationSeconds("GOPAY_CHANGE_PHONE_SMS_CANCEL_RETRY_SECONDS", defaultChangePhoneSMSCancelRetryInterval),
		temporal:                          temporalClient,
		taskQueue:                         envDefault("TEMPORAL_TASK_QUEUE", taskQueueDefault),
	}

	temporalWorker := temporalworker.New(temporalClient, server.taskQueue, temporalworker.Options{})
	registerTemporalWorker(temporalWorker, server)
	go func() {
		if err := temporalWorker.Run(temporalworker.InterruptCh()); err != nil {
			log.Fatalf("Temporal worker failed: %v", err)
		}
	}()

	grpcServer := grpc.NewServer()
	pb.RegisterOrchestratorServiceServer(grpcServer, server)

	log.Printf("Orchestrator gRPC API listening on %s", listenAddr)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}

func (s *orchestratorServer) workflowOptions(workflowID string) temporalclient.StartWorkflowOptions {
	return temporalclient.StartWorkflowOptions{
		ID:        workflowID,
		TaskQueue: s.taskQueue,
	}
}

func newTemporalClient(namespace string) (temporalclient.Client, func(), error) {
	if envBool("TEMPORAL_DEV_SERVER", false) {
		options := testsuite.DevServerOptions{
			CachedDownload: testsuite.CachedDownload{
				Version: envDefault("TEMPORAL_DEV_SERVER_VERSION", "default"),
				DestDir: strings.TrimSpace(os.Getenv("TEMPORAL_DEV_SERVER_CACHE_DIR")),
			},
			ClientOptions: &temporalclient.Options{
				Namespace: namespace,
			},
			DBFilename: strings.TrimSpace(os.Getenv("TEMPORAL_DEV_SERVER_DB")),
			EnableUI:   envBool("TEMPORAL_DEV_SERVER_UI", false),
			UIPort:     strings.TrimSpace(os.Getenv("TEMPORAL_DEV_SERVER_UI_PORT")),
			LogLevel:   envDefault("TEMPORAL_DEV_SERVER_LOG_LEVEL", "warn"),
		}
		server, err := testsuite.StartDevServer(context.Background(), options)
		if err != nil {
			return nil, nil, err
		}
		log.Printf("Temporal dev server listening on %s", server.FrontendHostPort())
		client := server.Client()
		return client, func() {
			client.Close()
			if err := server.Stop(); err != nil {
				log.Printf("Temporal dev server stop failed: %v", err)
			}
		}, nil
	}

	temporalAddr := envDefault("TEMPORAL_ADDR", "host.docker.internal:7233")
	client, err := temporalclient.Dial(temporalclient.Options{
		HostPort:  temporalAddr,
		Namespace: namespace,
	})
	if err != nil {
		return nil, nil, err
	}
	return client, client.Close, nil
}

func envDefault(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func envInt32(key string, fallback int32) int32 {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	n, err := strconv.Atoi(value)
	if err != nil || n <= 0 {
		return fallback
	}
	return int32(n)
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

func envIntNonNegative(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	n, err := strconv.Atoi(value)
	if err != nil || n < 0 {
		return fallback
	}
	return n
}

func envPositiveDurationSeconds(key string, fallback time.Duration) time.Duration {
	seconds := envInt(key, int(fallback/time.Second))
	return time.Duration(seconds) * time.Second
}

func envNonNegativeDurationSeconds(key string, fallback time.Duration) time.Duration {
	seconds := envIntNonNegative(key, int(fallback/time.Second))
	return time.Duration(seconds) * time.Second
}

func envBool(key string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	if value == "" {
		return fallback
	}
	return value == "1" || value == "true" || value == "yes" || value == "on"
}
