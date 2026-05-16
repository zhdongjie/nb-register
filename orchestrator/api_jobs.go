package main

import (
	"context"
	"orchestrator/pb"
	"strings"
)

func (s *orchestratorServer) GetJob(ctx context.Context, req *pb.GetJobRequest) (*pb.GetJobResponse, error) {
	jobID := strings.TrimSpace(req.GetJobId())
	if jobID == "" {
		return &pb.GetJobResponse{ErrorMessage: "job_id is required"}, nil
	}

	job, err := s.getJob(ctx, jobID)
	if err != nil {
		return &pb.GetJobResponse{ErrorMessage: err.Error()}, nil
	}

	steps, err := s.jobStore.Steps(ctx, jobID)
	if err != nil {
		return &pb.GetJobResponse{ErrorMessage: err.Error()}, nil
	}

	return &pb.GetJobResponse{Job: jobToProto(job, steps)}, nil
}

func (s *orchestratorServer) RequestAccount(ctx context.Context, req *pb.RequestAccountRequest) (*pb.RequestAccountResponse, error) {
	resp, err := s.RegisterAccount(ctx, &pb.RegisterAccountRequest{})
	if err != nil {
		return nil, err
	}

	return &pb.RequestAccountResponse{
		JobId:             resp.JobId,
		SessionToken:      resp.SessionToken,
		AccessToken:       resp.AccessToken,
		PlusTrialEligible: resp.PlusTrialEligible,
		ErrorMessage:      resp.ErrorMessage,
		CheckoutUrl:       resp.CheckoutUrl,
	}, nil
}
