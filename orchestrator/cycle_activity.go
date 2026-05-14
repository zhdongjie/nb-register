package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	pb "orchestrator/pb"
)

const (
	defaultChangePhoneMaxFailures            = 3
	defaultChangePhoneOTPWaitSeconds         = int32(120)
	defaultChangePhoneOTPRetryAttempts       = 1
	defaultChangePhoneGetNumberRetryDelay    = 5 * time.Second
	defaultChangePhoneSMSCancelTimeout       = 130 * time.Second
	defaultChangePhoneSMSCancelRetryInterval = 10 * time.Second
	gopayBalanceWaitTimeout                  = 2 * time.Minute
	gopayBalancePollInterval                 = 5 * time.Second
)

func (s *orchestratorServer) EnsureLogonActivity(ctx context.Context, input *pb.EnsureLogonRequest) (*pb.EnsureLogonResponse, error) {
	output := &pb.EnsureLogonResponse{}
	if input == nil {
		err := fmt.Errorf("ensure logon input is required")
		output.ErrorMessage = err.Error()
		return output, err
	}

	account, err := s.getAccount(ctx, input.GetAccountId())
	if err != nil {
		output.ErrorMessage = err.Error()
		return output, err
	}
	if err := accountEligibleForActivation(account); err != nil {
		output.ErrorMessage = err.Error()
		return output, err
	}

	_, err = s.runAtomicStep(ctx, input.GetJobId(), stepEnsureLogon, false, true, func() (any, error) {
		statusResp, statusErr := s.cycleStatus(ctx)
		output.StatusBefore = cycleStatusSnapshot(statusResp, statusErr)
		if statusErr != nil {
			output.ErrorMessage = statusErr.Error()
			return ensureLogonData(output), statusErr
		}

		tokenResp, err := s.ensureCycleSeedLogon(ctx, input.GetJobId())
		if err != nil {
			err = fmt.Errorf("ensure seed logon: %w", err)
			output.ErrorMessage = err.Error()
			return ensureLogonData(output), err
		}
		if tokenResp.GetTokenValid() {
			output.AlreadyReady = true
			output.CycleTokenReady = true
			return s.finishEnsureLogon(ctx, output)
		}

		activationID, err := s.cycleChangePhone(ctx)
		if err != nil {
			err = fmt.Errorf("cycle change phone: %w", err)
			output.ErrorMessage = err.Error()
			return ensureLogonData(output), err
		}
		output.ChangePhoneComplete = true

		if err := s.cycleDeactivate(ctx, activationID); err != nil {
			log.Printf("[cycle] Deactivate best-effort failed: %v", err)
		} else {
			output.DeactivateComplete = true
		}

		signupResult, err := s.cycleSignupAndCreatePin(ctx, input.GetJobId())
		if err != nil {
			err = fmt.Errorf("cycle signup: %w", err)
			output.ErrorMessage = err.Error()
			return ensureLogonData(output), err
		}
		signupResult.apply(output)

		output.CycleTokenReady = true

		return s.finishEnsureLogon(ctx, output)
	})
	if err != nil {
		if output.ErrorMessage == "" {
			output.ErrorMessage = err.Error()
		}
		return output, err
	}
	return output, nil
}

// CycleAndPayActivity 执行完整的 cycle + 支付流程
// 1. CheckTokenValid 2. CheckPhone+ChangePhone 3. Deactivate 4. Signup+CreatePin 5. 支付前等 1rp 6. 支付
func (s *orchestratorServer) CycleAndPayActivity(ctx context.Context, input GoPayActivityInput) (GoPayActivityOutput, error) {
	account, err := s.getAccount(ctx, input.AccountID)
	if err != nil {
		return GoPayActivityOutput{}, err
	}
	if err := accountEligibleForActivation(account); err != nil {
		return GoPayActivityOutput{}, err
	}

	data := map[string]any{"account_id": account.GetAccountId()}
	log.Printf("[cycle] Starting cycle for account %s", account.GetAccountId())

	// Step 0: seed token must be valid before changing the phone.
	tokenResp, err := s.ensureCycleSeedLogon(ctx, input.JobID)
	if err != nil {
		data["cycle_error"] = err.Error()
		log.Printf("[cycle] Ensure seed logon failed: %v", err)
		return GoPayActivityOutput{Data: data}, fmt.Errorf("ensure seed logon: %w", err)
	}
	if tokenResp.GetTokenValid() {
		data["cycle_token_ready"] = true
		log.Printf("[cycle] Token already ready, starting payment...")
	} else {
		// Step 1: 取号 + CheckPhone + 改号
		log.Printf("[cycle] Step 1: Change phone...")
		activationID, err := s.cycleChangePhone(ctx)
		if err != nil {
			data["cycle_error"] = err.Error()
			log.Printf("[cycle] Change phone failed: %v", err)
			return GoPayActivityOutput{Data: data}, fmt.Errorf("cycle change phone: %w", err)
		}

		// Step 2: 注销
		log.Printf("[cycle] Step 2: Deactivate...")
		if err := s.cycleDeactivate(ctx, activationID); err != nil {
			data["deactivate_error"] = err.Error()
			log.Printf("[cycle] Deactivate best-effort failed: %v", err)
		} else {
			data["deactivate_complete"] = true
		}

		// Step 3: signup + create pin
		log.Printf("[cycle] Step 3: Signup...")
		if _, err := s.cycleSignupAndCreatePin(ctx, input.JobID); err != nil {
			data["cycle_error"] = err.Error()
			log.Printf("[cycle] Signup failed: %v", err)
			return GoPayActivityOutput{Data: data}, fmt.Errorf("cycle signup: %w", err)
		}

		data["cycle_token_ready"] = true
		log.Printf("[cycle] Token ready, starting payment...")
	}

	// Step 5: 支付（用新 token）
	var result *pb.GoPayResponse
	_, err = s.runAtomicStep(ctx, input.JobID, stepGoPayPayment, false, true, func() (any, error) {
		var stepErr error
		result, data, stepErr = s.pay(ctx, input.JobID, account, "", "", true, "")
		return data, stepErr
	})
	if err != nil {
		log.Printf("[cycle] Payment failed: %v", err)
		return GoPayActivityOutput{Data: data}, err
	}
	log.Printf("[cycle] Payment success: %s", result.GetChargeRef())

	return GoPayActivityOutput{
		ChargeRef:         result.GetChargeRef(),
		SnapToken:         result.GetSnapToken(),
		PlusTrialEligible: true,
		PlusTrialChecked:  true,
		PlusActive:        true,
		Data:              data,
	}, nil
}

