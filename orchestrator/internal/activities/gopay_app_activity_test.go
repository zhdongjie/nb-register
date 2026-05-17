package activities

import (
	"context"
	"testing"
	"time"

	pb "orchestrator/pb"

	"google.golang.org/grpc"
)

type blockingSMSClient struct {
	pb.SmsServiceClient
	started chan string
	release chan struct{}
}

func (c *blockingSMSClient) CancelActivation(ctx context.Context, in *pb.CancelActivationRequest, opts ...grpc.CallOption) (*pb.ProviderActionResponse, error) {
	c.started <- in.GetActivationId()
	select {
	case <-c.release:
		return &pb.ProviderActionResponse{Success: true}, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

type scriptedCancelSMSClient struct {
	pb.SmsServiceClient
	started   chan string
	responses chan *pb.ProviderActionResponse
}

func (c *scriptedCancelSMSClient) CancelActivation(ctx context.Context, in *pb.CancelActivationRequest, opts ...grpc.CallOption) (*pb.ProviderActionResponse, error) {
	c.started <- in.GetActivationId()
	select {
	case resp := <-c.responses:
		return resp, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestEnsureLogonDataUsesLogonComplete(t *testing.T) {
	data := ensureLogonData(&pb.EnsureLogonResponse{
		Ready:         true,
		LogonComplete: true,
	})

	if data["logon_complete"] != true {
		t.Fatalf("logon_complete = %v, want true", data["logon_complete"])
	}
}

func TestNormalizeIndonesiaPhone(t *testing.T) {
	tests := map[string]string{
		"+6281234567890": "81234567890",
		"6281234567890":  "81234567890",
		"081234567890":   "081234567890",
	}
	for input, want := range tests {
		if got := normalizeIndonesiaPhone(input); got != want {
			t.Fatalf("normalizeIndonesiaPhone(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestGoPayWorkflowStatePresentRequiresNonEmptyObject(t *testing.T) {
	if goPayWorkflowStatePresent("") {
		t.Fatal("empty state reported present")
	}
	if goPayWorkflowStatePresent("{}") {
		t.Fatal("empty object reported present")
	}
	if !goPayWorkflowStatePresent(`{"token":"token"}`) {
		t.Fatal("token state reported missing")
	}
}

func TestSMSNoNumbers(t *testing.T) {
	if !smsNoNumbers("NO_NUMBERS") {
		t.Fatal("NO_NUMBERS was not detected")
	}
	if !smsNoNumbers("GetNumber: no_numbers") {
		t.Fatal("case-insensitive NO_NUMBERS was not detected")
	}
	if smsNoNumbers("BAD_KEY") {
		t.Fatal("unrelated provider error was detected as NO_NUMBERS")
	}
}

func TestChangePhoneStartRetryableError(t *testing.T) {
	if !changePhoneStartRetryableError("PHONE_REGISTERED") {
		t.Fatal("PHONE_REGISTERED should be retryable")
	}
	if !changePhoneStartRetryableError("phone_exhausted") {
		t.Fatal("PHONE_EXHAUSTED should be retryable")
	}
	if changePhoneStartRetryableError("BAD_PIN") {
		t.Fatal("BAD_PIN should not be retryable")
	}
}

func TestCancelSMSActivationAsyncDoesNotBlockCaller(t *testing.T) {
	client := &blockingSMSClient{
		started: make(chan string, 1),
		release: make(chan struct{}),
	}
	server := &Server{smsClient: client}
	done := make(chan struct{})

	go func() {
		server.cancelSMSActivationAsync("act-registered", "registered phone")
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(100 * time.Millisecond):
		t.Fatal("cancelSMSActivationAsync blocked caller")
	}

	select {
	case got := <-client.started:
		if got != "act-registered" {
			t.Fatalf("activation id = %q, want act-registered", got)
		}
	case <-time.After(time.Second):
		t.Fatal("async cancel was not started")
	}
	close(client.release)
}

func TestCancelSMSActivationAsyncRetriesEarlyCancelDenied(t *testing.T) {
	client := &scriptedCancelSMSClient{
		started:   make(chan string, 2),
		responses: make(chan *pb.ProviderActionResponse, 2),
	}
	client.responses <- &pb.ProviderActionResponse{
		Success:      false,
		ErrorMessage: "EARLY_CANCEL_DENIED",
		RawResponse:  "Activation cannot be cancelled at this time",
	}
	client.responses <- &pb.ProviderActionResponse{Success: true, RawResponse: "ACCESS_CANCEL"}
	server := &Server{
		smsClient:                         client,
		changePhoneSMSCancelTimeout:       100 * time.Millisecond,
		changePhoneSMSCancelRetryInterval: time.Millisecond,
	}

	server.cancelSMSActivationAsync("act-early", "registered phone")

	for i := 0; i < 2; i++ {
		select {
		case got := <-client.started:
			if got != "act-early" {
				t.Fatalf("activation id = %q, want act-early", got)
			}
		case <-time.After(time.Second):
			t.Fatalf("cancel attempt %d was not started", i+1)
		}
	}
}
