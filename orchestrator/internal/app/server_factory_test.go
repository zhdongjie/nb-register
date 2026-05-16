package app

import (
	"testing"
	"time"
)

func TestNewOrchestratorServerInjectsConfigAndDependencies(t *testing.T) {
	cfg := orchestratorConfig{
		GoPayOTPServiceAddr:               "otp:50051",
		GoPayOTPTimeout:                   70,
		RegistrationOTPWait:               190,
		GoPayAppStepBodyLimit:             7000,
		GoPayAppLinkPaymentTimeout:        181 * time.Second,
		GoPayAppUnlinkTimeout:             16 * time.Second,
		OutlookRegisterEnableOAuth2:       true,
		ChangePhoneMaxFailures:            5,
		ChangePhoneDisabled:               true,
		ChangePhoneOTPWaitSeconds:         130,
		ChangePhoneOTPRetryAttempts:       3,
		ChangePhoneGetNumberRetryDelay:    7 * time.Second,
		ChangePhoneSMSCancelTimeout:       140 * time.Second,
		ChangePhoneSMSCancelRetryInterval: 12 * time.Second,
		TemporalTaskQueue:                 "custom-task-queue",
	}
	deps := &orchestratorDependencies{}

	activityCfg := activityConfig(cfg, deps)

	if activityCfg.OTPAddr != cfg.GoPayOTPServiceAddr {
		t.Fatalf("OTPAddr = %q", activityCfg.OTPAddr)
	}
	if activityCfg.OTPTimeout != cfg.GoPayOTPTimeout || activityCfg.RegistrationOTPTimeout != cfg.RegistrationOTPWait {
		t.Fatalf("otp timeouts = %d/%d", activityCfg.OTPTimeout, activityCfg.RegistrationOTPTimeout)
	}
	if activityCfg.GoPayAppStepBodyLimit != cfg.GoPayAppStepBodyLimit ||
		activityCfg.GoPayAppLinkPaymentTimeout != cfg.GoPayAppLinkPaymentTimeout ||
		activityCfg.GoPayAppUnlinkTimeout != cfg.GoPayAppUnlinkTimeout {
		t.Fatalf("runtime config mismatch: %+v", activityCfg)
	}
	if activityCfg.ChangePhoneMaxFailures != cfg.ChangePhoneMaxFailures ||
		activityCfg.ChangePhoneDisabled != cfg.ChangePhoneDisabled ||
		activityCfg.ChangePhoneOTPWaitSeconds != cfg.ChangePhoneOTPWaitSeconds ||
		activityCfg.ChangePhoneOTPRetryAttempts != cfg.ChangePhoneOTPRetryAttempts ||
		activityCfg.ChangePhoneGetNumberRetryDelay != cfg.ChangePhoneGetNumberRetryDelay ||
		activityCfg.ChangePhoneSMSCancelTimeout != cfg.ChangePhoneSMSCancelTimeout ||
		activityCfg.ChangePhoneSMSCancelRetryInterval != cfg.ChangePhoneSMSCancelRetryInterval {
		t.Fatalf("change phone config mismatch: %+v", activityCfg)
	}
}