func (s *orchestratorServer) GoPayCycleLoginActivity(ctx context.Context, input GoPayCycleStepInput) (GoPayCycleStepOutput, error) {
	output := GoPayCycleStepOutput{Data: map[string]any{}}
	_, err := s.runAtomicStep(ctx, input.JobID, stepGoPayCycleLogin, false, true, func() (any, error) {
		data := output.Data
		statusBefore, statusErr := s.cycleStatus(ctx)
		data["status_before"] = cycleStatusSnapshotData(cycleStatusSnapshot(statusBefore, statusErr))
		if statusErr != nil {
			data["error_message"] = statusErr.Error()
			return data, statusErr
		}

		tokenResp, err := s.ensureCycleSeedLogon(ctx, input.JobID)
		data["check_token_valid"] = checkTokenValidData(tokenResp)
		if err != nil {
			data["error_message"] = err.Error()
			return data, err
		}

		statusAfter, statusErr := s.cycleStatus(ctx)
		data["status_after"] = cycleStatusSnapshotData(cycleStatusSnapshot(statusAfter, statusErr))
		if statusErr != nil {
			data["error_message"] = statusErr.Error()
			return data, statusErr
		}
		output.Ready = cycleStatusTokenReady(statusAfter)
		output.CycleTokenReady = tokenResp.GetTokenValid() || output.Ready
		output.Stage = statusAfter.GetStage()
		output.Phone = statusAfter.GetPhone()
		data["cycle_token_ready"] = output.CycleTokenReady
		data["ready"] = output.Ready
		return data, nil
	})
	return output, err
}

func (s *orchestratorServer) GoPayCycleChangePhoneActivity(ctx context.Context, input GoPayCycleStepInput) (GoPayCycleStepOutput, error) {
	output := GoPayCycleStepOutput{Data: map[string]any{}}
	_, err := s.runAtomicStep(ctx, input.JobID, stepGoPayCycleChangePhone, false, true, func() (any, error) {
		data := output.Data
		statusBefore, statusErr := s.cycleStatus(ctx)
		data["status_before"] = cycleStatusSnapshotData(cycleStatusSnapshot(statusBefore, statusErr))
		if statusErr != nil {
			data["error_message"] = statusErr.Error()
			return data, statusErr
		}

		activationID, err := s.cycleChangePhone(ctx)
		if err != nil {
			data["error_message"] = err.Error()
			return data, err
		}
		output.ActivationID = activationID
		output.ChangePhoneComplete = true
		data["activation_id"] = activationID
		data["change_phone_complete"] = true

		statusAfter, statusErr := s.cycleStatus(ctx)
		data["status_after"] = cycleStatusSnapshotData(cycleStatusSnapshot(statusAfter, statusErr))
		if statusErr != nil {
			data["error_message"] = statusErr.Error()
			return data, statusErr
		}
		output.Stage = statusAfter.GetStage()
		output.Phone = statusAfter.GetPhone()
		return data, nil
	})
	return output, err
}

func (s *orchestratorServer) cycleStatus(ctx context.Context) (*pb.StatusResponse, error) {
	if s.cycleClient == nil {
		return nil, fmt.Errorf("cycle client not configured")
	}
	stateJSON, err := s.loadGoPayCycleState(ctx)
	if err != nil {
		return nil, err
	}
	resp, err := s.cycleClient.Status(ctx, &pb.StatusRequest{StateJson: stateJSON})
	if err == nil {
		err = s.saveGoPayCycleState(ctx, resp.GetStateJson())
	}
	if err != nil {
		return resp, fmt.Errorf("Status: %w", err)
	}
	if resp == nil {
		return nil, fmt.Errorf("Status returned empty response")
	}
	return resp, nil
}

func (s *orchestratorServer) loadGoPayCycleState(ctx context.Context) (string, error) {
	if s.accountClient == nil {
		return "{}", fmt.Errorf("account database client not configured")
	}
	resp, err := s.accountClient.GetGoPayCycleState(ctx, &pb.GetGoPayCycleStateRequest{StateKey: goPayCycleStateKey})
	if err != nil {
		return "", fmt.Errorf("GetGoPayCycleState: %w", err)
	}
	stateJSON := strings.TrimSpace(resp.GetState().GetStateJson())
	if stateJSON == "" {
		stateJSON = "{}"
	}
	return stateJSON, nil
}

func (s *orchestratorServer) saveGoPayCycleState(ctx context.Context, stateJSON string) error {
	stateJSON = strings.TrimSpace(stateJSON)
	if stateJSON == "" {
		return nil
	}
	if s.accountClient == nil {
		return fmt.Errorf("account database client not configured")
	}
	_, err := s.accountClient.UpsertGoPayCycleState(ctx, &pb.UpsertGoPayCycleStateRequest{
		State: &pb.GoPayCycleState{
			StateKey:  goPayCycleStateKey,
			StateJson: stateJSON,
		},
	})
	if err != nil {
		return fmt.Errorf("UpsertGoPayCycleState: %w", err)
	}
	return nil
}

func cycleStatusTokenReady(resp *pb.StatusResponse) bool {
	return resp != nil && resp.GetStage() == "ready" && resp.GetTokenPresent()
}

func (s *orchestratorServer) validateCycleSeedToken(ctx context.Context) (*pb.CheckTokenValidResponse, error) {
	if s.cycleClient == nil {
		return nil, fmt.Errorf("cycle client not configured")
	}
	stateJSON, err := s.loadGoPayCycleState(ctx)
	if err != nil {
		return nil, err
	}
	resp, err := s.cycleClient.CheckTokenValid(ctx, &pb.CheckTokenValidRequest{StateJson: stateJSON})
	if err == nil {
		err = s.saveGoPayCycleState(ctx, resp.GetStateJson())
	}
	if err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, fmt.Errorf("empty response")
	}
	return resp, nil
}

