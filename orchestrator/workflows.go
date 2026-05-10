package main

import (
	"strconv"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

const (
	taskQueueDefault = "nb-register-orchestrator"

	createJobActivityName         = "CreateJobActivity"
	ensureAccountActivityName     = "EnsureAccountActivity"
	resolveAccountActivityName    = "ResolveAccountFromJobActivity"
	registerAccountActivityName   = "RegisterAccountAtomicActivity"
	goPayPaymentActivityName      = "GoPayPaymentAtomicActivity"
	probePlusTrialActivityName    = "ProbePlusTrialAtomicActivity"
	loginSessionActivityName      = "LoginSessionAtomicActivity"
	registerMailboxActivityName   = "RegisterMailboxAtomicActivity"
	mailboxOAuthActivityName      = "MailboxOAuthAtomicActivity"
	persistRegisteredActivityName = "PersistRegisteredActivity"
	persistActivatedActivityName  = "PersistActivatedActivity"
	markJobFailedActivityName     = "MarkJobFailedActivity"
	markJobSucceededActivityName  = "MarkJobSucceededActivity"
)

func RegisterAccountWorkflow(ctx workflow.Context, input RegisterAccountWorkflowInput) (RegisterAccountWorkflowResult, error) {
	result := RegisterAccountWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:     input.JobID,
		AccountID: input.Account.AccountID,
		Action:    actionRegister,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var account AccountRef
	if err := workflow.ExecuteActivity(retryCtx, ensureAccountActivityName, EnsureAccountInput{Account: input.Account}).Get(ctx, &account); err != nil {
		return failRegisterWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, nil), nil
	}

	var register RegisterActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, registerAccountActivityName, RegisterActivityInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
	}).Get(ctx, &register); err != nil {
		status, recoverable, retryable := registerFailurePolicy(err)
		return failRegisterWorkflow(ctx, retryCtx, result, input.JobID, stepRegisterAccount, status, recoverable, retryable, err, register.Data), nil
	}

	if err := workflow.ExecuteActivity(retryCtx, persistRegisteredActivityName, PersistRegisteredInput{
		AccountID:         account.AccountID,
		SessionToken:      register.SessionToken,
		AccessToken:       register.AccessToken,
		PlusTrialEligible: register.PlusTrialEligible,
		PlusTrialChecked:  register.PlusTrialChecked,
	}).Get(ctx, nil); err != nil {
		return failRegisterWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, register.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: register.Data,
	}).Get(ctx, nil)

	result.SessionToken = register.SessionToken
	result.AccessToken = register.AccessToken
	result.PlusTrialEligible = register.PlusTrialEligible
	result.CheckoutURL = register.CheckoutURL
	return result, nil
}

func ActivateAccountWorkflow(ctx workflow.Context, input ActivateAccountWorkflowInput) (ActivateAccountWorkflowResult, error) {
	result := ActivateAccountWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))

	var account AccountRef
	if err := workflow.ExecuteActivity(retryCtx, resolveAccountActivityName, ResolveAccountInput{
		AccountID:   input.AccountID,
		SourceJobID: input.SourceJobID,
	}).Get(ctx, &account); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
		Action:    actionActivate,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var payment GoPayActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, goPayPaymentActivityName, GoPayActivityInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
	}).Get(ctx, &payment); err != nil {
		return failActivateWorkflow(ctx, retryCtx, result, input.JobID, stepGoPayPayment, statusFailedRetryable, false, true, err, payment.Data), nil
	}

	if err := workflow.ExecuteActivity(retryCtx, persistActivatedActivityName, PersistActivatedInput{
		AccountID:         account.AccountID,
		ChargeRef:         payment.ChargeRef,
		PlusTrialEligible: payment.PlusTrialEligible,
		PlusTrialChecked:  payment.PlusTrialChecked,
	}).Get(ctx, nil); err != nil {
		return failActivateWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, payment.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: payment.Data,
	}).Get(ctx, nil)

	result.Success = true
	result.ChargeRef = payment.ChargeRef
	result.SnapToken = payment.SnapToken
	return result, nil
}

func ProbePlusTrialWorkflow(ctx workflow.Context, input ProbePlusTrialWorkflowInput) (ProbePlusTrialWorkflowResult, error) {
	result := ProbePlusTrialWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(5*time.Minute))

	var account AccountRef
	if err := workflow.ExecuteActivity(retryCtx, resolveAccountActivityName, ResolveAccountInput{
		AccountID: input.AccountID,
	}).Get(ctx, &account); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
		Action:    actionProbePlusTrial,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var probe ProbePlusTrialActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, probePlusTrialActivityName, ProbePlusTrialActivityInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
	}).Get(ctx, &probe); err != nil {
		return failProbePlusTrialWorkflow(ctx, retryCtx, result, input.JobID, stepProbePlusTrial, statusFailedRetryable, false, true, err, probe.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: probe.Data,
	}).Get(ctx, nil)

	result.Success = probe.Success
	result.Checked = probe.Checked
	result.PlusTrialEligible = probe.PlusTrialEligible
	result.Amount = probe.Amount
	result.Currency = probe.Currency
	result.Source = probe.Source
	result.CheckoutURL = probe.CheckoutURL
	result.ErrorMessage = probe.ErrorMessage
	return result, nil
}

