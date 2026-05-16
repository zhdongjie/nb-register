package db

import (
	"log"
	"os"
	"strings"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
)

type Account struct {
	ID                string `gorm:"primaryKey"`
	Email             string `gorm:"uniqueIndex"`
	Password          string
	Status            string
	ErrorMessage      string
	SessionToken      string
	AccessToken       string
	ChargeRef         string
	FirstName         string
	LastName          string
	DOB               string // YYYY-MM-DD
	PlusTrialEligible *bool
	PlusActive        *bool
	Tier              string
	CreatedAt         int64 `gorm:"autoCreateTime"`
	UpdatedAt         int64 `gorm:"autoUpdateTime"`
}

func InitDB() *gorm.DB {
	dsn := strings.TrimSpace(os.Getenv("PG_DSN"))
	if dsn == "" {
		log.Fatal("PG_DSN is required")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
	if err != nil {
		log.Fatalf("failed to connect to PostgreSQL database: %v", err)
	}

	db.AutoMigrate(&Account{})
	if err := db.Exec("ALTER TABLE accounts DROP COLUMN IF EXISTS proxy_url").Error; err != nil {
		log.Printf("failed to drop legacy proxy_url column: %v", err)
	}

	return db
}
