package workflows

import (
	"fmt"
	"time"

	"go.temporal.io/sdk/workflow"
)

func GoPayPaymentWorkflow(ctx workflow.Context, input GoPayPaymentWorkflowInput) (GoPayPaymentWorkflowResult, error) {
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
	combined := map[string]any{"otp_channel": otpChannel}

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
			"otp_channel": otpChannel,
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

	var phone GoPayAppAcquireSignupPhoneOutput
	setWorkflowProgress(ctx, progress, stepGoPayAppSignupPhone)
	if err := workflow.ExecuteActivity(gopayCtx, goPayAppAcquireSignupPhoneActivityName, GoPayAppAcquireSignupPhoneInput{
		JobId: input.GetJobId(),
	}).Get(ctx, &phone); err != nil {
		combined["signup_phone"] = protoDataMap(phone.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignupPhone, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["signup_phone"] = protoDataMap(phone.GetData())
	result.ActivationId = phone.GetActivationId()
	result.Phone = phone.GetPhone()

	otpOpts := goPayAppOTPOptions{
		Phone:           phone.GetPhone(),
		OTPChannel:      otpChannel,
		SMSActivationID: phone.GetActivationId(),
		ResetState:      true,
	}
	finishSMS := func(reason string) {
		if phone.GetActivationId() == "" {
			return
		}
		_ = workflow.ExecuteActivity(retryCtx, goPayAppSMSFinishActivityName, GoPayAppSMSActivationInput{
			JobId:        input.GetJobId(),
			ActivationId: phone.GetActivationId(),
			Reason:       reason,
		}).Get(ctx, nil)
	}

	setWorkflowProgress(ctx, progress, stepGoPayAppSignup)
	signup, err := runGoPayAppSignup(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
	if err != nil {
		finishSMS("signup failed")
		combined["signup"] = protoDataMap(signup.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSignup, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["signup"] = protoDataMap(signup.GetData())
	result.SignupComplete = signup.GetSignupComplete()

	setWorkflowProgress(ctx, progress, stepGoPayAppCreatePin)
	createPin, err := runGoPayAppCreatePin(ctx, gopayCtx, retryCtx, input.GetJobId(), otpOpts)
	if err != nil {
		finishSMS("create pin failed")
		combined["create_pin"] = protoDataMap(createPin.GetData())
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, err, combined), nil
	}
	combined["create_pin"] = protoDataMap(createPin.GetData())
	result.SignupPinComplete = createPin.GetSignupPinComplete()
	result.AccountTokenReady = createPin.GetAccountTokenReady()
	if !createPin.GetAccountTokenReady() {
		finishSMS("account token not ready after create pin")
		return failGoPayPaymentWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppCreatePin, statusFailedRetryable, false, true, fmt.Errorf("gopay account token is not ready after create pin"), combined), nil
	}

	finishSMS("gopay signup complete")

	var payment GoPayActivityOutput
	setWorkflowProgress(ctx, progress, stepGoPayPayment)
	payment, err = runGoPayPayment(ctx, paymentCtx, retryCtx, GoPayActivityInput{
		JobId:             input.GetJobId(),
		AccountId:         account.GetAccountId(),
		UseAccountToken:   true,
		Tokenization:      "true",
		CheckoutUrl:       probe.GetCheckoutUrl(),
		CheckoutSessionId: probe.GetCheckoutSessionId(),
	})
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
