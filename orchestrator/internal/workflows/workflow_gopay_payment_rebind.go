package workflows

import (
	"fmt"
	"strings"
	"time"

	"go.temporal.io/sdk/workflow"
)

func GoPayPaymentRebindWorkflow(ctx workflow.Context, input GoPayPaymentRebindWorkflowInput) (GoPayPaymentRebindWorkflowResult, error) {
	progress := newWorkflowProgress(ctx, "GoPayPaymentRebindWorkflow", input.GetJobId())
	result := GoPayPaymentRebindWorkflowResult{
		JobId:       input.GetJobId(),
		SourceJobId: input.GetSourceJobId(),
	}
	defer func() {
		finishWorkflowProgressOnError(ctx, progress, result.GetErrorMessage())
	}()

	retryCtx := workflow.WithActivityOptions(ctx, retryableActivityOptions(30*time.Second, 5))
	gopayCtx := workflow.WithActivityOptions(ctx, atomicActivityOptions(30*time.Minute))

	stateKey := strings.TrimSpace(input.GetStateKey())
	if stateKey == "" {
		stateKey = goPayLocalSource
	}
	combined := map[string]any{
		"source_job_id": input.GetSourceJobId(),
		"state_key":     stateKey,
	}

	var source GoPayPaymentRebindSourceOutput
	setWorkflowProgress(ctx, progress, "resolve_rebind_source")
	if err := workflow.ExecuteActivity(retryCtx, goPayPaymentRebindSourceActivityName, GoPayPaymentRebindSourceInput{
		JobId:       input.GetJobId(),
		SourceJobId: input.GetSourceJobId(),
		AccountId:   input.GetAccountId(),
		StateKey:    input.GetStateKey(),
	}).Get(ctx, &source); err != nil {
		combined["rebind_source"] = protoDataMap(source.GetData())
		return failGoPayPaymentRebindWorkflow(ctx, retryCtx, result, input.GetJobId(), "resolve_rebind_source", statusFailedRetryable, false, true, err, combined), nil
	}
	combined["rebind_source"] = protoDataMap(source.GetData())
	stateKey = source.GetStateKey()
	result.StateKey = stateKey
	result.AccountId = source.GetAccountId()
	result.WaPhone = source.GetWaPhone()

	setWorkflowProgress(ctx, progress, "create_job")
	params := map[string]string{
		"source_job_id": source.GetSourceJobId(),
		"state_key":     stateKey,
	}
	if strings.TrimSpace(source.GetWaPhone()) != "" {
		params["wa_phone"] = source.GetWaPhone()
	}
	if err := workflow.ExecuteActivity(retryCtx, createJobActivityName, CreateJobInput{
		JobId:     input.GetJobId(),
		AccountId: source.GetAccountId(),
		Action:    actionGoPayPaymentRebind,
		Params:    params,
	}).Get(ctx, nil); err != nil {
		result.ErrorMessage = err.Error()
		return result, nil
	}

	var stored GoPayAppStateActivityOutput
	setWorkflowProgress(ctx, progress, "load_gopay_state")
	if err := workflow.ExecuteActivity(retryCtx, goPayAppLoadStateActivityName, GoPayAppStateActivityInput{
		JobId:    input.GetJobId(),
		StateKey: stateKey,
		Reason:   "payment_rebind_retry",
	}).Get(ctx, &stored); err != nil {
		combined["load_state"] = protoDataMap(stored.GetData())
		return failGoPayPaymentRebindWorkflow(ctx, retryCtx, result, input.GetJobId(), "load_gopay_state", statusFailedRetryable, false, true, err, combined), nil
	}
	stateJSON := stored.GetStateJson()
	if strings.TrimSpace(stateJSON) == "" {
		stateJSON = "{}"
	}
	combined["load_state"] = protoDataMap(stored.GetData())

	setWorkflowProgress(ctx, progress, stepGoPayAppChangePhone)
	changePhone, err := runGoPayAppChangePhone(ctx, gopayCtx, input.GetJobId(), stateJSON)
	stateJSON = changePhone.GetStateJson()
	combined["change_phone"] = protoDataMap(changePhone.GetData())
	result.ActivationId = changePhone.GetActivationId()
	result.BoundPhone = changePhone.GetPhone()
	result.ChangePhoneComplete = changePhone.GetChangePhoneComplete()
	_ = workflow.ExecuteActivity(retryCtx, goPayAppSaveStateActivityName, GoPayAppStateActivityInput{
		JobId:     input.GetJobId(),
		StateKey:  stateKey,
		StateJson: stateJSON,
		Reason:    "payment_rebind_retry_attempt",
	}).Get(ctx, nil)
	if err != nil {
		return failGoPayPaymentRebindWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppChangePhone, statusFailedRetryable, false, true, err, combined), nil
	}
	if !result.GetChangePhoneComplete() {
		err := fmt.Errorf("gopay payment rebind did not complete")
		return failGoPayPaymentRebindWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppChangePhone, statusFailedRetryable, false, true, err, combined), nil
	}
	if err := finishGoPayChangePhoneSMS(ctx, retryCtx, input.GetJobId(), result.GetActivationId(), "payment_rebind_retry_complete"); err != nil {
		return failGoPayPaymentRebindWorkflow(ctx, retryCtx, result, input.GetJobId(), stepGoPayAppSMSFinish, statusFailedRetryable, false, true, err, combined), nil
	}

	_ = workflow.ExecuteActivity(retryCtx, markJobSucceededActivityName, JobSuccessInput{
		JobId:  input.GetJobId(),
		Result: protoData(combined),
	}).Get(ctx, nil)

	result.Success = true
	setWorkflowProgressSucceeded(ctx, progress)
	return result, nil
}
