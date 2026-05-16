package main

import (
	"context"
	"log"
	"net"
	"os"
	"strings"
	"time"

	"github.com/brianvoe/gofakeit/v6"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"gorm.io/gorm"

	"accountdb/db"
	"accountdb/pb"
)

type accountDatabaseServer struct {
	pb.UnimplementedAccountDatabaseServiceServer
	db *gorm.DB
}

func (s *accountDatabaseServer) CreateAccount(ctx context.Context, req *pb.CreateAccountRequest) (*pb.CreateAccountResponse, error) {
	account, err := s.buildAccount(req.GetAccount())
	if err != nil {
		return nil, err
	}
	if account.Email == "" {
		return nil, status.Error(codes.InvalidArgument, "email is required")
	}

	if err := s.db.WithContext(ctx).Create(account).Error; err != nil {
		return nil, err
	}

	log.Printf("Created account id=%s email=%s", account.ID, redactEmail(account.Email))
	return &pb.CreateAccountResponse{Account: accountToProto(account)}, nil
}

func (s *accountDatabaseServer) GetAccount(ctx context.Context, req *pb.GetAccountRequest) (*pb.GetAccountResponse, error) {
	account, err := s.findAccount(ctx, req.GetAccountId())
	if err != nil {
		return nil, err
	}
	return &pb.GetAccountResponse{Account: accountToProto(account)}, nil
}

func (s *accountDatabaseServer) UpdateAccount(ctx context.Context, req *pb.UpdateAccountRequest) (*pb.UpdateAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccount().GetAccountId())
	if accountID == "" {
		return nil, status.Error(codes.InvalidArgument, "account_id is required")
	}

	if _, err := s.findAccount(ctx, accountID); err != nil {
		return nil, err
	}

	updates := updateMap(req.GetAccount())
	if len(updates) > 0 {
		if err := s.db.WithContext(ctx).Model(&db.Account{}).Where("id = ?", accountID).Updates(updates).Error; err != nil {
			return nil, err
		}
	}

	account, err := s.findAccount(ctx, accountID)
	if err != nil {
		return nil, err
	}
	log.Printf("Updated account id=%s status=%s", account.ID, account.Status)
	return &pb.UpdateAccountResponse{Account: accountToProto(account)}, nil
}

func (s *accountDatabaseServer) DeleteAccount(ctx context.Context, req *pb.DeleteAccountRequest) (*pb.DeleteAccountResponse, error) {
	accountID := strings.TrimSpace(req.GetAccountId())
	if accountID == "" {
		return nil, status.Error(codes.InvalidArgument, "account_id is required")
	}

	result := s.db.WithContext(ctx).Delete(&db.Account{}, "id = ?", accountID)
	if result.Error != nil {
		return nil, result.Error
	}
	if result.RowsAffected == 0 {
		return nil, status.Error(codes.NotFound, "account not found")
	}
	return &pb.DeleteAccountResponse{Ack: true}, nil
}

func (s *accountDatabaseServer) ListAccounts(ctx context.Context, req *pb.ListAccountsRequest) (*pb.ListAccountsResponse, error) {
	limit := int(req.GetLimit())
	if limit <= 0 {
		limit = 50
	}
	if limit > 500 {
		limit = 500
	}

	query := s.db.WithContext(ctx).Order("created_at DESC").Limit(limit)
	if statusFilter := strings.TrimSpace(req.GetStatus()); statusFilter != "" {
		query = query.Where("status = ?", statusFilter)
	}
	if emailFilter := normalizeEmail(req.GetEmail()); emailFilter != "" {
		query = query.Where("email = ?", emailFilter)
	}

	var accounts []db.Account
	if err := query.Find(&accounts).Error; err != nil {
		return nil, err
	}

	resp := &pb.ListAccountsResponse{Accounts: make([]*pb.Account, 0, len(accounts))}
	for i := range accounts {
		resp.Accounts = append(resp.Accounts, accountToProto(&accounts[i]))
	}
	return resp, nil
}

func (s *accountDatabaseServer) buildAccount(input *pb.Account) (*db.Account, error) {
	if input == nil {
		input = &pb.Account{}
	}

	now := time.Now()
	account := &db.Account{
		ID:           strings.TrimSpace(input.GetAccountId()),
		Email:        strings.TrimSpace(input.GetEmail()),
		Password:     input.GetPassword(),
		Status:       strings.TrimSpace(input.GetStatus()),
		ErrorMessage: input.GetErrorMessage(),
		SessionToken: strings.TrimSpace(input.GetSessionToken()),
		AccessToken:  strings.TrimSpace(input.GetAccessToken()),
		ChargeRef:    strings.TrimSpace(input.GetChargeRef()),
		FirstName:    strings.TrimSpace(input.GetFirstName()),
		LastName:     strings.TrimSpace(input.GetLastName()),
		DOB:          strings.TrimSpace(input.GetDob()),
		Tier:         normalizeTier(input.GetTier()),
	}
	if input.PlusTrialEligible != nil {
		value := input.GetPlusTrialEligible()
		account.PlusTrialEligible = &value
	}
	if input.PlusActive != nil {
		value := input.GetPlusActive()
		account.PlusActive = &value
	}

	if account.ID == "" {
		account.ID = gofakeit.UUID()
	}
	account.Email = normalizeEmail(account.Email)
	if account.Password == "" {
		account.Password = gofakeit.Password(true, true, true, true, false, 12)
	}
	if account.FirstName == "" {
		account.FirstName = gofakeit.FirstName()
	}
	if account.LastName == "" {
		account.LastName = gofakeit.LastName()
	}
	if account.DOB == "" {
		account.DOB = randomDOB(now)
	}
	if account.Status == "" {
		account.Status = "CREATED"
	}

	return account, nil
}