func LoginSessionWorkflow(ctx workflow.Context, input LoginSessionWorkflowInput) (LoginSessionWorkflowResult, error) {
	result := LoginSessionWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))

	var account AccountRef
	if err := workflow.ExecuteActivity(retryCtx, resolveAccountActivityName, ResolveAccountInput{
		AccountID: input.AccountID,
	}).Get(ctx, &account); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
		Action:    actionLoginSession,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var login LoginSessionActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, loginSessionActivityName, LoginSessionActivityInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
	}).Get(ctx, &login); err != nil {
		return failLoginSessionWorkflow(ctx, retryCtx, result, input.JobID, stepLoginSession, statusFailedRetryable, false, true, err, login.Data), nil
	}

	if err := workflow.ExecuteActivity(retryCtx, persistRegisteredActivityName, PersistRegisteredInput{
		AccountID:    account.AccountID,
		SessionToken: login.SessionToken,
		AccessToken:  login.AccessToken,
	}).Get(ctx, nil); err != nil {
		return failLoginSessionWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, login.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: login.Data,
	}).Get(ctx, nil)

	result.SessionToken = login.SessionToken
	result.AccessToken = login.AccessToken
	return result, nil
}

func RegisterAndActivateWorkflow(ctx workflow.Context, input RegisterAndActivateWorkflowInput) (RegisterAndActivateWorkflowResult, error) {
	result := RegisterAndActivateWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(15*time.Minute))

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:     input.JobID,
		AccountID: input.Account.AccountID,
		Action:    actionRegisterAndActivate,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var account AccountRef
	if err := workflow.ExecuteActivity(retryCtx, ensureAccountActivityName, EnsureAccountInput{Account: input.Account}).Get(ctx, &account); err != nil {
		return failRegisterAndActivateWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, nil), nil
	}

	var register RegisterActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, registerAccountActivityName, RegisterActivityInput{
		JobID:     input.JobID,
		AccountID: account.AccountID,
	}).Get(ctx, &register); err != nil {
		status, recoverable, retryable := registerFailurePolicy(err)
		return failRegisterAndActivateWorkflow(ctx, retryCtx, result, input.JobID, stepRegisterAccount, status, recoverable, retryable, err, register.Data), nil
	}

	if err := workflow.ExecuteActivity(retryCtx, persistRegisteredActivityName, PersistRegisteredInput{
		AccountID:         account.AccountID,
		SessionToken:      register.SessionToken,
		AccessToken:       register.AccessToken,
		PlusTrialEligible: register.PlusTrialEligible,
		PlusTrialChecked:  register.PlusTrialChecked,
	}).Get(ctx, nil); err != nil {
		return failRegisterAndActivateWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, register.Data), nil
	}

	var payment GoPayActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, goPayPaymentActivityName, GoPayActivityInput{
		JobID:        input.JobID,
		AccountID:    account.AccountID,
		SessionToken: register.SessionToken,
		AccessToken:  register.AccessToken,
	}).Get(ctx, &payment); err != nil {
		combined := map[string]any{"register_account": register.Data, "gopay_payment": payment.Data}
		return failRegisterAndActivateWorkflow(ctx, retryCtx, result, input.JobID, stepGoPayPayment, statusFailedRetryable, false, true, err, combined), nil
	}

	combined := map[string]any{"register_account": register.Data, "gopay_payment": payment.Data}
	if err := workflow.ExecuteActivity(retryCtx, persistActivatedActivityName, PersistActivatedInput{
		AccountID:         account.AccountID,
		SessionToken:      register.SessionToken,
		AccessToken:       register.AccessToken,
		ChargeRef:         payment.ChargeRef,
		PlusTrialEligible: payment.PlusTrialEligible,
		PlusTrialChecked:  payment.PlusTrialChecked,
	}).Get(ctx, nil); err != nil {
		return failRegisterAndActivateWorkflow(ctx, retryCtx, result, input.JobID, "", statusFailedRecoverable, true, false, err, combined), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: combined,
	}).Get(ctx, nil)

	result.SessionToken = register.SessionToken
	result.AccessToken = register.AccessToken
	result.PlusTrialEligible = payment.PlusTrialEligible || register.PlusTrialEligible
	result.CheckoutURL = register.CheckoutURL
	result.ActivationSuccess = true
	result.ChargeRef = payment.ChargeRef
	result.SnapToken = payment.SnapToken
	return result, nil
}