func (s *orchestratorServer) ensureCycleSeedLogon(ctx context.Context, jobID string) (*pb.CheckTokenValidResponse, error) {
	resp, err := s.validateCycleSeedToken(ctx)
	if err != nil {
		return nil, err
	}
	if resp.GetTokenValid() {
		return resp, nil
	}

	message := strings.TrimSpace(resp.GetErrorMessage())
	if message == "" {
		message = "token invalid"
	}
	log.Printf("[cycle] CheckTokenValid requires login/signup: %s", message)
	if err := s.cycleLoginOrSignupSeed(ctx, jobID); err != nil {
		return resp, err
	}

	resp, err = s.validateCycleSeedToken(ctx)
	if err != nil {
		return nil, err
	}
	if !resp.GetTokenValid() {
		message := strings.TrimSpace(resp.GetErrorMessage())
		if message == "" {
			message = "token invalid"
		}
		return resp, fmt.Errorf("%s", message)
	}
	return resp, nil
}

func (s *orchestratorServer) waitForCycleMinBalance(ctx context.Context) error {
	if s.cycleClient == nil {
		return fmt.Errorf("cycle client not configured")
	}
	deadline := time.NewTimer(gopayBalanceWaitTimeout)
	defer deadline.Stop()
	ticker := time.NewTicker(gopayBalancePollInterval)
	defer ticker.Stop()

	var lastAmount int64
	lastCurrency := "IDR"
	var lastErr string
	for {
		var resp *pb.CheckTokenValidResponse
		var err error
		stateJSON, stateErr := s.loadGoPayCycleState(ctx)
		if stateErr != nil {
			lastErr = stateErr.Error()
			resp = nil
			err = stateErr
		} else {
			resp, err = s.cycleClient.CheckTokenValid(ctx, &pb.CheckTokenValidRequest{StateJson: stateJSON})
			if err == nil {
				err = s.saveGoPayCycleState(ctx, resp.GetStateJson())
			}
		}
		if err != nil {
			lastErr = err.Error()
		} else if resp == nil {
			lastErr = "empty response"
		} else {
			lastAmount = resp.GetBalanceAmount()
			if strings.TrimSpace(resp.GetBalanceCurrency()) != "" {
				lastCurrency = resp.GetBalanceCurrency()
			}
			if !resp.GetTokenValid() {
				message := strings.TrimSpace(resp.GetErrorMessage())
				if message == "" {
					message = "token invalid"
				}
				return fmt.Errorf("%s", message)
			}
			if resp.GetSuccess() && resp.GetHasMinBalance() {
				return nil
			}
			lastErr = strings.TrimSpace(resp.GetErrorMessage())
		}

		select {
		case <-ticker.C:
			continue
		case <-deadline.C:
			if lastErr != "" {
				return fmt.Errorf("gopay balance not ready after %s: %d %s; last_error=%s", gopayBalanceWaitTimeout, lastAmount, lastCurrency, lastErr)
			}
			return fmt.Errorf("gopay balance not ready after %s: %d %s", gopayBalanceWaitTimeout, lastAmount, lastCurrency)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (s *orchestratorServer) finishEnsureLogon(ctx context.Context, output *pb.EnsureLogonResponse) (map[string]any, error) {
	statusResp, statusErr := s.cycleStatus(ctx)
	output.StatusAfter = cycleStatusSnapshot(statusResp, statusErr)
	if statusErr != nil {
		output.ErrorMessage = statusErr.Error()
		return ensureLogonData(output), statusErr
	}
	if !cycleStatusTokenReady(statusResp) {
		err := fmt.Errorf("gopay-cycle not logged on after logon: stage=%s", statusResp.GetStage())
		output.ErrorMessage = err.Error()
		return ensureLogonData(output), err
	}
	output.Ready = true
	output.Stage = statusResp.GetStage()
	output.Phone = statusResp.GetPhone()
	output.LogonComplete = true
	return ensureLogonData(output), nil
}

func cycleStatusSnapshot(resp *pb.StatusResponse, err error) *pb.CycleStatusSnapshot {
	if resp == nil && err == nil {
		return nil
	}
	snapshot := &pb.CycleStatusSnapshot{}
	if resp != nil {
		snapshot.Stage = resp.GetStage()
		snapshot.Phone = resp.GetPhone()
		snapshot.TokenPresent = resp.GetTokenPresent()
		snapshot.DeviceFingerprint = resp.GetDeviceFingerprint()
		snapshot.DeactivatedAt = resp.GetDeactivatedAt()
		snapshot.ErrorMessage = resp.GetErrorMessage()
		snapshot.BalanceAmount = resp.GetBalanceAmount()
		snapshot.HasMinBalance = resp.GetHasMinBalance()
		snapshot.BalanceCurrency = resp.GetBalanceCurrency()
	}
	if err != nil {
		snapshot.ErrorMessage = err.Error()
	}
	return snapshot
}

func ensureLogonData(resp *pb.EnsureLogonResponse) map[string]any {
	if resp == nil || ensureLogonResponseEmpty(resp) {
		return nil
	}
	data := map[string]any{
		"ready":                 resp.GetReady(),
		"already_ready":         resp.GetAlreadyReady(),
		"change_phone_complete": resp.GetChangePhoneComplete(),
		"deactivate_complete":   resp.GetDeactivateComplete(),
		"logon_complete":        resp.GetLogonComplete(),
		"signup_complete":       resp.GetSignupComplete(),
		"signup_pin_complete":   resp.GetSignupPinComplete(),
		"cycle_token_ready":     resp.GetCycleTokenReady(),
	}
	if resp.GetStage() != "" {
		data["stage"] = resp.GetStage()
	}
	if resp.GetPhone() != "" {
		data["phone"] = resp.GetPhone()
	}
	if status := cycleStatusSnapshotData(resp.GetStatusBefore()); status != nil {
		data["status_before"] = status
	}
	if status := cycleStatusSnapshotData(resp.GetStatusAfter()); status != nil {
		data["status_after"] = status
	}
	if resp.GetErrorMessage() != "" {
		data["error_message"] = resp.GetErrorMessage()
	}
	return data
}

func ensureLogonResponseEmpty(resp *pb.EnsureLogonResponse) bool {
	return !resp.GetReady() &&
		resp.GetStage() == "" &&
		resp.GetPhone() == "" &&
		resp.GetStatusBefore() == nil &&
		resp.GetStatusAfter() == nil &&
		!resp.GetAlreadyReady() &&
		!resp.GetChangePhoneComplete() &&
		!resp.GetDeactivateComplete() &&
		!resp.GetLogonComplete() &&
		!resp.GetSignupComplete() &&
		!resp.GetSignupPinComplete() &&
		!resp.GetCycleTokenReady() &&
		resp.GetErrorMessage() == ""
}

func cycleStatusSnapshotData(snapshot *pb.CycleStatusSnapshot) map[string]any {
	if snapshot == nil || cycleStatusSnapshotEmpty(snapshot) {
		return nil
	}
	data := map[string]any{
		"token_present": snapshot.GetTokenPresent(),
	}
	if snapshot.GetStage() != "" {
		data["stage"] = snapshot.GetStage()
	}
	if snapshot.GetPhone() != "" {
		data["phone"] = snapshot.GetPhone()
	}
	if snapshot.GetDeviceFingerprint() != "" {
		data["device_fingerprint"] = snapshot.GetDeviceFingerprint()
	}
	if snapshot.GetDeactivatedAt() != 0 {
		data["deactivated_at"] = snapshot.GetDeactivatedAt()
	}
	if snapshot.GetErrorMessage() != "" {
		data["error_message"] = snapshot.GetErrorMessage()
	}
	if snapshot.GetBalanceAmount() != 0 || snapshot.GetBalanceCurrency() != "" || snapshot.GetHasMinBalance() {
		data["balance_amount"] = snapshot.GetBalanceAmount()
		data["has_min_balance"] = snapshot.GetHasMinBalance()
		if snapshot.GetBalanceCurrency() != "" {
			data["balance_currency"] = snapshot.GetBalanceCurrency()
		}
	}
	return data
}

func checkTokenValidData(resp *pb.CheckTokenValidResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	data := map[string]any{
		"response_present": true,
		"success":          resp.GetSuccess(),
		"token_valid":      resp.GetTokenValid(),
		"refreshed":        resp.GetRefreshed(),
		"has_min_balance":  resp.GetHasMinBalance(),
	}
	if resp.GetStage() != "" {
		data["stage"] = resp.GetStage()
	}
	if resp.GetPhone() != "" {
		data["phone"] = resp.GetPhone()
	}
	if resp.GetBalanceAmount() != 0 || resp.GetBalanceCurrency() != "" {
		data["balance_amount"] = resp.GetBalanceAmount()
		if resp.GetBalanceCurrency() != "" {
			data["balance_currency"] = resp.GetBalanceCurrency()
		}
	}
	if resp.GetErrorMessage() != "" {
		data["error_message"] = resp.GetErrorMessage()
	}
	return data
}

func cycleStatusSnapshotEmpty(snapshot *pb.CycleStatusSnapshot) bool {
	return snapshot.GetStage() == "" &&
		snapshot.GetPhone() == "" &&
		!snapshot.GetTokenPresent() &&
		snapshot.GetDeviceFingerprint() == "" &&
		snapshot.GetDeactivatedAt() == 0 &&
		snapshot.GetErrorMessage() == "" &&
		snapshot.GetBalanceAmount() == 0 &&
		!snapshot.GetHasMinBalance() &&
		snapshot.GetBalanceCurrency() == ""
}

func normalizeIndonesiaPhone(phone string) string {
	value := strings.TrimPrefix(strings.TrimSpace(phone), "+")
	if strings.HasPrefix(value, "62") {
		return strings.TrimPrefix(value[2:], "0")
	}
	return value
}

func checkPhoneStatus(resp *pb.CheckPhoneResponse) string {
	if resp == nil {
		return "error"
	}
	status := strings.ToLower(strings.TrimSpace(resp.GetStatus()))
	if status != "" {
		return status
	}
	if resp.GetAvailable() {
		return "available"
	}
	switch strings.ToUpper(strings.TrimSpace(resp.GetErrorMessage())) {
	case "PHONE_REGISTERED":
		return "registered"
	case "PHONE_EXHAUSTED":
		return "exhausted"
	}
	return "unavailable"
}

func (s *orchestratorServer) changePhoneMaxFailureCount() int {
	if s.changePhoneMaxFailures <= 0 {
		return defaultChangePhoneMaxFailures
	}
	return s.changePhoneMaxFailures
}

func (s *orchestratorServer) changePhoneOTPWaitTimeoutSeconds() int32 {
	if s.changePhoneOTPWaitSeconds <= 0 {
		return defaultChangePhoneOTPWaitSeconds
	}
	return s.changePhoneOTPWaitSeconds
}

func (s *orchestratorServer) changePhoneOTPRetryCount() int {
	if s.changePhoneOTPRetryAttempts < 0 {
		return defaultChangePhoneOTPRetryAttempts
	}
	return s.changePhoneOTPRetryAttempts
}

func (s *orchestratorServer) changePhoneGetNumberRetryInterval() time.Duration {
	if s.changePhoneGetNumberRetryDelay < 0 {
		return defaultChangePhoneGetNumberRetryDelay
	}
	return s.changePhoneGetNumberRetryDelay
}

func (s *orchestratorServer) changePhoneSMSCancelWaitTimeout() time.Duration {
	if s.changePhoneSMSCancelTimeout <= 0 {
		return defaultChangePhoneSMSCancelTimeout
	}
	return s.changePhoneSMSCancelTimeout
}

func (s *orchestratorServer) changePhoneSMSCancelRetryDelay() time.Duration {
	if s.changePhoneSMSCancelRetryInterval <= 0 {
		return defaultChangePhoneSMSCancelRetryInterval
	}
	return s.changePhoneSMSCancelRetryInterval
}

func (s *orchestratorServer) recordChangePhoneFailure(ctx context.Context, activationID string, failures *int, reason string) error {
	if activationID != "" {
		if err := s.cancelSMSActivationBeforeRotation(ctx, activationID); err != nil {
			return fmt.Errorf("cancel SMS activation before phone rotation after %s: %w", reason, err)
		}
	}
	*failures++
	maxFailures := s.changePhoneMaxFailureCount()
	log.Printf("[cycle] Change phone retryable failure %d/%d: %s", *failures, maxFailures, reason)
	if *failures >= maxFailures {
		return fmt.Errorf("failed to change phone after %d consecutive failures: %s", maxFailures, reason)
	}
	return nil
}

func (s *orchestratorServer) cycleChangePhone(ctx context.Context) (string, error) {
	if s.changePhoneDisabled {
		return "", fmt.Errorf("cycle change phone disabled by GOPAY_CHANGE_PHONE_DISABLED")
	}
	if s.cycleClient == nil || s.smsClient == nil {
		return "", fmt.Errorf("cycle or sms client not configured")
	}
	maxFailures := s.changePhoneMaxFailureCount()
	otpWaitSeconds := s.changePhoneOTPWaitTimeoutSeconds()
	otpRetryAttempts := s.changePhoneOTPRetryCount()

phoneAttempts:
	for failures := 0; failures < maxFailures; {
		// 取号
		numResp, err := s.smsClient.GetNumber(ctx, &pb.GetNumberRequest{})
		if err != nil {
			return "", fmt.Errorf("GetNumber: %w", err)
		}
		if !numResp.GetSuccess() {
			if err := s.recordChangePhoneFailure(ctx, "", &failures, fmt.Sprintf("GetNumber: %s", numResp.GetErrorMessage())); err != nil {
				return "", err
			}
			if delay := s.changePhoneGetNumberRetryInterval(); delay > 0 {
				if err := sleepContext(ctx, delay); err != nil {
					return "", fmt.Errorf("waiting to retry GetNumber: %w", err)
				}
			}
			continue
		}

		phone := normalizeIndonesiaPhone(numResp.GetPhone())
		activationID := numResp.GetActivationId()
		if phone == "" {
			if err := s.recordChangePhoneFailure(ctx, activationID, &failures, "empty phone from SMS service"); err != nil {
				return "", err
			}
			continue
		}

		checkResp, err := s.cycleClient.CheckPhone(ctx, &pb.CheckPhoneRequest{Phone: phone})
		if err != nil {
			if cancelErr := s.recordChangePhoneFailure(ctx, activationID, &failures, fmt.Sprintf("CheckPhone: %v", err)); cancelErr != nil {
				return "", cancelErr
			}
			continue
		}
		status := checkPhoneStatus(checkResp)
		if status != "available" {
			reason := fmt.Sprintf("CheckPhone status=%s", status)
			if checkResp.GetErrorMessage() != "" {
				reason = fmt.Sprintf("%s: %s", reason, checkResp.GetErrorMessage())
			}
			if err := s.recordChangePhoneFailure(ctx, activationID, &failures, reason); err != nil {
				return "", err
			}
			continue
		}

		// 改号
		stateJSON, err := s.loadGoPayCycleState(ctx)
		if err != nil {
			s.cancelSMSActivation(ctx, activationID)
			return "", err
		}
		changeResp, err := s.cycleClient.ChangePhoneStart(ctx, &pb.ChangePhoneStartRequest{
			NewPhone:  phone,
			StateJson: stateJSON,
		})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, changeResp.GetStateJson())
		}
		if err != nil {
			s.cancelSMSActivation(ctx, activationID)
			return "", fmt.Errorf("ChangePhoneStart: %w", err)
		}
		if !changeResp.GetSuccess() {
			if changeResp.GetErrorMessage() == "PHONE_REGISTERED" || changeResp.GetErrorMessage() == "PHONE_EXHAUSTED" {
				// 已注册的号码不要使用；换新号前必须确认旧 activation 已取消。
				if err := s.recordChangePhoneFailure(ctx, activationID, &failures, fmt.Sprintf("ChangePhoneStart: %s", changeResp.GetErrorMessage())); err != nil {
					return "", err
				}
				continue
			}
			s.cancelSMSActivation(ctx, activationID)
			return "", fmt.Errorf("ChangePhoneStart: %s", changeResp.GetErrorMessage())
		}

		// 通知 SMS 平台验证码已发送到该号码。
		if sentResp, err := s.smsClient.MarkSMSSent(ctx, &pb.MarkSMSSentRequest{ActivationId: activationID}); err != nil {
			s.cancelSMSActivation(ctx, activationID)
			return "", fmt.Errorf("MarkSMSSent: %w", err)
		} else if !sentResp.GetSuccess() {
			s.cancelSMSActivation(ctx, activationID)
			return "", fmt.Errorf("MarkSMSSent: %s", sentResp.GetErrorMessage())
		}

		// 等 OTP；超时后按 GoPay Activity 流量触发 /v2/otp/retry 再等一次。
		var otpCode string
		for otpAttempt := 0; otpAttempt <= otpRetryAttempts; otpAttempt++ {
			otpResp, err := s.smsClient.WaitOTP(ctx, &pb.WaitOTPRequest{
				ActivationId:   activationID,
				TimeoutSeconds: otpWaitSeconds,
			})
			if err != nil {
				s.cancelSMSActivation(ctx, activationID)
				return "", fmt.Errorf("WaitOTP: %w", err)
			}
			if otpResp.GetSuccess() {
				otpCode = otpResp.GetCode()
				break
			}
			if otpAttempt < otpRetryAttempts {
				stateJSON, stateErr := s.loadGoPayCycleState(ctx)
				if stateErr != nil {
					s.cancelSMSActivation(ctx, activationID)
					return "", stateErr
				}
				retryResp, err := s.cycleClient.ChangePhoneRetry(ctx, &pb.ChangePhoneRetryRequest{StateJson: stateJSON})
				if err == nil {
					err = s.saveGoPayCycleState(ctx, retryResp.GetStateJson())
				}
				if err != nil {
					s.cancelSMSActivation(ctx, activationID)
					return "", fmt.Errorf("ChangePhoneRetry after WaitOTP failure (%s): %w", otpResp.GetErrorMessage(), err)
				}
				if retryResp.GetSuccess() {
					log.Printf("[cycle] Change phone OTP retry requested activation=%s", activationID)
					continue
				}
				log.Printf("[cycle] Change phone OTP retry failed activation=%s: %s", activationID, retryResp.GetErrorMessage())
			}
			if err := s.cancelSMSActivationBeforeRotation(ctx, activationID); err != nil {
				return "", fmt.Errorf("cancel SMS activation before phone rotation after WaitOTP failure (%s): %w", otpResp.GetErrorMessage(), err)
			}
			failures++
			log.Printf("[cycle] Change phone retryable failure %d/%d: WaitOTP: %s", failures, maxFailures, otpResp.GetErrorMessage())
			if failures >= maxFailures {
				return "", fmt.Errorf("failed to change phone after %d consecutive failures: WaitOTP: %s", maxFailures, otpResp.GetErrorMessage())
			}
			continue phoneAttempts
		}
		if strings.TrimSpace(otpCode) == "" {
			s.cancelSMSActivation(ctx, activationID)
			return "", fmt.Errorf("WaitOTP returned empty code")
		}

		// 完成改号
		stateJSON, err = s.loadGoPayCycleState(ctx)
		if err != nil {
			s.finishSMSActivation(ctx, activationID)
			return "", err
		}
		completeResp, err := s.cycleClient.ChangePhoneComplete(ctx, &pb.ChangePhoneCompleteRequest{Otp: otpCode, StateJson: stateJSON})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, completeResp.GetStateJson())
		}
		if err != nil {
			s.finishSMSActivation(ctx, activationID)
			return "", fmt.Errorf("ChangePhoneComplete: %w", err)
		}
		if !completeResp.GetSuccess() {
			s.finishSMSActivation(ctx, activationID)
			return "", fmt.Errorf("ChangePhoneComplete: %s", completeResp.GetErrorMessage())
		}

		log.Printf("[cycle] Phone changed to +62%s", phone)
		return activationID, nil
	}
	return "", fmt.Errorf("failed to change phone after %d consecutive failures", maxFailures)
}

