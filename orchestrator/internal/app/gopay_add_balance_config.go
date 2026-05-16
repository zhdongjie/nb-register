package app

import (
	"strings"

	"orchestrator/pb"
)

func defaultGoPayAddBalance(cfg orchestratorConfig) *pb.GoPayAddBalance {
	switch normalizeGoPayAddBalanceMode(cfg.GoPayAddBalanceMode) {
	case "envelope":
		return &pb.GoPayAddBalance{
			Method: &pb.GoPayAddBalance_Envelope{
				Envelope: &pb.GoPayEnvelopeAddBalance{
					Link: strings.TrimSpace(cfg.GoPayAddBalanceEnvelopeLink),
				},
			},
		}
	default:
		currency := strings.TrimSpace(cfg.GoPayAddBalanceTransferCurrency)
		if currency == "" {
			currency = "IDR"
		}
		return &pb.GoPayAddBalance{
			Method: &pb.GoPayAddBalance_ManualTransfer{
				ManualTransfer: &pb.GoPayManualTransferAddBalance{
					Instructions: strings.TrimSpace(cfg.GoPayAddBalanceTransferInstructions),
					Amount:       cfg.GoPayAddBalanceTransferAmountRp,
					Currency:     currency,
				},
			},
		}
	}
}

func normalizeGoPayAddBalanceMode(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "envelope", "claim_envelope", "red_packet", "红包":
		return "envelope"
	case "manual_transfer", "transfer", "qr", "qrcode":
		return "manual_transfer"
	default:
		return "manual_transfer"
	}
}
