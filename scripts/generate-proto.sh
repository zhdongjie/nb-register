#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gen_go_root() {
  local service="$1"
  shift
  mkdir -p "${ROOT}/${service}/pb"
  rm -f "${ROOT}/${service}/pb"/*.pb.go "${ROOT}/${service}/pb"/*_grpc.pb.go
  protoc -I "${ROOT}/proto" \
    --go_out="${ROOT}/${service}/pb" \
    --go-grpc_out="${ROOT}/${service}/pb" \
    "$@"
}

gen_py_root() {
  local service="$1"
  shift
  python3 -m grpc_tools.protoc -I "${ROOT}/proto" \
    --python_out="${ROOT}/${service}" \
    --grpc_python_out="${ROOT}/${service}" \
    "$@"
}

gen_ts_dashboard() {
  local out_dir="${ROOT}/dashboard/web/src/proto"
  local plugin="${ROOT}/dashboard/web/node_modules/.bin/protoc-gen-ts_proto"
  if [[ ! -x "$plugin" ]]; then
    printf 'ts-proto plugin not found at %s; run npm install in dashboard/web\n' "$plugin" >&2
    return 1
  fi
  rm -rf "$out_dir"
  mkdir -p "$out_dir"
  protoc -I "${ROOT}/proto" \
    --plugin="protoc-gen-ts_proto=${plugin}" \
    --ts_proto_out="$out_dir" \
    --ts_proto_opt=onlyTypes=true,outputServices=none,esModuleInterop=true,useJsonWireFormat=true,snakeToCamel=false \
    "$(root_proto account_db.proto)" \
    "$(root_proto email.proto)" \
    "$(root_proto gopay_app.proto)" \
    "$(root_proto mailbox_register.proto)" \
    "$(root_proto payment.proto)" \
    "${orchestrator_protos[@]}"
}

root_proto() {
  printf '%s/proto/%s\n' "$ROOT" "$1"
}

orchestrator_protos=("${ROOT}"/proto/orchestrator*.proto)

gen_go_root account-db "$(root_proto account_db.proto)"
gen_go_root orchestrator "${ROOT}"/proto/*.proto
gen_go_root dashboard \
  "$(root_proto account_db.proto)" \
  "$(root_proto email.proto)" \
  "$(root_proto gopay_app.proto)" \
  "$(root_proto mailbox_register.proto)" \
  "$(root_proto payment.proto)" \
  "${orchestrator_protos[@]}"
gen_go_root outlook-imap-service "$(root_proto email.proto)"
gen_go_root whatsapp-otp-relay "$(root_proto otp.proto)"

gen_py_root browser-reg "$(root_proto browser.proto)"
gen_py_root outlook-register-service "$(root_proto mailbox_register.proto)"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/gopay-payment/gopay-flow" \
  --grpc_python_out="${ROOT}/gopay-payment/gopay-flow" \
  "$(root_proto payment.proto)"

gen_py_root checkphone-tgbot \
  "$(root_proto email.proto)" \
  "$(root_proto gopay_app.proto)" \
  "${orchestrator_protos[@]}"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/gopay-app" \
  --grpc_python_out="${ROOT}/gopay-app" \
  "$(root_proto gopay_app.proto)"

python3 -m grpc_tools.protoc \
  -I "${ROOT}/proto" \
  --python_out="${ROOT}/herosms-sms-service" \
  --grpc_python_out="${ROOT}/herosms-sms-service" \
  "$(root_proto sms.proto)"

gen_ts_dashboard