func (s *orchestratorServer) cycleDeactivate(ctx context.Context, activationID string) error {
	if s.cycleClient == nil || s.smsClient == nil {
		return fmt.Errorf("cycle or sms client not configured")
	}
	if activationID == "" {
		return fmt.Errorf("activation id missing")
	}

	// 发起注销
	stateJSON, err := s.loadGoPayCycleState(ctx)
	if err != nil {
		s.finishSMSActivation(ctx, activationID)
		return err
	}
	deactResp, err := s.cycleClient.DeactivateStart(ctx, &pb.DeactivateStartRequest{StateJson: stateJSON})
	if err == nil {
		err = s.saveGoPayCycleState(ctx, deactResp.GetStateJson())
	}
	if err != nil {
		s.finishSMSActivation(ctx, activationID)
		return fmt.Errorf("DeactivateStart: %w", err)
	}
	if !deactResp.GetSuccess() {
		s.finishSMSActivation(ctx, activationID)
		return fmt.Errorf("DeactivateStart: %s", deactResp.GetErrorMessage())
	}

	otpResp, err := s.smsClient.WaitOTP(ctx, &pb.WaitOTPRequest{ActivationId: activationID, TimeoutSeconds: 120})
	if err != nil || !otpResp.GetSuccess() {
		s.finishSMSActivation(ctx, activationID)
		if err != nil {
			return fmt.Errorf("WaitOTP deactivate: %w", err)
		}
		return fmt.Errorf("WaitOTP deactivate: %s", otpResp.GetErrorMessage())
	}

	stateJSON, err = s.loadGoPayCycleState(ctx)
	if err != nil {
		s.finishSMSActivation(ctx, activationID)
		return err
	}
	completeResp, err := s.cycleClient.DeactivateComplete(ctx, &pb.DeactivateCompleteRequest{Otp: otpResp.GetCode(), StateJson: stateJSON})
	if err == nil {
		err = s.saveGoPayCycleState(ctx, completeResp.GetStateJson())
	}
	if err != nil {
		s.finishSMSActivation(ctx, activationID)
		return fmt.Errorf("DeactivateComplete: %w", err)
	}
	if !completeResp.GetSuccess() {
		s.finishSMSActivation(ctx, activationID)
		return fmt.Errorf("DeactivateComplete: %s", completeResp.GetErrorMessage())
	}

	s.finishSMSActivation(ctx, activationID)
	log.Printf("[cycle] Deactivated")
	return nil
}

