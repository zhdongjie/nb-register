#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gen_go() {
  local service="$1"
  mkdir -p "${ROOT}/${service}/pb"
  protoc -I "${ROOT}/${service}/proto" \
    --go_out="${ROOT}/${service}/pb" \
    --go-grpc_out="${ROOT}/${service}/pb" \
    "${ROOT}/${service}"/proto/*.proto
}

gen_py() {
  local service="$1"
  shift
  python3 -m grpc_tools.protoc -I "${ROOT}/${service}/proto" \
    --python_out="${ROOT}/${service}" \
    --grpc_python_out="${ROOT}/${service}" \
    "$@"
}

gen_go account-db
gen_go orchestrator
gen_go dashboard
gen_go outlook-imap-service
gen_go whatsapp-otp-relay

gen_py browser-reg "${ROOT}/browser-reg/proto/browser.proto"
gen_py outlook-register-service "${ROOT}/outlook-register-service/proto/mailbox_register.proto"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/gopay-payment/gopay-flow/proto" \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/gopay-payment/gopay-flow" \
  --grpc_python_out="${ROOT}/gopay-payment/gopay-flow" \
  "${ROOT}/gopay-payment/gopay-flow/proto/payment.proto" \
  "${ROOT}/proto/gopay_cycle.proto" \
  "${ROOT}/proto/account_db.proto"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/gopay-cycle" \
  --grpc_python_out="${ROOT}/gopay-cycle" \
  "${ROOT}/proto/gopay_cycle.proto" \
  "${ROOT}/proto/sms.proto"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/sms-service" \
  --grpc_python_out="${ROOT}/sms-service" \
  "${ROOT}/proto/sms.proto"
