package api

import (
	"context"
	"github.com/google/uuid"
	"orchestrator/internal/contracts"
	"orchestrator/internal/workflows"
	"orchestrator/pb"
	"strings"

	proto "google.golang.org/protobuf/proto"
)

func (s *Server) RegisterAccount(ctx context.Context, req *pb.RegisterAccountRequest) (*pb.RegisterAccountResponse, error) {
	jobID := uuid.NewString()
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		accountID = uuid.NewString()
	}
	var result workflows.RegisterAccountWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionRegister, jobID)), workflows.RegisterAccountWorkflow, workflows.RegisterAccountWorkflowInput{
		JobId: jobID,
		Account: &workflows.AccountSpec{
			AccountId: accountID,
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
		JobId:             result.GetJobId(),
		SessionToken:      result.GetSessionToken(),
		AccessToken:       result.GetAccessToken(),
		PlusTrialEligible: result.GetPlusTrialEligible(),
		ErrorMessage:      result.GetErrorMessage(),
		CheckoutUrl:       result.GetCheckoutUrl(),
	}, nil
}

func (s *Server) ActivateAccount(ctx context.Context, req *pb.ActivateAccountRequest) (*pb.ActivateAccountResponse, error) {
	jobID := uuid.NewString()
	var result workflows.ActivateAccountWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionActivate, jobID)), workflows.ActivateAccountWorkflow, workflows.ActivateAccountWorkflowInput{
		JobId:       jobID,
		AccountId:   strings.TrimSpace(req.GetAccountId()),
		SourceJobId: req.GetJobId(),
		Action:      actionActivate,
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.ActivateAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.ActivateAccountResponse{
		JobId:        result.GetJobId(),
		Success:      result.GetSuccess(),
		ErrorMessage: result.GetErrorMessage(),
		ChargeRef:    result.GetChargeRef(),
		SnapToken:    result.GetSnapToken(),
	}, nil
}

func (s *Server) AutopayAccount(ctx context.Context, req *pb.ActivateAccountRequest) (*pb.ActivateAccountResponse, error) {
	jobID := uuid.NewString()
	var result workflows.AutoPayWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionAutopay, jobID)), workflows.AutoPayWorkflow, workflows.AutoPayWorkflowInput{
		JobId:       jobID,
		AccountId:   strings.TrimSpace(req.GetAccountId()),
		SourceJobId: req.GetJobId(),
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.ActivateAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}

	return &pb.ActivateAccountResponse{
		JobId:        result.GetJobId(),
		Success:      result.GetSuccess(),
		ErrorMessage: result.GetErrorMessage(),
		ChargeRef:    result.GetChargeRef(),
		SnapToken:    result.GetSnapToken(),
	}, nil
}

func (s *Server) LoginAccount(ctx context.Context, req *pb.LoginAccountRequest) (*pb.LoginAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		return &pb.LoginAccountResponse{ErrorMessage: "account_id is required"}, nil
	}
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionLoginSession, jobID)), workflows.LoginSessionWorkflow, workflows.LoginSessionWorkflowInput{
		JobId:     jobID,
		AccountId: accountID,
	})
	if err != nil {
		return &pb.LoginAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.LoginAccountResponse{JobId: jobID, Started: true}, nil
}

func (s *Server) ProbeAccount(ctx context.Context, req *pb.ProbeAccountRequest) (*pb.ProbeAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		return &pb.ProbeAccountResponse{ErrorMessage: "account_id is required"}, nil
	}
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionProbeAccount, jobID)), workflows.ProbeAccountWorkflow, workflows.ProbeAccountWorkflowInput{
		JobId:     jobID,
		AccountId: accountID,
	})
	if err != nil {
		return &pb.ProbeAccountResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.ProbeAccountResponse{JobId: jobID, Started: true}, nil
}

func (s *Server) RunGoPayApp(ctx context.Context, req *pb.GoPayAppRequest) (*pb.GoPayAppResponse, error) {
	jobID := uuid.NewString()
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionGoPayApp, jobID)), workflows.GoPayAppWorkflow, workflows.GoPayAppWorkflowInput{
		JobId: jobID,
	})
	if err != nil {
		return &pb.GoPayAppResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.GoPayAppResponse{JobId: jobID, Started: true}, nil
}