func (s *orchestratorServer) finishSMSActivation(ctx context.Context, activationID string) {
	if s.smsClient == nil || activationID == "" {
		return
	}
	resp, err := s.smsClient.FinishActivation(ctx, &pb.FinishActivationRequest{ActivationId: activationID})
	if err != nil {
		log.Printf("[cycle] FinishActivation failed: %v", err)
		return
	}
	if !resp.GetSuccess() {
		log.Printf("[cycle] FinishActivation failed: %s", resp.GetErrorMessage())
	}
}

func (s *orchestratorServer) cancelSMSActivationBeforeRotation(ctx context.Context, activationID string) error {
	if s.smsClient == nil {
		return fmt.Errorf("sms client not configured")
	}
	if activationID == "" {
		return fmt.Errorf("activation id missing")
	}

	deadline := time.Now().Add(s.changePhoneSMSCancelWaitTimeout())
	for {
		resp, err := s.smsClient.CancelActivation(ctx, &pb.CancelActivationRequest{ActivationId: activationID})
		if err != nil {
			return fmt.Errorf("CancelActivation: %w", err)
		}
		if smsCancelSettled(resp) {
			if resp != nil && !resp.GetSuccess() {
				log.Printf("[cycle] CancelActivation settled without ACCESS_CANCEL: %s", smsCancelResponseText(resp))
			}
			return nil
		}

		message := smsCancelResponseText(resp)
		if !smsEarlyCancelDenied(message) {
			return fmt.Errorf("CancelActivation: %s", message)
		}

		remaining := time.Until(deadline)
		if remaining <= 0 {
			return fmt.Errorf("CancelActivation: %s", message)
		}
		delay := minDuration(s.changePhoneSMSCancelRetryDelay(), remaining)
		log.Printf("[cycle] CancelActivation denied too early; retrying in %s", delay)
		if err := sleepContext(ctx, delay); err != nil {
			return fmt.Errorf("waiting to retry CancelActivation: %w", err)
		}
	}
}

