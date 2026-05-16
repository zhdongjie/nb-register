package app

import (
	"log"
	"net"

	temporalworker "go.temporal.io/sdk/worker"
	"google.golang.org/grpc"
	"orchestrator/internal/api"
	"orchestrator/pb"
)

func Run() {
	log.Println("Initializing Orchestrator API...")

	cfg := loadOrchestratorConfig()
	deps, err := newOrchestratorDependencies(cfg)
	if err != nil {
		log.Fatalf("Failed to initialize orchestrator dependencies: %v", err)
	}
	defer deps.Close()

	lis, err := net.Listen("tcp", cfg.ListenAddr)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	activityServer := newActivityServer(cfg, deps)
	apiServer := api.NewServer(api.Config{
		DB:                          deps.db,
		JobStore:                    deps.jobStore,
		JobEvents:                   deps.jobEvents,
		Temporal:                    deps.temporal,
		TaskQueue:                   cfg.TemporalTaskQueue,
		AccountClient:               deps.accountClient,
		EmailClient:                 deps.emailClient,
		GoPayClient:                 deps.gopayClient,
		OutlookRegisterEnableOAuth2: cfg.OutlookRegisterEnableOAuth2,
	})

	temporalWorker := temporalworker.New(deps.temporal, cfg.TemporalTaskQueue, temporalworker.Options{})
	registerTemporalWorker(temporalWorker, activityServer)
	go func() {
		if err := temporalWorker.Run(temporalworker.InterruptCh()); err != nil {
			log.Fatalf("Temporal worker failed: %v", err)
		}
	}()

	grpcServer := grpc.NewServer()
	pb.RegisterAccountWorkflowServiceServer(grpcServer, apiServer)
	pb.RegisterPaymentWorkflowServiceServer(grpcServer, apiServer)
	pb.RegisterGoPayAppWorkflowServiceServer(grpcServer, apiServer)
	pb.RegisterMailboxWorkflowServiceServer(grpcServer, apiServer)
	pb.RegisterOTPServiceServer(grpcServer, apiServer)
	pb.RegisterJobServiceServer(grpcServer, apiServer)

	log.Printf("Orchestrator gRPC API listening on %s", cfg.ListenAddr)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}
