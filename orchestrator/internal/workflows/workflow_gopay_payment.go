package workflows

import (
	"fmt"
	"strings"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

func GoPayPaymentWorkflow(ctx workflow.Context, input GoPayPaymentWorkflowInput) (GoPayPaymentWorkflowResult, error) {
	if normalizeGoPayOTPChannel(input.GetOtpChannel()) == "wa" {
		return goPayWAPaymentWorkflow(ctx, input)
	}
	return goPaySMSPaymentWorkflow(ctx, input)
}

func goPaySMSPaymentWorkflow(ctx workflow.Context, input GoPayPaymentWorkflowInput) (GoPayPaymentWorkflowResult, error) {
	progress := newWorkflowProgress(ctx, "GoPayPaymentWorkflow", input.GetJobId())
	result := GoPayPaymentWorkflowResult{JobId: input.GetJobId()}
	defer func() {
		finishWorkflowProgressOnError(ctx, progress, result.GetErrorMessage())
	}()

	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))
	gopayCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(30*time.Minute))
	paymentCtx := workflow.WithActivityOptions(ctx, paymentActivityOptions())
	tierCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(2*time.Minute))

	otpChannel := normalizeGoPayOTPChannel(input.GetOtpChannel())
	if otpChannel == "" {
		otpChannel = "sms"
	}
	stateKey := strings.TrimSpace(input.GetStateKey())
	if stateKey == "" {
		stateKey = goPayLocalSource
	}
	result.StateKey = stateKey
	addBalance := input.GetAddBalance()
	addBalanceMethod := goPayAddBalanceMethod(addBalance)
	stateJSON := "{}"
	combined := map[string]any{
		"otp_channel":        otpChannel,
		"add_balance_method": addBalanceMethod,
	}

	var account AccountRef
	setWorkflowProgress(ctx, progress, "resolve_account")
	if err := workflow.ExecuteActivity(retryCtx, resolveAccountActivityName, ResolveAccountInput{
		AccountId:   input.GetAccountId(),
		SourceJobId: input.GetSourceJobId(),
	}).Get(ctx, &account); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}
	combined["account_id"] = account.GetAccountId()

	setWorkflowProgress(ctx, progress, "create_job")
	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
		Action:    actionGoPayPayment,
		Params: map[string]string{
			"otp_channel":        otpChannel,
			"add_balance_method": addBalanceMethod,
			"state_key":          stateKey,
		},
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var probe ProbePlusTrialActivityOutput
	setWorkflowProgress(ctx, progress, stepProbePlusTrial)
	if err := workflow.ExecuteActivity(atomicCtx, probePlusTrialActivityName, ProbePlusTrialActivityInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
	}).Get(ctx, &probe); err != nil {
		combined["probe_plus_trial"] = protoDataMap(probe.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["probe_plus_trial"] = protoDataMap(probe.GetData())
	if !probe.GetChecked() {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedRetryable, false, true, fmt.Errorf("plus trial eligibility is unknown"), combined), nil
	}
	if !probe.GetPlusTrialEligible() {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedFinal, false, false, fmt.Errorf("account is not plus trial eligible"), combined), nil
	}

	var otpOpts goPayAppOTPOptions
	var signup GoPayAppStepOutput
	signupAttempts := []any{}

	for attempt := 1; attempt <= goPayAppSignupMaxPhoneAttempts; attempt++ {
		attemptData := map[string]any{"attempt": attempt}

		var phone GoPayAppAcquireSignupPhoneOutput
		setWorkflowProgress(ctx, progress, stepGoPayAppSignupPhone)
		if err := workflow.ExecuteActivity(gopayCtx, goPayAppAcquireSignupPhoneActivityName, GoPayAppAcquireSignupPhoneInput{
			JobId:        input.GetJobId(),
			FailureCount: int32(attempt - 1),
		}).Get(ctx, &phone); err != nil {
			attemptData["signup_phone"] = protoDataMap(phone.GetData())
			attemptData["error_message"] = err.Error()
			signupAttempts = append(signupAttempts, attemptData)
			combined["signup_attempts"] = signupAttempts
			combined["signup_phone"] = protoDataMap(phone.GetData())
			result.ActivationId = phone.GetActivationId()
			result.Phone = phone.GetPhone()
			if attempt < goPayAppSignupMaxPhoneAttempts && isGoPaySignupPhoneRotatableError(err) {
				continue
			}
			return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignupPhone, statusFailedRetryable, false, true, err, combined), nil
		}
		attemptData["signup_phone"] = protoDataMap(phone.GetData())
		combined["signup_phone"] = protoDataMap(phone.GetData())
		result.ActivationId = phone.GetActivationId()
		result.Phone = phone.GetPhone()

		otpOpts = goPayAppOTPOptions{
			Phone:           phone.GetPhone(),
			OTPChannel:      otpChannel,
			SMSActivationID: phone.GetActivationId(),
			Source:          stateKey,
			ResetState:      true,
			StateJSON:       stateJSON,
		}

		setWorkflowProgress(ctx, progress, stepGoPayAppSignup)
		currentSignup, err := runGoPayAppSignup(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
		signup = currentSignup
		stateJSON = signup.GetStateJson()
		attemptData["signup"] = protoDataMap(signup.GetData())
		if err == nil {
			signupAttempts = append(signupAttempts, attemptData)
			combined["signup_attempts"] = signupAttempts
			combined["signup"] = protoDataMap(signup.GetData())
			break
		}

		attemptData["error_message"] = err.Error()
		if isGoPaySignupOTPNotReceived(err) {
			var discarded GoPayAppSMSActivationOutput
			discardErr := workflow.ExecuteActivity(retryCtx, goPayAppDiscardSignupPhoneActivityName, GoPayAppSMSActivationInput{
				JobId:        input.GetJobId(),
				ActivationId: phone.GetActivationId(),
				FailureCount: int32(attempt),
				Reason:       err.Error(),
			}).Get(ctx, &discarded)
			attemptData["discard_signup_phone"] = protoDataMap(discarded.GetData())
			if discardErr != nil {
				attemptData["discard_error_message"] = discardErr.Error()
			}
			signupAttempts = append(signupAttempts, attemptData)
			combined["signup_attempts"] = signupAttempts
			combined["signup"] = protoDataMap(signup.GetData())
			if attempt < goPayAppSignupMaxPhoneAttempts {
				continue
			}
			return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignup, statusFailedRetryable, false, true, err, combined), nil
		}

		signupAttempts = append(signupAttempts, attemptData)
		combined["signup_attempts"] = signupAttempts
		combined["signup"] = protoDataMap(signup.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignup, statusFailedRetryable, false, true, err, combined), nil
	}
	result.SignupComplete = signup.GetSignupComplete()

	var balance GoPayAppAddBalanceOutput
	setWorkflowProgress(ctx, progress, stepGoPayAppAddBalance)
	addBalanceCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
	if err := workflow.ExecuteActivity(addBalanceCtx, goPayAppAddBalanceActivityName, GoPayAppAddBalanceInput{
		JobId:      input.GetJobId(),
		StateJson:  stateJSON,
		AddBalance: addBalance,
	}).Get(ctx, &balance); err != nil {
		stateJSON = balance.GetStateJson()
		combined["add_balance"] = protoDataMap(balance.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppAddBalance, statusFailedRetryable, false, true, err, combined), nil
	}
	stateJSON = balance.GetStateJson()
	combined["add_balance"] = protoDataMap(balance.GetData())
	result.AddBalanceMethod = balance.GetMethod()
	result.AddBalanceStatus = balance.GetStatus()
	if goPayAddBalanceMethod(addBalance) == "manual_transfer" {
		setWorkflowProgress(ctx, progress, stepGoPayAppAddBalanceConfirm)
		if err := waitForManualAddBalance(ctx, input.GetAddBalanceConfirmTimeoutSeconds()); err != nil {
			combined["add_balance_confirmation"] = map[string]any{
				"confirmed": false,
				"method":    "manual_transfer",
			}
			return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppAddBalanceConfirm, statusFailedRetryable, false, true, err, combined), nil
		}
		combined["add_balance_confirmation"] = map[string]any{
			"confirmed": true,
			"method":    "manual_transfer",
		}
		result.AddBalanceStatus = "confirmed"
	}
	result.AddBalanceComplete = true

	var paymentPrepare GoPayPaymentPrepareOutput
	setWorkflowProgress(ctx, progress, stepGoPayPaymentPrepare)
	paymentPrepare, err := prepareGoPayPayment(ctx, paymentCtx, GoPayActivityInput{
		JobId:             input.GetJobId(),
		AccountId:         account.GetAccountId(),
		UseAccountToken:   false,
		Tokenization:      "true",
		CheckoutUrl:       probe.GetCheckoutUrl(),
		CheckoutSessionId: probe.GetCheckoutSessionId(),
		GopayPhone:        result.GetPhone(),
		StateJson:         stateJSON,
	})
	stateJSON = paymentPrepare.GetStateJson()
	combined["gopay_payment_prepare"] = protoDataMap(paymentPrepare.GetData())
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayPaymentPrepare, statusFailedRetryable, false, true, err, combined), nil
	}

	setWorkflowProgress(ctx, progress, stepGoPayAppCreatePin)
	otpOpts.StateJSON = stateJSON
	createPin, err := runGoPayAppCreatePin(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
	stateJSON = createPin.GetStateJson()
	if err != nil {
		cancelGoPayPayment(ctx, retryCtx, paymentPrepare.GetFlowId())
		combined["create_pin"] = protoDataMap(createPin.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["create_pin"] = protoDataMap(createPin.GetData())
	result.SignupPinComplete = createPin.GetSignupPinComplete()
	result.AccountTokenReady = createPin.GetAccountTokenReady()
	if !createPin.GetAccountTokenReady() {
		cancelGoPayPayment(ctx, retryCtx, paymentPrepare.GetFlowId())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, fmt.Errorf("gopay account token is not ready after create pin"), combined), nil
	}

	var payment GoPayActivityOutput
	setWorkflowProgress(ctx, progress, stepGoPayPayment)
	payment, err = runGoPayPayment(ctx, paymentCtx, retryCtx, GoPayActivityInput{
		JobId:             input.GetJobId(),
		AccountId:         account.GetAccountId(),
		UseAccountToken:   true,
		Tokenization:      "true",
		CheckoutUrl:       probe.GetCheckoutUrl(),
		CheckoutSessionId: probe.GetCheckoutSessionId(),
		PreparedFlowId:    paymentPrepare.GetFlowId(),
		GopayPhone:        result.GetPhone(),
		OtpChannel:        otpChannel,
		SmsActivationId:   otpOpts.SMSActivationID,
		StateKey:          stateKey,
		StateJson:         stateJSON,
	})
	stateJSON = payment.GetStateJson()
	if err != nil {
		combined["gopay_payment"] = protoDataMap(payment.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayPayment, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["gopay_payment"] = protoDataMap(payment.GetData())

	var tier ProbeTierActivityOutput
	setWorkflowProgress(ctx, progress, stepProbeTier)
	if err := workflow.ExecuteActivity(tierCtx, probeTierActivityName, ProbeTierActivityInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
	}).Get(ctx, &tier); err != nil {
		combined["probe_tier"] = protoDataMap(tier.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbeTier, statusFailedRecoverable, true, false, err, combined), nil
	}
	combined["probe_tier"] = protoDataMap(tier.GetData())

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobId:  input.GetJobId(),
		Result: protoData(combined),
	}).Get(ctx, nil)

	result.Success = true
	result.ChargeRef = payment.GetChargeRef()
	result.SnapToken = payment.GetSnapToken()
	setWorkflowProgressSucceeded(ctx, progress)
	return result, nil
}