func (s *Server) RunGoPayPayment(ctx context.Context, req *pb.GoPayPaymentRequest) (*pb.GoPayPaymentResponse, error) {
	jobID := uuid.NewString()
	otpChannel := strings.TrimSpace(req.GetOtpChannel())
	if otpChannel == "" {
		otpChannel = "sms"
	}
	addBalance := cloneGoPayAddBalance(req.GetAddBalance())
	if addBalance == nil {
		addBalance = cloneGoPayAddBalance(s.defaultGoPayAddBalance)
	}
	addBalanceConfirmTimeoutSeconds := req.GetAddBalanceConfirmTimeoutSeconds()
	if addBalanceConfirmTimeoutSeconds <= 0 {
		addBalanceConfirmTimeoutSeconds = s.goPayAddBalanceConfirmTimeoutSeconds
	}
	if addBalanceConfirmTimeoutSeconds <= 0 {
		addBalanceConfirmTimeoutSeconds = 1800
	}
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionGoPayPayment, jobID)), workflows.GoPayPaymentWorkflow, workflows.GoPayPaymentWorkflowInput{
		JobId:                           jobID,
		AccountId:                       strings.TrimSpace(req.GetAccountId()),
		SourceJobId:                     strings.TrimSpace(req.GetSourceJobId()),
		OtpChannel:                      otpChannel,
		SmsActivationId:                 strings.TrimSpace(req.GetSmsActivationId()),
		AddBalance:                      addBalance,
		AddBalanceConfirmTimeoutSeconds: addBalanceConfirmTimeoutSeconds,
		StateKey:                        strings.TrimSpace(req.GetStateKey()),
		WaPhone:                         strings.TrimSpace(req.GetWaPhone()),
	})
	if err != nil {
		return &pb.GoPayPaymentResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.GoPayPaymentResponse{JobId: jobID, Started: true}, nil
}

func (s *Server) RetryGoPayPaymentRebind(ctx context.Context, req *pb.GoPayPaymentRebindRequest) (*pb.GoPayPaymentResponse, error) {
	jobID := uuid.NewString()
	sourceJobID := strings.TrimSpace(req.GetSourceJobId())
	if sourceJobID == "" {
		return &pb.GoPayPaymentResponse{JobId: jobID, ErrorMessage: "source_job_id is required"}, nil
	}
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionGoPayPaymentRebind, jobID)), workflows.GoPayPaymentRebindWorkflow, workflows.GoPayPaymentRebindWorkflowInput{
		JobId:       jobID,
		SourceJobId: sourceJobID,
		AccountId:   strings.TrimSpace(req.GetAccountId()),
		StateKey:    strings.TrimSpace(req.GetStateKey()),
	})
	if err != nil {
		return &pb.GoPayPaymentResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.GoPayPaymentResponse{JobId: jobID, Started: true}, nil
}

