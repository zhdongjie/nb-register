package main

import (
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
)

func registerTemporalWorker(w worker.Worker, s *orchestratorServer) {
	w.RegisterWorkflow(RegisterAccountWorkflow)
	w.RegisterWorkflow(ActivateAccountWorkflow)
	w.RegisterWorkflow(AutoPayWorkflow)
	w.RegisterWorkflow(GoPayCycleWorkflow)
	w.RegisterWorkflow(ProbeAccountWorkflow)
	w.RegisterWorkflow(LoginSessionWorkflow)
	w.RegisterWorkflow(RegisterAndActivateWorkflow)
	w.RegisterWorkflow(RegisterMailboxWorkflow)
	w.RegisterWorkflow(MailboxOAuthWorkflow)

	w.RegisterActivityWithOptions(s.CreateJobActivity, activity.RegisterOptions{Name: createJobActivityName})
	w.RegisterActivityWithOptions(s.EnsureAccountActivity, activity.RegisterOptions{Name: ensureAccountActivityName})
	w.RegisterActivityWithOptions(s.ResolveAccountFromJobActivity, activity.RegisterOptions{Name: resolveAccountActivityName})
	w.RegisterActivityWithOptions(s.RegisterAccountAtomicActivity, activity.RegisterOptions{Name: registerAccountActivityName})
	w.RegisterActivityWithOptions(s.EnsureLogonActivity, activity.RegisterOptions{Name: ensureLogonActivityName})
	w.RegisterActivityWithOptions(s.GoPayPaymentAtomicActivity, activity.RegisterOptions{Name: goPayPaymentActivityName})
	w.RegisterActivityWithOptions(s.CycleAndPayActivity, activity.RegisterOptions{Name: cycleAndPayActivityName})
	w.RegisterActivityWithOptions(s.GoPayCycleLoginActivity, activity.RegisterOptions{Name: goPayCycleLoginActivityName})
	w.RegisterActivityWithOptions(s.GoPayCycleChangePhoneActivity, activity.RegisterOptions{Name: goPayCycleChangePhoneActivityName})
	w.RegisterActivityWithOptions(s.ProbePlusTrialAtomicActivity, activity.RegisterOptions{Name: probePlusTrialActivityName})
	w.RegisterActivityWithOptions(s.ProbeTierAtomicActivity, activity.RegisterOptions{Name: probeTierActivityName})
	w.RegisterActivityWithOptions(s.LoginSessionAtomicActivity, activity.RegisterOptions{Name: loginSessionActivityName})
	w.RegisterActivityWithOptions(s.RegisterMailboxAtomicActivity, activity.RegisterOptions{Name: registerMailboxActivityName})
	w.RegisterActivityWithOptions(s.MailboxOAuthAtomicActivity, activity.RegisterOptions{Name: mailboxOAuthActivityName})
	w.RegisterActivityWithOptions(s.PersistRegisteredActivity, activity.RegisterOptions{Name: persistRegisteredActivityName})
	w.RegisterActivityWithOptions(s.PersistActivatedActivity, activity.RegisterOptions{Name: persistActivatedActivityName})
	w.RegisterActivityWithOptions(s.MarkJobFailedActivity, activity.RegisterOptions{Name: markJobFailedActivityName})
	w.RegisterActivityWithOptions(s.MarkJobSucceededActivity, activity.RegisterOptions{Name: markJobSucceededActivityName})
}
