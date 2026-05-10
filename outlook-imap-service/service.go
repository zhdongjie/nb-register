package main

import (
	"context"
	"errors"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"outlookimapservice/pb"
)

type EmailService struct {
	pb.UnimplementedEmailServiceServer
	store     *MailboxStore
	watcher   *MailWatcher
	inboxOnce sync.Once
	inboxGate chan struct{}
}

func (s *EmailService) acquireInboxLock(ctx context.Context) (func(), error) {
	s.inboxOnce.Do(func() {
		s.inboxGate = make(chan struct{}, 1)
	})
	select {
	case s.inboxGate <- struct{}{}:
		return func() { <-s.inboxGate }, nil
	case <-ctx.Done():
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return nil, status.Error(codes.DeadlineExceeded, "inbox fetch wait timeout")
		}
		return nil, status.Error(codes.Canceled, "request cancelled")
	}
}

func (s *EmailService) GetEmail(ctx context.Context, request *pb.GetEmailRequest) (*pb.GetEmailResponse, error) {
	mailbox, err := s.store.AcquireEmail(ctx, request.GetExcludeEmailAddresses())
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	return &pb.GetEmailResponse{
		EmailAddress: mailbox.GetEmailAddress(),
		Password:     mailbox.GetPassword(),
		Mailbox:      mailbox,
	}, nil
}

func (s *EmailService) MarkEmailStatus(ctx context.Context, request *pb.MarkEmailStatusRequest) (*pb.MarkEmailStatusResponse, error) {
	mailbox, err := s.store.MarkEmailStatus(ctx, request.GetEmailAddress(), request.GetStatus(), request.GetLastError())
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	return &pb.MarkEmailStatusResponse{Mailbox: mailbox}, nil
}

func (s *EmailService) MarkEmailAuthStatus(ctx context.Context, request *pb.MarkEmailAuthStatusRequest) (*pb.MarkEmailAuthStatusResponse, error) {
	mailbox, err := s.store.MarkEmailAuthStatus(ctx, request.GetEmailAddress(), request.GetAuthStatus(), request.GetLastError())
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	return &pb.MarkEmailAuthStatusResponse{Mailbox: mailbox}, nil
}

func (s *EmailService) UpsertMailbox(ctx context.Context, request *pb.UpsertEmailMailboxRequest) (*pb.UpsertEmailMailboxResponse, error) {
	mailbox, err := s.store.UpsertMailbox(ctx, request.GetMailbox())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	return &pb.UpsertEmailMailboxResponse{Mailbox: mailbox}, nil
}

func (s *EmailService) ListMailboxes(ctx context.Context, request *pb.ListEmailMailboxesRequest) (*pb.ListEmailMailboxesResponse, error) {
	mailboxes, err := s.store.ListMailboxes(ctx, request.GetStatus(), request.GetLimit())
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	return &pb.ListEmailMailboxesResponse{Mailboxes: mailboxes}, nil
}

func (s *EmailService) DeleteMailbox(ctx context.Context, request *pb.DeleteMailboxRequest) (*pb.DeleteMailboxResponse, error) {
	deleted, err := s.store.DeleteMailbox(ctx, request.GetEmailAddress())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	return &pb.DeleteMailboxResponse{Deleted: deleted}, nil
}

func (s *EmailService) FetchInboxes(ctx context.Context, request *pb.FetchInboxesRequest) (*pb.FetchInboxesResponse, error) {
	unlock, err := s.acquireInboxLock(ctx)
	if err != nil {
		return nil, err
	}
	defer unlock()

	type inboxTarget struct {
		fetchMailbox  *pb.EmailMailbox
		resultMailbox *pb.EmailMailbox
	}
	targets := []inboxTarget{}
	requestedEmail := normalizeEmail(request.GetEmailAddress())
	if requestedEmail != "" {
		fetchMailbox, err := s.store.PollMailboxForEmail(ctx, requestedEmail)
		if err != nil {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		resultMailbox := fetchMailbox
		if mailbox, err := s.store.FindMailbox(ctx, requestedEmail); err == nil {
			resultMailbox = mailbox
		}
		targets = append(targets, inboxTarget{fetchMailbox: fetchMailbox, resultMailbox: resultMailbox})
	} else {
		mailboxes, err := s.store.ListOAuthPrimaryMailboxes(ctx, request.GetMaxMailboxes())
		if err != nil {
			return nil, status.Error(codes.Internal, err.Error())
		}
		for _, mailbox := range mailboxes {
			targets = append(targets, inboxTarget{fetchMailbox: mailbox, resultMailbox: mailbox})
		}
	}

	resp := &pb.FetchInboxesResponse{
		MailboxCount: int32(len(targets)),
		Results:      []*pb.FetchMailboxInboxResult{},
	}
	for _, target := range targets {
		select {
		case <-ctx.Done():
			return nil, status.Error(codes.Canceled, "request cancelled")
		default:
		}

		result := &pb.FetchMailboxInboxResult{Mailbox: target.resultMailbox}
		messages, err := s.watcher.FetchMailboxInbox(ctx, target.fetchMailbox, request.GetLimitPerMailbox())
		if err != nil {
			result.ErrorMessage = err.Error()
			resp.FailedCount++
		} else {
			result.Messages = messages
			resp.FetchedCount++
			resp.MessageCount += int32(len(messages))
		}
		resp.Results = append(resp.Results, result)
	}
	return resp, nil
}

func (s *EmailService) WaitForEmail(ctx context.Context, request *pb.WaitForEmailRequest) (*pb.WaitForEmailResponse, error) {
	timeoutSeconds := request.GetTimeoutSeconds()
	if timeoutSeconds <= 0 {
		timeoutSeconds = 300
	}
	issuedAfter := float64(request.GetIssuedAfterUnix())
	if otp, ok := s.watcher.ConsumeCachedOTP(request.GetEmailAddress(), request.GetSubjectKeyword(), issuedAfter); ok {
		return &pb.WaitForEmailResponse{Found: true, ContentExtracted: otp}, nil
	}
	if err := s.watcher.PollForEmail(ctx, request.GetEmailAddress()); err != nil {
		return nil, waitError(ctx, err)
	}
	if otp, ok := s.watcher.ConsumeCachedOTP(request.GetEmailAddress(), request.GetSubjectKeyword(), issuedAfter); ok {
		return &pb.WaitForEmailResponse{Found: true, ContentExtracted: otp}, nil
	}

	deadline := time.Now().Add(time.Duration(timeoutSeconds) * time.Second)
	for time.Now().Before(deadline) {
		sleepFor := time.Duration(s.watcher.pollInterval) * time.Second
		if remaining := time.Until(deadline); remaining < sleepFor {
			sleepFor = remaining
		}
		if sleepFor > 0 {
			timer := time.NewTimer(sleepFor)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, status.Error(codes.Canceled, "request cancelled")
			case <-timer.C:
			}
		}
		if err := s.watcher.PollForEmail(ctx, request.GetEmailAddress()); err != nil {
			return nil, waitError(ctx, err)
		}
		if otp, ok := s.watcher.ConsumeCachedOTP(request.GetEmailAddress(), request.GetSubjectKeyword(), issuedAfter); ok {
			return &pb.WaitForEmailResponse{Found: true, ContentExtracted: otp}, nil
		}
	}
	return &pb.WaitForEmailResponse{Found: false}, nil
}

func waitError(ctx context.Context, err error) error {
	if errors.Is(err, context.Canceled) || errors.Is(ctx.Err(), context.Canceled) {
		return status.Error(codes.Canceled, "request cancelled")
	}
	return status.Error(codes.Internal, err.Error())
}
