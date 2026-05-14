package main

type AccountSpec struct {
	AccountID string
	Email     string
	Password  string
}

type CreateJobInput struct {
	JobID     string
	AccountID string
	Action    string
	Params    map[string]string
}

type EnsureAccountInput struct {
	Account AccountSpec
}

type AccountRef struct {
	AccountID         string
	PlusTrialKnown    bool
	PlusTrialEligible bool
}

type ResolveAccountInput struct {
	AccountID   string
	SourceJobID string
}

type RegisterActivityInput struct {
	JobID     string
	AccountID string
}

type RegisterActivityOutput struct {
	SessionToken      string
	AccessToken       string
	DeviceID          string
	PlusTrialEligible bool
	PlusTrialChecked  bool
	CheckoutURL       string
	Data              map[string]any
}

type GoPayActivityInput struct {
	JobID         string
	AccountID     string
	SessionToken  string
	AccessToken   string
	UseCycleToken bool
	Tokenization  string
}

type GoPayActivityOutput struct {
	ChargeRef         string
	SnapToken         string
	PlusTrialEligible bool
	PlusTrialChecked  bool
	PlusActive        bool
	Data              map[string]any
}

type GoPayCycleStepInput struct {
	JobID        string
	ActivationID string
}

type GoPayCycleStepOutput struct {
	ActivationID        string
	Ready               bool
	Stage               string
	Phone               string
	CycleTokenReady     bool
	ChangePhoneComplete bool
	DeactivateComplete  bool
	SignupComplete      bool
	SignupPinComplete   bool
	Data                map[string]any
}

type ProbePlusTrialActivityInput struct {
	JobID     string
	AccountID string
}

type ProbeTierActivityInput struct {
	JobID     string
	AccountID string
}

type ProbePlusTrialActivityOutput struct {
	Success           bool
	Checked           bool
	PlusTrialEligible bool
	PlusActive        bool
	Amount            int64
	Currency          string
	Source            string
	PlanType          string
	CheckoutURL       string
	ErrorMessage      string
	Data              map[string]any
}

type ProbeTierActivityOutput struct {
	Success      bool
	Checked      bool
	Tier         string
	PlusActive   bool
	Source       string
	ErrorMessage string
	Data         map[string]any
}

type LoginSessionActivityInput struct {
	JobID     string
	AccountID string
}

type LoginSessionActivityOutput struct {
	SessionToken string
	AccessToken  string
	DeviceID     string
	Data         map[string]any
}

type PersistRegisteredInput struct {
	AccountID         string
	SessionToken      string
	AccessToken       string
	PlusTrialEligible bool
	PlusTrialChecked  bool
}

type PersistActivatedInput struct {
	AccountID         string
	SessionToken      string
	AccessToken       string
	ChargeRef         string
	PlusTrialEligible bool
	PlusTrialChecked  bool
	PlusActive        bool
}

type JobFailureInput struct {
	JobID        string
	StepName     string
	Status       string
	Recoverable  bool
	Retryable    bool
	ErrorMessage string
	Result       map[string]any
}

type JobSuccessInput struct {
	JobID  string
	Result map[string]any
}

type MailboxRegistrationActivityInput struct {
	JobID      string
	Enabled    bool
	ImportOnly bool
}

type MailboxRegistrationActivityOutput struct {
	Success      bool
	ExitCode     int32
	ErrorMessage string
	Mailboxes    []RegisteredMailboxResult
	Data         map[string]any
}

type RegisteredMailboxResult struct {
	EmailAddress string
	Status       string
}

type MailboxOAuthActivityInput struct {
	JobID        string
	EmailAddress string
	OnlyMissing  bool
	Limit        int32
}

type MailboxOAuthActivityOutput struct {
	Success      bool
	Processed    int32
	Succeeded    int32
	Failed       int32
	ErrorMessage string
	Data         map[string]any
}

type RegisterAccountWorkflowInput struct {
	JobID   string
	Account AccountSpec
}

type RegisterAccountWorkflowResult struct {
	JobID             string
	SessionToken      string
	AccessToken       string
	PlusTrialEligible bool
	CheckoutURL       string
	ErrorMessage      string
}

type ActivateAccountWorkflowInput struct {
	JobID       string
	AccountID   string
	SourceJobID string
	Action      string
}

type ActivateAccountWorkflowResult struct {
	JobID        string
	Success      bool
	ErrorMessage string
	ChargeRef    string
	SnapToken    string
}

type AutoPayWorkflowInput struct {
	JobID       string
	AccountID   string
	SourceJobID string
}

type AutoPayWorkflowResult struct {
	JobID        string
	Success      bool
	ErrorMessage string
	ChargeRef    string
	SnapToken    string
}

type GoPayCycleWorkflowInput struct {
	JobID string
}

type GoPayCycleWorkflowResult struct {
	JobID               string
	Success             bool
	ErrorMessage        string
	ActivationID        string
	CycleTokenReady     bool
	ChangePhoneComplete bool
	DeactivateComplete  bool
	SignupComplete      bool
	SignupPinComplete   bool
}

type ProbeAccountWorkflowInput struct {
	JobID     string
	AccountID string
}

type LoginSessionWorkflowInput struct {
	JobID     string
	AccountID string
}

type LoginSessionWorkflowResult struct {
	JobID        string
	SessionToken string
	AccessToken  string
	ErrorMessage string
}

type ProbeAccountWorkflowResult struct {
	JobID             string
	Success           bool
	PlusTrialChecked  bool
	PlusTrialEligible bool
	TierChecked       bool
	Tier              string
	PlusActive        bool
	Amount            int64
	Currency          string
	Source            string
	PlanType          string
	CheckoutURL       string
	ErrorMessage      string
}

type RegisterAndActivateWorkflowInput struct {
	JobID   string
	Account AccountSpec
}

type RegisterAndActivateWorkflowResult struct {
	JobID             string
	SessionToken      string
	AccessToken       string
	PlusTrialEligible bool
	CheckoutURL       string
	ActivationSuccess bool
	ErrorMessage      string
	ChargeRef         string
	SnapToken         string
}

type RegisterMailboxWorkflowInput struct {
	JobID      string
	ImportOnly bool
	AutoOAuth  bool
}

type RegisterMailboxWorkflowResult struct {
	JobID        string
	Success      bool
	ExitCode     int32
	ErrorMessage string
	Mailboxes    []RegisteredMailboxResult
}

type MailboxOAuthWorkflowInput struct {
	JobID        string
	EmailAddress string
	OnlyMissing  bool
	Limit        int32
}

type MailboxOAuthWorkflowResult struct {
	JobID        string
	Success      bool
	Processed    int32
	Succeeded    int32
	Failed       int32
	ErrorMessage string
}