func (s *orchestratorServer) cancelSMSActivation(ctx context.Context, activationID string) {
	if s.smsClient == nil || activationID == "" {
		return
	}
	resp, err := s.smsClient.CancelActivation(ctx, &pb.CancelActivationRequest{ActivationId: activationID})
	if err != nil {
		log.Printf("[cycle] CancelActivation failed: %v", err)
		return
	}
	if !resp.GetSuccess() {
		log.Printf("[cycle] CancelActivation failed: %s", resp.GetErrorMessage())
	}
}

func smsCancelSettled(resp *pb.CancelActivationResponse) bool {
	if resp == nil {
		return false
	}
	if resp.GetSuccess() {
		return true
	}
	message := strings.ToUpper(smsCancelResponseText(resp))
	return strings.Contains(message, "NO_ACTIVATION") || strings.Contains(message, "STATUS_CANCEL")
}

func smsEarlyCancelDenied(message string) bool {
	return strings.Contains(strings.ToUpper(message), "EARLY_CANCEL_DENIED")
}

func smsCancelResponseText(resp *pb.CancelActivationResponse) string {
	if resp == nil {
		return "empty response"
	}
	parts := []string{}
	if resp.GetErrorMessage() != "" {
		parts = append(parts, resp.GetErrorMessage())
	}
	if resp.GetRawResponse() != "" {
		parts = append(parts, resp.GetRawResponse())
	}
	if len(parts) == 0 {
		return "unknown error"
	}
	return strings.Join(parts, ": ")
}

