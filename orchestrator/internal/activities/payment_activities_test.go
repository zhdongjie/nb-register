package activities

import (
	"errors"
	"testing"

	"orchestrator/pb"
)

func TestIsStalePreparedPaymentFlow(t *testing.T) {
	if !isStalePreparedPaymentFlow(&pb.StartGoPayResponse{Success: false, ErrorMessage: "prepared payment flow not found"}, nil) {
		t.Fatalf("prepared flow error was not detected as stale")
	}
	if !isStalePreparedPaymentFlow(nil, errors.New("payment flow not found")) {
		t.Fatalf("transport error was not detected as stale")
	}
	if isStalePreparedPaymentFlow(&pb.StartGoPayResponse{Success: false, ErrorMessage: "otp required"}, nil) {
		t.Fatalf("unrelated error was detected as stale")
	}
}
