package api

import (
	"context"
	"fmt"
	"strings"

	"orchestrator/pb"
)

func (s *Server) loadGoPayAppStateForKey(ctx context.Context, stateKey string) (string, error) {
	if s.gopayClient == nil {
		return "{}", fmt.Errorf("gopay-app client not configured")
	}
	resp, err := s.gopayClient.GetGoPayState(ctx, &pb.GetGoPayStateRequest{StateKey: stateKey})
	if err != nil {
		return "", fmt.Errorf("GetGoPayState: %w", err)
	}
	if resp == nil {
		return "", fmt.Errorf("GetGoPayState returned empty response")
	}
	if !resp.GetSuccess() {
		return "", fmt.Errorf("GetGoPayState: %s", resp.GetErrorMessage())
	}
	stateJSON := strings.TrimSpace(resp.GetStateJson())
	if stateJSON == "" {
		stateJSON = "{}"
	}
	return stateJSON, nil
}

func (s *Server) saveGoPayAppStateForKey(ctx context.Context, stateKey string, stateJSON string) error {
	stateJSON = strings.TrimSpace(stateJSON)
	if stateJSON == "" {
		return nil
	}
	if s.gopayClient == nil {
		return fmt.Errorf("gopay-app client not configured")
	}
	resp, err := s.gopayClient.UpsertGoPayState(ctx, &pb.UpsertGoPayStateRequest{
		StateKey:  stateKey,
		StateJson: stateJSON,
	})
	if err != nil {
		return fmt.Errorf("UpsertGoPayState: %w", err)
	}
	if resp == nil {
		return fmt.Errorf("UpsertGoPayState returned empty response")
	}
	if !resp.GetSuccess() {
		return fmt.Errorf("UpsertGoPayState: %s", resp.GetErrorMessage())
	}
	return nil
}

func (s *Server) deleteGoPayAppStateForKey(ctx context.Context, stateKey string) error {
	if s.gopayClient == nil {
		return fmt.Errorf("gopay-app client not configured")
	}
	resp, err := s.gopayClient.DeleteGoPayState(ctx, &pb.DeleteGoPayStateRequest{StateKey: stateKey})
	if err != nil {
		return fmt.Errorf("DeleteGoPayState: %w", err)
	}
	if resp == nil {
		return fmt.Errorf("DeleteGoPayState returned empty response")
	}
	if !resp.GetSuccess() {
		return fmt.Errorf("DeleteGoPayState: %s", resp.GetErrorMessage())
	}
	return nil
}

func goPayStatusSnapshot(resp *pb.StatusResponse, err error) *pb.StatusResponse {
	if resp == nil && err == nil {
		return nil
	}
	snapshot := &pb.StatusResponse{}
	if resp != nil {
		snapshot.Stage = resp.GetStage()
		snapshot.Phone = resp.GetPhone()
		snapshot.TokenPresent = resp.GetTokenPresent()
		snapshot.DeviceFingerprint = resp.GetDeviceFingerprint()
		snapshot.DeactivatedAt = resp.GetDeactivatedAt()
		snapshot.ErrorMessage = resp.GetErrorMessage()
		snapshot.BalanceAmount = resp.GetBalanceAmount()
		snapshot.HasMinBalance = resp.GetHasMinBalance()
		snapshot.BalanceCurrency = resp.GetBalanceCurrency()
	}
	if err != nil {
		snapshot.ErrorMessage = err.Error()
	}
	return snapshot
}

func goPayStatusTokenReady(resp *pb.StatusResponse) bool {
	return resp != nil && resp.GetTokenPresent() && strings.TrimSpace(resp.GetStage()) == "ready"
}
