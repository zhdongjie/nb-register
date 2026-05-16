package api

import (
	"context"
	"fmt"
	"strings"

	"gorm.io/gorm/clause"

	"orchestrator/db"
	"orchestrator/pb"
)

func (s *Server) GoPayUserSetWAPhone(ctx context.Context, req *pb.GoPayUserSetWAPhoneRequest) (*pb.GoPayUserWAPhoneResponse, error) {
	stateKey, err := normalizeGoPayUserStateKey(req.GetStateKey())
	if err != nil {
		return &pb.GoPayUserWAPhoneResponse{ErrorMessage: err.Error()}, nil
	}
	phone := normalizeIndonesiaPhoneForAPI(req.GetWaPhone())
	if phone == "" {
		return &pb.GoPayUserWAPhoneResponse{StateKey: stateKey, ErrorMessage: "wa_phone is required"}, nil
	}
	if s.db == nil {
		return &pb.GoPayUserWAPhoneResponse{StateKey: stateKey, ErrorMessage: "orchestrator db not configured"}, nil
	}
	err = s.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "state_key"}},
		DoUpdates: clause.AssignmentColumns([]string{"wa_phone", "updated_at"}),
	}).Create(&db.GoPayUserProfile{StateKey: stateKey, WAPhone: phone}).Error
	if err != nil {
		return &pb.GoPayUserWAPhoneResponse{StateKey: stateKey, ErrorMessage: fmt.Sprintf("save wa_phone: %v", err)}, nil
	}
	return &pb.GoPayUserWAPhoneResponse{Success: true, StateKey: stateKey, WaPhone: phone}, nil
}

func (s *Server) GoPayUserGetWAPhone(ctx context.Context, req *pb.GoPayUserGetWAPhoneRequest) (*pb.GoPayUserWAPhoneResponse, error) {
	stateKey, err := normalizeGoPayUserStateKey(req.GetStateKey())
	if err != nil {
		return &pb.GoPayUserWAPhoneResponse{ErrorMessage: err.Error()}, nil
	}
	if s.db == nil {
		return &pb.GoPayUserWAPhoneResponse{StateKey: stateKey, ErrorMessage: "orchestrator db not configured"}, nil
	}
	var profile db.GoPayUserProfile
	result := s.db.WithContext(ctx).Where("state_key = ?", stateKey).Limit(1).Find(&profile)
	if result.Error != nil {
		return &pb.GoPayUserWAPhoneResponse{StateKey: stateKey, ErrorMessage: fmt.Sprintf("load wa_phone: %v", result.Error)}, nil
	}
	if result.RowsAffected == 0 {
		return &pb.GoPayUserWAPhoneResponse{Success: true, StateKey: stateKey}, nil
	}
	return &pb.GoPayUserWAPhoneResponse{Success: true, StateKey: stateKey, WaPhone: normalizeIndonesiaPhoneForAPI(profile.WAPhone)}, nil
}

func normalizeIndonesiaPhoneForAPI(phone string) string {
	value := strings.TrimPrefix(strings.TrimSpace(phone), "+")
	if strings.HasPrefix(value, "62") {
		return strings.TrimPrefix(value[2:], "0")
	}
	return value
}