func (s *accountDatabaseServer) findAccount(ctx context.Context, accountID string) (*db.Account, error) {
	accountID = strings.TrimSpace(accountID)
	if accountID == "" {
		return nil, status.Error(codes.InvalidArgument, "account_id is required")
	}

	var account db.Account
	err := s.db.WithContext(ctx).First(&account, "id = ?", accountID).Error
	if err == gorm.ErrRecordNotFound {
		return nil, status.Error(codes.NotFound, "account not found")
	}
	if err != nil {
		return nil, err
	}
	return &account, nil
}

func updateMap(account *pb.Account) map[string]interface{} {
	updates := map[string]interface{}{}
	if account == nil {
		return updates
	}

	if value := normalizeEmail(account.GetEmail()); value != "" {
		updates["email"] = value
	}
	if value := account.GetPassword(); value != "" {
		updates["password"] = value
	}
	if value := strings.TrimSpace(account.GetStatus()); value != "" {
		updates["status"] = value
		updates["error_message"] = account.GetErrorMessage()
	} else if account.GetErrorMessage() != "" {
		updates["error_message"] = account.GetErrorMessage()
	}
	if value := strings.TrimSpace(account.GetSessionToken()); value != "" {
		updates["session_token"] = value
	}
	if value := strings.TrimSpace(account.GetAccessToken()); value != "" {
		updates["access_token"] = value
	}
	if value := strings.TrimSpace(account.GetChargeRef()); value != "" {
		updates["charge_ref"] = value
	}
	if value := strings.TrimSpace(account.GetFirstName()); value != "" {
		updates["first_name"] = value
	}
	if value := strings.TrimSpace(account.GetLastName()); value != "" {
		updates["last_name"] = value
	}
	if value := strings.TrimSpace(account.GetDob()); value != "" {
		updates["dob"] = value
	}
	if account.PlusTrialEligible != nil {
		updates["plus_trial_eligible"] = account.GetPlusTrialEligible()
	}
	if account.PlusActive != nil {
		updates["plus_active"] = account.GetPlusActive()
	}
	if value := normalizeTier(account.GetTier()); value != "" {
		updates["tier"] = value
	}
	return updates
}

func accountToProto(account *db.Account) *pb.Account {
	if account == nil {
		return nil
	}
	return &pb.Account{
		AccountId:         account.ID,
		Email:             account.Email,
		Password:          account.Password,
		Status:            account.Status,
		ErrorMessage:      account.ErrorMessage,
		SessionToken:      account.SessionToken,
		AccessToken:       account.AccessToken,
		ChargeRef:         account.ChargeRef,
		FirstName:         account.FirstName,
		LastName:          account.LastName,
		Dob:               account.DOB,
		CreatedAt:         account.CreatedAt,
		UpdatedAt:         account.UpdatedAt,
		PlusTrialEligible: account.PlusTrialEligible,
		PlusActive:        account.PlusActive,
		Tier:              account.Tier,
	}
}

func envDefault(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func randomDOB(now time.Time) string {
	earliest := now.AddDate(-23, 0, 1)
	latest := now.AddDate(-18, 0, 0)
	return gofakeit.DateRange(earliest, latest).Format("2006-01-02")
}

func redactEmail(email string) string {
	email = strings.TrimSpace(email)
	parts := strings.Split(email, "@")
	if len(parts) != 2 || parts[0] == "" {
		return "<redacted>"
	}
	local := parts[0]
	if len(local) > 2 {
		local = local[:2]
	}
	return local + "***@" + parts[1]
}

func normalizeEmail(email string) string {
	return strings.ToLower(strings.TrimSpace(email))
}

func normalizeTier(tier string) string {
	return strings.ToLower(strings.TrimSpace(tier))
}

func main() {
	log.Println("Initializing Account DB Service...")
	gofakeit.Seed(time.Now().UnixNano())
	database := db.InitDB()

	listenAddr := envDefault("LISTEN_ADDR", ":50051")
	lis, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	grpcServer := grpc.NewServer()
	pb.RegisterAccountDatabaseServiceServer(grpcServer, &accountDatabaseServer{db: database})

	log.Printf("Account DB gRPC Server listening on %s", listenAddr)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}
