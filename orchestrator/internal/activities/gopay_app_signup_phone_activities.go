package activities

import (
	"context"
	"fmt"

	pb "orchestrator/pb"
)

func (s *Server) GoPayAppAcquireSignupPhoneActivity(ctx context.Context, input GoPayAppAcquireSignupPhoneInput) (GoPayAppAcquireSignupPhoneOutput, error) {
	output := GoPayAppAcquireSignupPhoneOutput{}
	data := map[string]any{}
	step := s.activityStep(ctx, input.GetJobId(), stepGoPayAppSignupPhone, false, true)
	_, err := step.run(func() (any, error) {
		if s.gopayClient == nil || s.smsClient == nil {
			err := fmt.Errorf("gopay app or sms client not configured")
			data["error_message"] = err.Error()
			return data, err
		}

		maxFailures := s.changePhoneMaxFailureCount()
		failures := int(input.GetFailureCount())
		if failures < 0 {
			failures = 0
		}
		otpWaitSeconds := s.changePhoneOTPWaitTimeoutSeconds()
		data["failure_count"] = failures
		data["max_failures"] = maxFailures
		data["otp_timeout_seconds"] = otpWaitSeconds

		for failures < maxFailures {
			step.progress("acquiring unregistered gopay phone", map[string]any{
				"failures":     failures,
				"max_failures": maxFailures,
			})
			numResp, err := s.smsClient.AcquireNumber(ctx, &pb.AcquireNumberRequest{})
			if err != nil {
				err = fmt.Errorf("AcquireNumber: %w", err)
				data["error_message"] = err.Error()
				return data, err
			}
			if numResp == nil || !numResp.GetSuccess() {
				message := ""
				if numResp != nil {
					message = numResp.GetErrorMessage()
				}
				if message == "" {
					message = "empty response"
				}
				failures++
				data["failure_count"] = failures
				data["last_error"] = fmt.Sprintf("AcquireNumber: %s", message)
				if delay := s.changePhoneGetNumberRetryInterval(); delay > 0 {
					if err := sleepContext(ctx, delay); err != nil {
						err = fmt.Errorf("waiting to retry AcquireNumber: %w", err)
						data["error_message"] = err.Error()
						return data, err
					}
				}
				continue
			}

			phone := normalizeIndonesiaPhone(numResp.GetPhone())
			activationID := numResp.GetActivationId()
			data["activation_id"] = activationID
			data["phone_present"] = phone != ""
			if phone == "" {
				s.cancelSMSActivation(ctx, activationID)
				failures++
				data["failure_count"] = failures
				continue
			}

			checkResp, err := s.gopayClient.CheckPhone(ctx, &pb.CheckPhoneRequest{Phone: phone})
			if err != nil {
				s.cancelSMSActivation(ctx, activationID)
				failures++
				data["failure_count"] = failures
				data["phone_status"] = "error"
				data["last_error"] = fmt.Sprintf("CheckPhone: %v", err)
				continue
			}
			status := checkPhoneStatus(checkResp)
			data["phone_status"] = status
			step.progress("gopay phone availability checked", map[string]any{
				"activation_id": activationID,
				"status":        status,
			})
			if status != "available" {
				s.cancelSMSActivation(ctx, activationID)
				failures++
				data["failure_count"] = failures
				continue
			}

			output.ActivationId = activationID
			output.Phone = phone
			output.FailureCount = int32(failures)
			output.MaxFailures = int32(maxFailures)
			output.OtpTimeoutSeconds = otpWaitSeconds
			data["signup_phone_acquired"] = true
			data["failure_count"] = failures
			return data, nil
		}

		err := fmt.Errorf("failed to acquire unregistered gopay phone after %d failures", maxFailures)
		data["failure_count"] = failures
		data["error_message"] = err.Error()
		return data, err
	})
	output.Data = protoData(data)
	return output, err
}
