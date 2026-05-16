package activities

import (
	"context"
	"fmt"
	"strings"

	pb "orchestrator/pb"
)

func (s *Server) GoPayAppAddBalanceActivity(ctx context.Context, input GoPayAppAddBalanceInput) (GoPayAppAddBalanceOutput, error) {
	output := GoPayAppAddBalanceOutput{StateJson: normalizeGoPayWorkflowStateJSON(input.GetStateJson())}
	data := map[string]any{}
	step := s.activityStep(ctx, input.GetJobId(), stepGoPayAppAddBalance, false, true)
	_, err := step.run(func() (any, error) {
		return s.runGoPayAddBalance(ctx, step, input, &output, data)
	})
	output.Data = protoData(data)
	return output, err
}

func (s *Server) runGoPayAddBalance(ctx context.Context, step activityStep, input GoPayAppAddBalanceInput, output *GoPayAppAddBalanceOutput, data map[string]any) (any, error) {
	addBalance := input.GetAddBalance()
	if addBalance == nil {
		err := fmt.Errorf("add_balance is required")
		data["error_message"] = err.Error()
		return data, err
	}
	switch {
	case addBalance.GetManualTransfer() != nil:
		return s.prepareManualTransferAddBalance(ctx, step, addBalance.GetManualTransfer(), output, data)
	case addBalance.GetEnvelope() != nil:
		return s.claimEnvelopeAddBalance(ctx, step, addBalance.GetEnvelope(), output, data)
	default:
		err := fmt.Errorf("add_balance method is required")
		data["error_message"] = err.Error()
		return data, err
	}
}

func (s *Server) prepareManualTransferAddBalance(ctx context.Context, step activityStep, transfer *pb.GoPayManualTransferAddBalance, output *GoPayAppAddBalanceOutput, data map[string]any) (any, error) {
	if s.gopayClient == nil {
		err := fmt.Errorf("gopay-app client not configured")
		data["error_message"] = err.Error()
		return data, err
	}

	data["method"] = "manual_transfer"
	data["status"] = "awaiting_manual_confirmation"
	data["manual_confirmation_required"] = true
	output.Method = "manual_transfer"
	output.Status = "awaiting_manual_confirmation"
	output.Success = true

	step.progress("fetching qr_id from gopay-app", nil)
	resp, err := s.gopayClient.GetQrId(ctx, &pb.GetQrIdRequest{StateJson: output.GetStateJson()})
	if err != nil {
		err = fmt.Errorf("GetQrId: %w", err)
		data["error_message"] = err.Error()
		return data, err
	}
	if !resp.GetSuccess() {
		err = fmt.Errorf("GetQrId: %s", resp.GetErrorMessage())
		data["error_message"] = err.Error()
		return data, err
	}

	qrPayload := fmt.Sprintf(`{"qr_id":"%s"}`, resp.GetQrId())
	data["manual_transfer"] = map[string]any{
		"configured":         true,
		"qr_payload":         qrPayload,
		"qr_payload_present": true,
		"qr_image_present":   false,
		"instructions":       strings.TrimSpace(transfer.GetInstructions()),
		"amount":             transfer.GetAmount(),
		"currency":           strings.TrimSpace(transfer.GetCurrency()),
	}
	step.progress("waiting for manual gopay transfer confirmation", map[string]any{
		"qr_payload_present": true,
	})
	return data, nil
}

func (s *Server) claimEnvelopeAddBalance(ctx context.Context, step activityStep, envelope *pb.GoPayEnvelopeAddBalance, output *GoPayAppAddBalanceOutput, data map[string]any) (any, error) {
	if s.gopayClient == nil {
		err := fmt.Errorf("gopay-app client not configured")
		data["error_message"] = err.Error()
		return data, err
	}

	envelopeLink := strings.TrimSpace(envelope.GetLink())
	envelopeRequestID := strings.TrimSpace(envelope.GetEnvelopeRequestId())
	data["method"] = "envelope"
	data["status"] = "claiming"
	data["envelope_link_present"] = envelopeLink != ""
	data["envelope_request_id_present"] = envelopeRequestID != ""
	if envelopeLink == "" && envelopeRequestID == "" {
		err := fmt.Errorf("GOPAY_ADD_BALANCE_ENVELOPE_LINK or envelope_request_id is required")
		data["error_message"] = err.Error()
		return data, err
	}

	step.progress("claiming gopay envelope", map[string]any{
		"envelope_link_present":       envelopeLink != "",
		"envelope_request_id_present": envelopeRequestID != "",
	})
	resp, err := s.gopayClient.ClaimEnvelope(ctx, &pb.ClaimEnvelopeRequest{
		EnvelopeRequestId: envelopeRequestID,
		Link:              envelopeLink,
		StateJson:         output.GetStateJson(),
	})
	output.StateJson = goPayWorkflowStateAfter(output.GetStateJson(), responseStateJSON(resp))
	data["envelope"] = claimEnvelopeData(resp)
	if err != nil {
		err = fmt.Errorf("ClaimEnvelope: %w", err)
		data["error_message"] = err.Error()
		return data, err
	}
	if resp == nil {
		err := fmt.Errorf("ClaimEnvelope returned empty response")
		data["error_message"] = err.Error()
		return data, err
	}
	output.Success = resp.GetSuccess()
	output.Method = "envelope"
	output.Status = resp.GetStatus()
	data["status"] = resp.GetStatus()
	if !resp.GetSuccess() {
		message := strings.TrimSpace(resp.GetErrorMessage())
		if message == "" {
			message = "claim envelope failed"
		}
		output.ErrorMessage = message
		err := fmt.Errorf("ClaimEnvelope: %s", message)
		data["error_message"] = err.Error()
		return data, err
	}
	data["add_balance_complete"] = true
	return data, nil
}

func claimEnvelopeData(resp *pb.ClaimEnvelopeResponse) map[string]any {
	if resp == nil {
		return map[string]any{"response_present": false}
	}
	return map[string]any{
		"response_present":             true,
		"success":                      resp.GetSuccess(),
		"error_message":                resp.GetErrorMessage(),
		"envelope_request_id":          resp.GetEnvelopeRequestId(),
		"response_envelope_request_id": resp.GetResponseEnvelopeRequestId(),
		"status":                       resp.GetStatus(),
		"http_status":                  resp.GetHttpStatus(),
		"raw_json":                     limitStepText(resp.GetRawJson(), 2000),
	}
}

func limitStepText(value string, limit int) string {
	if limit <= 0 || len(value) <= limit {
		return value
	}
	return value[:limit] + "...<truncated>"
}