func goPayWAPaymentWorkflow(ctx workflow.Context, input GoPayPaymentWorkflowInput) (GoPayPaymentWorkflowResult, error) {
	progress := newWorkflowProgress(ctx, "GoPayPaymentWorkflow", input.GetJobId())
	result := GoPayPaymentWorkflowResult{JobId: input.GetJobId()}
	defer func() {
		finishWorkflowProgressOnError(ctx, progress, result.GetErrorMessage())
	}()

	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))
	gopayCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(30*time.Minute))
	paymentCtx := workflow.WithActivityOptions(ctx, paymentActivityOptions())
	tierCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(2*time.Minute))

	stateKey := strings.TrimSpace(input.GetStateKey())
	if stateKey == "" {
		stateKey = goPayLocalSource
	}
	result.StateKey = stateKey
	addBalance := input.GetAddBalance()
	addBalanceMethod := goPayAddBalanceMethod(addBalance)
	stateJSON := "{}"
	combined := map[string]any{
		"otp_channel":        "wa",
		"state_key":          stateKey,
		"add_balance_method": addBalanceMethod,
	}

	var account AccountRef
	setWorkflowProgress(ctx, progress, "resolve_account")
	if err := workflow.ExecuteActivity(retryCtx, resolveAccountActivityName, ResolveAccountInput{
		AccountId:   input.GetAccountId(),
		SourceJobId: input.GetSourceJobId(),
	}).Get(ctx, &account); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}
	combined["account_id"] = account.GetAccountId()

	setWorkflowProgress(ctx, progress, "create_job")
	params := map[string]string{
		"otp_channel":        "wa",
		"state_key":          stateKey,
		"add_balance_method": addBalanceMethod,
	}
	if strings.TrimSpace(input.GetWaPhone()) != "" {
		params["wa_phone"] = strings.TrimSpace(input.GetWaPhone())
	}
	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
		Action:    actionGoPayPayment,
		Params:    params,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var probe ProbePlusTrialActivityOutput
	setWorkflowProgress(ctx, progress, stepProbePlusTrial)
	if err := workflow.ExecuteActivity(atomicCtx, probePlusTrialActivityName, ProbePlusTrialActivityInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
	}).Get(ctx, &probe); err != nil {
		combined["probe_plus_trial"] = protoDataMap(probe.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["probe_plus_trial"] = protoDataMap(probe.GetData())
	if !probe.GetChecked() {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedRetryable, false, true, fmt.Errorf("plus trial eligibility is unknown"), combined), nil
	}
	if !probe.GetPlusTrialEligible() {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbePlusTrial, statusFailedFinal, false, false, fmt.Errorf("account is not plus trial eligible"), combined), nil
	}

	var waPhone GoPayResolveWAPhoneOutput
	setWorkflowProgress(ctx, progress, stepGoPayAppWAPhoneCheck)
	if err := workflow.ExecuteActivity(gopayCtx, goPayResolveWAPhoneActivityName, GoPayResolveWAPhoneInput{
		JobId:    input.GetJobId(),
		StateKey: stateKey,
		WaPhone:  input.GetWaPhone(),
	}).Get(ctx, &waPhone); err != nil {
		combined["wa_phone"] = protoDataMap(waPhone.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppWAPhoneCheck, statusFailedRetryable, false, true, err, combined), nil
	}
	stateKey = waPhone.GetStateKey()
	result.StateKey = stateKey
	result.WaPhone = waPhone.GetWaPhone()
	result.Phone = waPhone.GetWaPhone()
	combined["wa_phone"] = result.GetWaPhone()
	combined["wa_phone_resolution"] = protoDataMap(waPhone.GetData())

	otpOpts := goPayAppOTPOptions{
		Phone:      result.GetWaPhone(),
		OTPChannel: "wa",
		Source:     stateKey,
		ResetState: true,
		StateJSON:  stateJSON,
	}
	setWorkflowProgress(ctx, progress, stepGoPayAppSignup)
	signup, err := runGoPayAppSignup(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
	stateJSON = signup.GetStateJson()
	combined["signup"] = protoDataMap(signup.GetData())
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignup, statusFailedRetryable, false, true, err, combined), nil
	}
	result.SignupComplete = signup.GetSignupComplete()

	setWorkflowProgress(ctx, progress, stepGoPayAppCreatePin)
	otpOpts.StateJSON = stateJSON
	createPin, err := runGoPayAppCreatePin(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
	stateJSON = createPin.GetStateJson()
	combined["create_pin"] = protoDataMap(createPin.GetData())
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, err, combined), nil
	}
	result.SignupPinComplete = createPin.GetSignupPinComplete()
	result.AccountTokenReady = createPin.GetAccountTokenReady()
	if !createPin.GetAccountTokenReady() {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, fmt.Errorf("gopay account token is not ready after create pin"), combined), nil
	}

	var balance GoPayAppAddBalanceOutput
	setWorkflowProgress(ctx, progress, stepGoPayAppAddBalance)
	addBalanceCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
	if err := workflow.ExecuteActivity(addBalanceCtx, goPayAppAddBalanceActivityName, GoPayAppAddBalanceInput{
		JobId:      input.GetJobId(),
		StateJson:  stateJSON,
		AddBalance: addBalance,
	}).Get(ctx, &balance); err != nil {
		stateJSON = balance.GetStateJson()
		combined["add_balance"] = protoDataMap(balance.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppAddBalance, statusFailedRetryable, false, true, err, combined), nil
	}
	stateJSON = balance.GetStateJson()
	combined["add_balance"] = protoDataMap(balance.GetData())
	result.AddBalanceMethod = balance.GetMethod()
	result.AddBalanceStatus = balance.GetStatus()
	if goPayAddBalanceMethod(addBalance) == "manual_transfer" {
		setWorkflowProgress(ctx, progress, stepGoPayAppAddBalanceConfirm)
		if err := waitForManualAddBalance(ctx, input.GetAddBalanceConfirmTimeoutSeconds()); err != nil {
			combined["add_balance_confirmation"] = map[string]any{
				"confirmed": false,
				"method":    "manual_transfer",
			}
			return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppAddBalanceConfirm, statusFailedRetryable, false, true, err, combined), nil
		}
		combined["add_balance_confirmation"] = map[string]any{
			"confirmed": true,
			"method":    "manual_transfer",
		}
		result.AddBalanceStatus = "confirmed"
	}
	result.AddBalanceComplete = true

	var paymentPrepare GoPayPaymentPrepareOutput
	setWorkflowProgress(ctx, progress, stepGoPayPaymentPrepare)
	paymentPrepare, err = prepareGoPayPayment(ctx, paymentCtx, GoPayActivityInput{
		JobId:             input.GetJobId(),
		AccountId:         account.GetAccountId(),
		UseAccountToken:   false,
		Tokenization:      "true",
		CheckoutUrl:       probe.GetCheckoutUrl(),
		CheckoutSessionId: probe.GetCheckoutSessionId(),
		GopayPhone:        result.GetWaPhone(),
		StateKey:          stateKey,
		StateJson:         stateJSON,
	})
	stateJSON = paymentPrepare.GetStateJson()
	combined["gopay_payment_prepare"] = protoDataMap(paymentPrepare.GetData())
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayPaymentPrepare, statusFailedRetryable, false, true, err, combined), nil
	}

	var payment GoPayActivityOutput
	setWorkflowProgress(ctx, progress, stepGoPayPayment)
	payment, err = runGoPayPayment(ctx, paymentCtx, retryCtx, GoPayActivityInput{
		JobId:             input.GetJobId(),
		AccountId:         account.GetAccountId(),
		UseAccountToken:   true,
		Tokenization:      "true",
		CheckoutUrl:       probe.GetCheckoutUrl(),
		CheckoutSessionId: probe.GetCheckoutSessionId(),
		PreparedFlowId:    paymentPrepare.GetFlowId(),
		GopayPhone:        result.GetWaPhone(),
		OtpChannel:        "wa",
		StateKey:          stateKey,
		StateJson:         stateJSON,
	})
	stateJSON = payment.GetStateJson()
	combined["gopay_payment"] = protoDataMap(payment.GetData())
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayPayment, statusFailedRetryable, false, true, err, combined), nil
	}
	result.ChargeRef = payment.GetChargeRef()
	result.SnapToken = payment.GetSnapToken()
	combined["payment_completed"] = true
	combined["charge_ref"] = result.GetChargeRef()
	combined["snap_token"] = result.GetSnapToken()
	_ = workflow.ExecuteActivity(retryCtx, goPayAppSaveStateActivityName, GoPayAppStateActivityInput{
		JobId:     input.GetJobId(),
		StateKey:  stateKey,
		StateJson: stateJSON,
		Reason:    "payment_completed_before_rebind",
	}).Get(ctx, nil)

	setWorkflowProgress(ctx, progress, stepGoPayAppChangePhone)
	changePhone, err := runGoPayAppChangePhone(ctx, gopayCtx, input.GetJobId(), stateJSON)
	stateJSON = changePhone.GetStateJson()
	combined["change_phone"] = protoDataMap(changePhone.GetData())
	result.ChangePhoneActivationId = changePhone.GetActivationId()
	result.BoundPhone = changePhone.GetPhone()
	result.ChangePhoneComplete = changePhone.GetChangePhoneComplete()
	_ = workflow.ExecuteActivity(retryCtx, goPayAppSaveStateActivityName, GoPayAppStateActivityInput{
		JobId:     input.GetJobId(),
		StateKey:  stateKey,
		StateJson: stateJSON,
		Reason:    "payment_rebind_attempt",
	}).Get(ctx, nil)
	if err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppChangePhone, statusFailedRecoverable, true, false, err, combined), nil
	}
	if err := finishGoPayChangePhoneSMS(ctx, retryCtx, input.GetJobId(), result.GetChangePhoneActivationId(), "payment_rebind_complete"); err != nil {
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSMSFinish, statusFailedRecoverable, true, false, err, combined), nil
	}

	var tier ProbeTierActivityOutput
	setWorkflowProgress(ctx, progress, stepProbeTier)
	if err := workflow.ExecuteActivity(tierCtx, probeTierActivityName, ProbeTierActivityInput{
		JobId:     input.GetJobId(),
		AccountId: account.GetAccountId(),
	}).Get(ctx, &tier); err != nil {
		combined["probe_tier"] = protoDataMap(tier.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepProbeTier, statusFailedRecoverable, true, false, err, combined), nil
	}
	combined["probe_tier"] = protoDataMap(tier.GetData())

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobId:  input.GetJobId(),
		Result: protoData(combined),
	}).Get(ctx, nil)

	result.Success = true
	setWorkflowProgressSucceeded(ctx, progress)
	return result, nil
}

func finishGoPayChangePhoneSMS(ctx workflow.Context, activityCtx workflow.Context, jobID, activationID, reason string) error {
	if strings.TrimSpace(activationID) == "" {
		return fmt.Errorf("change phone activation id is missing")
	}
	return workflow.ExecuteActivity(activityCtx, goPayAppSMSFinishActivityName, GoPayAppSMSActivationInput{
		JobId:        jobID,
		ActivationId: activationID,
		Reason:       reason,
	}).Get(ctx, nil)
}

func isGoPaySignupPhoneRotatableError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "signup phone already registered")
}