func sleepContext(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

type cycleLogonResult struct {
	SignupComplete    bool
	SignupPinComplete bool
	CycleTokenReady   bool
}

func (r cycleLogonResult) apply(output *pb.EnsureLogonResponse) {
	if output == nil {
		return
	}
	output.SignupComplete = r.SignupComplete
	output.SignupPinComplete = r.SignupPinComplete
	output.CycleTokenReady = r.CycleTokenReady
}

func authOtpIssuedAfterUnix(resp *pb.StatusResponse, fallback int64) int64 {
	if resp == nil {
		return fallback
	}
	var issuedAfter int64
	switch strings.TrimSpace(resp.GetStage()) {
	case "login_otp_pending":
		issuedAfter = resp.GetLoginOtpSentAtUnix()
	case "signup_otp_pending":
		issuedAfter = resp.GetSignupOtpSentAtUnix()
	case "signup_pin_otp_pending":
		issuedAfter = resp.GetSignupPinOtpSentAtUnix()
	}
	if issuedAfter > 0 {
		return issuedAfter
	}
	return fallback
}

func (s *orchestratorServer) cycleLoginOrSignupSeed(ctx context.Context, jobID string) error {
	if s.cycleClient == nil {
		return fmt.Errorf("cycle client not configured")
	}

	for attempt := 0; attempt < 4; attempt++ {
		tokenResp, err := s.validateCycleSeedToken(ctx)
		if err != nil {
			return err
		}
		if tokenResp.GetTokenValid() {
			return nil
		}

		startedAt := time.Now().Unix()
		stateJSON, err := s.loadGoPayCycleState(ctx)
		if err != nil {
			return err
		}
		startResp, err := s.cycleClient.AuthStart(ctx, &pb.AuthStartRequest{StateJson: stateJSON})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, startResp.GetStateJson())
		}
		if err != nil {
			return fmt.Errorf("gopay auth start: %w", err)
		}
		if !startResp.GetSuccess() {
			return fmt.Errorf("gopay auth start: %s", startResp.GetErrorMessage())
		}
		if startResp.GetReady() {
			tokenResp, err = s.validateCycleSeedToken(ctx)
			if err != nil {
				return err
			}
			if tokenResp.GetTokenValid() {
				return nil
			}
			return fmt.Errorf("gopay auth start returned ready but token validation failed: %s", tokenResp.GetErrorMessage())
		}
		if !startResp.GetOtpSent() {
			statusResp, statusErr := s.cycleStatus(ctx)
			if statusErr != nil {
				return fmt.Errorf("Status after gopay auth start: %w", statusErr)
			}
			if cycleStatusTokenReady(statusResp) {
				return nil
			}
			if strings.TrimSpace(statusResp.GetStage()) == "signup_pin_required" {
				continue
			}
			return fmt.Errorf("gopay auth start did not send OTP and cycle not logged on: stage=%s", statusResp.GetStage())
		}

		statusResp, statusErr := s.cycleStatus(ctx)
		if statusErr != nil {
			return fmt.Errorf("Status after gopay auth start: %w", statusErr)
		}
		issuedAfterUnix := authOtpIssuedAfterUnix(statusResp, startedAt)
		otp, err := s.waitForPaymentOtp(ctx, jobID, issuedAfterUnix)
		if err != nil {
			return fmt.Errorf("waiting auth OTP: %w", err)
		}

		stateJSON, err = s.loadGoPayCycleState(ctx)
		if err != nil {
			return err
		}
		completeResp, err := s.cycleClient.AuthComplete(ctx, &pb.AuthCompleteRequest{Otp: otp.Code, StateJson: stateJSON})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, completeResp.GetStateJson())
		}
		if err != nil {
			return fmt.Errorf("gopay auth complete: %w", err)
		}
		if !completeResp.GetSuccess() {
			return fmt.Errorf("gopay auth complete: %s", completeResp.GetErrorMessage())
		}
		if completeResp.GetReady() {
			tokenResp, err = s.validateCycleSeedToken(ctx)
			if err != nil {
				return err
			}
			if tokenResp.GetTokenValid() {
				return nil
			}
			return fmt.Errorf("gopay auth complete returned ready but token validation failed: %s", tokenResp.GetErrorMessage())
		}
		if completeResp.GetPinSetupRequired() || strings.TrimSpace(completeResp.GetStage()) == "signup_pin_required" {
			continue
		}

		statusResp, statusErr = s.cycleStatus(ctx)
		if statusErr != nil {
			return fmt.Errorf("Status after gopay auth complete: %w", statusErr)
		}
		if cycleStatusTokenReady(statusResp) {
			return nil
		}
		if strings.TrimSpace(statusResp.GetStage()) == "signup_pin_required" {
			continue
		}
		return fmt.Errorf("gopay-cycle not logged on after auth: stage=%s", statusResp.GetStage())
	}
	return fmt.Errorf("gopay auth did not reach token-valid state")
}