func RegisterMailboxWorkflow(ctx workflow.Context, input RegisterMailboxWorkflowInput) (RegisterMailboxWorkflowResult, error) {
	result := RegisterMailboxWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(30*time.Minute))

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:  input.JobID,
		Action: actionRegisterMailbox,
		Params: map[string]string{
			"import_only": boolString(input.ImportOnly),
		},
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var registration MailboxRegistrationActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, registerMailboxActivityName, MailboxRegistrationActivityInput{
		JobID:      input.JobID,
		Enabled:    !input.ImportOnly,
		ImportOnly: input.ImportOnly,
	}).Get(ctx, &registration); err != nil {
		return failRegisterMailboxWorkflow(ctx, retryCtx, result, input.JobID, stepRegisterMailbox, statusFailedRetryable, false, true, err, registration.Data), nil
	}
	if !registration.Success {
		err := temporal.NewApplicationError(registration.ErrorMessage, "MailboxRegistrationFailed")
		return failRegisterMailboxWorkflow(ctx, retryCtx, result, input.JobID, stepRegisterMailbox, statusFailedRetryable, false, true, err, registration.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: registration.Data,
	}).Get(ctx, nil)

	result.Success = registration.Success
	result.ExitCode = registration.ExitCode
	result.Mailboxes = registration.Mailboxes
	return result, nil
}

func MailboxOAuthWorkflow(ctx workflow.Context, input MailboxOAuthWorkflowInput) (MailboxOAuthWorkflowResult, error) {
	result := MailboxOAuthWorkflowResult{JobID: input.JobID}
	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	atomicCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(30*time.Minute))

	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobID:  input.JobID,
		Action: actionMailboxOAuth,
		Params: map[string]string{
			"email_address": input.EmailAddress,
			"only_missing":  boolString(input.OnlyMissing),
			"limit":         int32String(input.Limit),
		},
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var oauth MailboxOAuthActivityOutput
	if err := workflow.ExecuteActivity(atomicCtx, mailboxOAuthActivityName, MailboxOAuthActivityInput{
		JobID:        input.JobID,
		EmailAddress: input.EmailAddress,
		OnlyMissing:  input.OnlyMissing,
		Limit:        input.Limit,
	}).Get(ctx, &oauth); err != nil {
		return failMailboxOAuthWorkflow(ctx, retryCtx, result, input.JobID, stepMailboxOAuth, statusFailedRetryable, false, true, err, oauth.Data), nil
	}
	if !oauth.Success {
		msg := oauth.ErrorMessage
		if msg == "" {
			msg = "mailbox OAuth failed"
		}
		err := temporal.NewApplicationError(msg, "MailboxOAuthFailed")
		return failMailboxOAuthWorkflow(ctx, retryCtx, result, input.JobID, stepMailboxOAuth, statusFailedRetryable, false, true, err, oauth.Data), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobID:  input.JobID,
		Result: oauth.Data,
	}).Get(ctx, nil)

	result.Success = oauth.Success
	result.Processed = oauth.Processed
	result.Succeeded = oauth.Succeeded
	result.Failed = oauth.Failed
	return result, nil
}

func failRegisterWorkflow(ctx workflow.Context, activityCtx workflow.Context, result RegisterAccountWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) RegisterAccountWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failActivateWorkflow(ctx workflow.Context, activityCtx workflow.Context, result ActivateAccountWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) ActivateAccountWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failProbePlusTrialWorkflow(ctx workflow.Context, activityCtx workflow.Context, result ProbePlusTrialWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) ProbePlusTrialWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failLoginSessionWorkflow(ctx workflow.Context, activityCtx workflow.Context, result LoginSessionWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) LoginSessionWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failRegisterAndActivateWorkflow(ctx workflow.Context, activityCtx workflow.Context, result RegisterAndActivateWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) RegisterAndActivateWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failRegisterMailboxWorkflow(ctx workflow.Context, activityCtx workflow.Context, result RegisterMailboxWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) RegisterMailboxWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func failMailboxOAuthWorkflow(ctx workflow.Context, activityCtx workflow.Context, result MailboxOAuthWorkflowResult, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) MailboxOAuthWorkflowResult {
	result.ErrorMessage = err.Error()
	markWorkflowFailure(ctx, activityCtx, jobID, stepName, status, recoverable, retryable, err, data)
	return result
}

func markWorkflowFailure(ctx workflow.Context, activityCtx workflow.Context, jobID, stepName, status string, recoverable, retryable bool, err error, data map[string]any) {
	_ = workflow.ExecuteActivity(activityCtx, markJobFailedActivityName, JobFailureInput{
		JobID:        jobID,
		StepName:     stepName,
		Status:       status,
		Recoverable:  recoverable,
		Retryable:    retryable,
		ErrorMessage: err.Error(),
		Result:       data,
	}).Get(ctx, nil)
}

func atomicActivityOptions(timeout time.Duration) workflow.ActivityOptions {
	return workflow.ActivityOptions{
		StartToCloseTimeout: timeout,
		RetryPolicy: &temporal.RetryPolicy{
			MaximumAttempts: 1,
		},
	}
}

func retryableActivityOptions(timeout time.Duration, attempts int32) workflow.ActivityOptions {
	return workflow.ActivityOptions{
		StartToCloseTimeout: timeout,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2,
			MaximumInterval:    10 * time.Second,
			MaximumAttempts:    attempts,
		},
	}
}

func boolString(value bool) string {
	if value {
		return "true"
	}
	return "false"
}

func int32String(value int32) string {
	if value == 0 {
		return ""
	}
	return strconv.FormatInt(int64(value), 10)
}