func (s *Server) ConfirmManualAddBalance(ctx context.Context, req *pb.ConfirmManualAddBalanceRequest) (*pb.ConfirmManualAddBalanceResponse, error) {
	jobID := strings.TrimSpace(req.GetJobId())
	if jobID == "" {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, ErrorMessage: "job_id is required"}, nil
	}
	job, err := s.getJob(ctx, jobID)
	if err != nil {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	if job.Status != statusRunning {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: "job is not running: " + job.Status}, nil
	}
	if job.Action != actionGoPayPayment {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: "job does not accept add_balance confirmation: " + job.Action}, nil
	}
	if job.LastStep != stepGoPayAppAddBalance {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: "job is not waiting for add_balance confirmation: " + job.LastStep}, nil
	}
	workflowID, ok := contracts.WorkflowID(job.Action, job.ID)
	if !ok || workflowID == "" {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: "workflow id not found"}, nil
	}
	if err := s.temporal.SignalWorkflow(ctx, workflowID, "", manualAddBalanceSignalName, ManualAddBalanceSignal{Kind: "manual_transfer_confirmed"}); err != nil {
		return &pb.ConfirmManualAddBalanceResponse{Success: false, JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.ConfirmManualAddBalanceResponse{Success: true, JobId: jobID}, nil
}

func cloneGoPayAddBalance(value *pb.GoPayAddBalance) *pb.GoPayAddBalance {
	if value == nil {
		return nil
	}
	cloned, ok := proto.Clone(value).(*pb.GoPayAddBalance)
	if !ok {
		return nil
	}
	return cloned
}

func (s *Server) RegisterAndActivateAccount(ctx context.Context, req *pb.RegisterAndActivateAccountRequest) (*pb.RegisterAndActivateAccountResponse, error) {
	jobID := uuid.NewString()
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		accountID = uuid.NewString()
	}
	var result workflows.RegisterAndActivateWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionRegisterAndActivate, jobID)), workflows.RegisterAndActivateWorkflow, workflows.RegisterAndActivateWorkflowInput{
		JobId: jobID,
		Account: &workflows.AccountSpec{
			AccountId: accountID,
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
		JobId:             result.GetJobId(),
		SessionToken:      result.GetSessionToken(),
		AccessToken:       result.GetAccessToken(),
		PlusTrialEligible: result.GetPlusTrialEligible(),
		CheckoutUrl:       result.GetCheckoutUrl(),
		ActivationSuccess: result.GetActivationSuccess(),
		ErrorMessage:      result.GetErrorMessage(),
		ChargeRef:         result.GetChargeRef(),
		SnapToken:         result.GetSnapToken(),
	}, nil
}

func (s *Server) RegisterMailbox(ctx context.Context, req *pb.RegisterMailboxRequest) (*pb.RegisterMailboxResponse, error) {
	jobID := uuid.NewString()
	var result workflows.RegisterMailboxWorkflowResult
	run, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionRegisterMailbox, jobID)), workflows.RegisterMailboxWorkflow, workflows.RegisterMailboxWorkflowInput{
		JobId:      jobID,
		ImportOnly: req.GetImportOnly(),
		AutoOauth:  !req.GetImportOnly() && s.outlookRegisterEnableOAuth2,
	})
	if err != nil {
		return nil, err
	}
	if err := run.Get(ctx, &result); err != nil {
		return &pb.RegisterMailboxResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	mailboxes := make([]*pb.RegisteredMailbox, 0, len(result.GetMailboxes()))
	for _, mailbox := range result.GetMailboxes() {
		mailboxes = append(mailboxes, &pb.RegisteredMailbox{
			EmailAddress: mailbox.GetEmailAddress(),
			Status:       mailbox.GetStatus(),
		})
	}
	return &pb.RegisterMailboxResponse{
		JobId:        result.GetJobId(),
		Success:      result.GetSuccess(),
		ExitCode:     result.GetExitCode(),
		ErrorMessage: result.GetErrorMessage(),
		Mailboxes:    mailboxes,
	}, nil
}

func (s *Server) RunMailboxOAuth(ctx context.Context, req *pb.StartMailboxOAuthRequest) (*pb.StartMailboxOAuthResponse, error) {
	jobID := uuid.NewString()
	limit := req.GetLimit()
	if limit <= 0 {
		limit = 100
	}
	onlyMissing := req.GetOnlyMissing()
	if strings.TrimSpace(req.GetEmailAddress()) == "" {
		onlyMissing = true
	}
	_, err := s.temporal.ExecuteWorkflow(ctx, s.workflowOptions(workflowIDForAction(actionMailboxOAuth, jobID)), workflows.MailboxOAuthWorkflow, workflows.MailboxOAuthWorkflowInput{
		JobId:        jobID,
		EmailAddress: strings.TrimSpace(req.GetEmailAddress()),
		OnlyMissing:  onlyMissing,
		Limit:        limit,
	})
	if err != nil {
		return &pb.StartMailboxOAuthResponse{JobId: jobID, ErrorMessage: err.Error()}, nil
	}
	return &pb.StartMailboxOAuthResponse{JobId: jobID, Started: true}, nil
}

func workflowIDForAction(action string, jobID string) string {
	workflowID, ok := contracts.WorkflowID(action, jobID)
	if !ok {
		return jobID
	}
	return workflowID
}