func (s *orchestratorServer) cycleSignupAndCreatePin(ctx context.Context, jobID string) (cycleLogonResult, error) {
	if s.cycleClient == nil {
		return cycleLogonResult{}, fmt.Errorf("cycle client not configured")
	}

	result := cycleLogonResult{}
	statusResp, statusErr := s.cycleStatus(ctx)
	if statusErr != nil {
		return result, fmt.Errorf("Status before signup: %w", statusErr)
	}

	stage := strings.TrimSpace(statusResp.GetStage())
	if !cycleStatusTokenReady(statusResp) && stage != "signup_pin_required" && stage != "signup_pin_otp_pending" {
		issuedAfterUnix := int64(0)
		if stage == "signup_otp_pending" {
			log.Printf("[cycle] Signup OTP already pending; waiting through orchestrator OTP channel")
			issuedAfterUnix = statusResp.GetSignupOtpSentAtUnix()
			if issuedAfterUnix <= 0 {
				issuedAfterUnix = time.Now().Unix()
			}
		} else {
			startedAt := time.Now().Unix()
			stateJSON, err := s.loadGoPayCycleState(ctx)
			if err != nil {
				return result, err
			}
			startResp, err := s.cycleClient.SignupStart(ctx, &pb.SignupStartRequest{StateJson: stateJSON})
			if err == nil {
				err = s.saveGoPayCycleState(ctx, startResp.GetStateJson())
			}
			if err != nil {
				return result, fmt.Errorf("gopay signup start: %w", err)
			}
			if !startResp.GetSuccess() {
				return result, fmt.Errorf("gopay signup start: %s", startResp.GetErrorMessage())
			}
			if !startResp.GetOtpSent() {
				statusResp, statusErr = s.cycleStatus(ctx)
				if statusErr != nil {
					return result, fmt.Errorf("Status after gopay signup start: %w", statusErr)
				}
				stage = strings.TrimSpace(statusResp.GetStage())
				if cycleStatusTokenReady(statusResp) || stage == "signup_pin_required" || stage == "signup_pin_otp_pending" {
					result.SignupComplete = true
					goto createPin
				}
				return result, fmt.Errorf("gopay signup start did not send OTP and cycle not ready: stage=%s", statusResp.GetStage())
			}
			statusResp, statusErr = s.cycleStatus(ctx)
			if statusErr != nil {
				return result, fmt.Errorf("Status after gopay signup start: %w", statusErr)
			}
			issuedAfterUnix = authOtpIssuedAfterUnix(statusResp, startedAt)
		}

		otp, err := s.waitForPaymentOtp(ctx, jobID, issuedAfterUnix)
		if err != nil {
			return result, fmt.Errorf("waiting signup OTP: %w", err)
		}

		stateJSON, err := s.loadGoPayCycleState(ctx)
		if err != nil {
			return result, err
		}
		completeResp, err := s.cycleClient.SignupComplete(ctx, &pb.SignupCompleteRequest{Otp: otp.Code, StateJson: stateJSON})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, completeResp.GetStateJson())
		}
		if err != nil {
			return result, fmt.Errorf("gopay signup complete: %w", err)
		}
		if !completeResp.GetSuccess() {
			return result, fmt.Errorf("gopay signup complete: %s", completeResp.GetErrorMessage())
		}
		result.SignupComplete = true
	}

createPin:
	statusResp, statusErr = s.cycleStatus(ctx)
	if statusErr != nil {
		return result, fmt.Errorf("Status before create pin: %w", statusErr)
	}
	if cycleStatusTokenReady(statusResp) && strings.TrimSpace(statusResp.GetStage()) != "signup_pin_required" {
		return result, nil
	}

	stage = strings.TrimSpace(statusResp.GetStage())
	issuedAfterUnix := int64(0)
	if stage == "signup_pin_otp_pending" {
		log.Printf("[cycle] Signup PIN OTP already pending; waiting through orchestrator OTP channel")
		issuedAfterUnix = statusResp.GetSignupPinOtpSentAtUnix()
		if issuedAfterUnix <= 0 {
			issuedAfterUnix = time.Now().Unix()
		}
	} else {
		startedAt := time.Now().Unix()
		stateJSON, err := s.loadGoPayCycleState(ctx)
		if err != nil {
			return result, err
		}
		startResp, err := s.cycleClient.CreatePinStart(ctx, &pb.CreatePinStartRequest{StateJson: stateJSON})
		if err == nil {
			err = s.saveGoPayCycleState(ctx, startResp.GetStateJson())
		}
		if err != nil {
			return result, fmt.Errorf("gopay create pin start: %w", err)
		}
		if !startResp.GetSuccess() {
			return result, fmt.Errorf("gopay create pin start: %s", startResp.GetErrorMessage())
		}
		if !startResp.GetOtpSent() {
			statusResp, statusErr = s.cycleStatus(ctx)
			if statusErr != nil {
				return result, fmt.Errorf("Status after gopay create pin start: %w", statusErr)
			}
			if cycleStatusTokenReady(statusResp) {
				result.SignupPinComplete = true
				return result, nil
			}
			return result, fmt.Errorf("gopay create pin start did not send OTP and cycle not ready: stage=%s", statusResp.GetStage())
		}
		statusResp, statusErr = s.cycleStatus(ctx)
		if statusErr != nil {
			return result, fmt.Errorf("Status after gopay create pin start: %w", statusErr)
		}
		issuedAfterUnix = authOtpIssuedAfterUnix(statusResp, startedAt)
	}

	otp, err := s.waitForPaymentOtp(ctx, jobID, issuedAfterUnix)
	if err != nil {
		return result, fmt.Errorf("waiting create pin OTP: %w", err)
	}

	stateJSON, err := s.loadGoPayCycleState(ctx)
	if err != nil {
		return result, err
	}
	completeResp, err := s.cycleClient.CreatePinComplete(ctx, &pb.CreatePinCompleteRequest{Otp: otp.Code, StateJson: stateJSON})
	if err == nil {
		err = s.saveGoPayCycleState(ctx, completeResp.GetStateJson())
	}
	if err != nil {
		return result, fmt.Errorf("gopay create pin complete: %w", err)
	}
	if !completeResp.GetSuccess() {
		return result, fmt.Errorf("gopay create pin complete: %s", completeResp.GetErrorMessage())
	}
	result.SignupPinComplete = true
	return result, nil
}
