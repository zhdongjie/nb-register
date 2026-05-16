#!/usr/bin/env bash
set -Eeuo pipefail

REMOTE_HOST=${REMOTE_HOST:-pood1e@192.168.0.126}
REMOTE_KUBECONFIG=${REMOTE_KUBECONFIG:-/tmp/self-hosted-business-kubeconfigs/nb-register-business.yaml}
RELEASE=${RELEASE:-nb-register}
NAMESPACE=${NAMESPACE:-nb-register}

TAIL=${TAIL:-200}
SINCE=${SINCE:-}
SINCE_TIME=${SINCE_TIME:-}
FOLLOW=${FOLLOW:-false}
PREVIOUS=${PREVIOUS:-false}
TIMESTAMPS=${TIMESTAMPS:-false}
PREFIX=${PREFIX:-true}
ALL_CONTAINERS=${ALL_CONTAINERS:-false}
IGNORE_ERRORS=${IGNORE_ERRORS:-true}
MAX_LOG_REQUESTS=${MAX_LOG_REQUESTS:-50}
REQUEST_TIMEOUT=${REQUEST_TIMEOUT:-10s}
LIST_PODS=${LIST_PODS:-false}

ALL_SERVICES=(
  account-db
  browser-reg
  dashboard
  gopay-app
  gopay-payment
  herosms-sms-service
  orchestrator
  outlook-imap-service
  outlook-register-service
  whatsapp-otp-relay
  temporal
  postgres
)

usage() {
  cat <<'EOF'
Usage:
  scripts/logs-remote.sh [options] <service...|all>

Examples:
  scripts/logs-remote.sh dashboard
  scripts/logs-remote.sh -f orchestrator gopay-app
  scripts/logs-remote.sh --since 30m --tail 500 gopay-payment
  scripts/logs-remote.sh --previous browser-reg
  scripts/logs-remote.sh --list-pods all

Services:
  account-db browser-reg dashboard gopay-app gopay-payment
  herosms-sms-service orchestrator outlook-imap-service
  outlook-register-service whatsapp-otp-relay temporal postgres

Options:
  -f, --follow              Follow logs.
  --tail N                  Lines per pod. Default: 200.
  --since DURATION          Only logs newer than duration, e.g. 10m, 1h.
  --since-time TIME         Only logs after RFC3339 time.
  --previous                Show previous container logs.
  --timestamps              Include Kubernetes log timestamps.
  --no-prefix               Do not prefix lines with pod/container.
  --all-containers          Include all containers in matching pods.
  --list-pods               List matching pods instead of printing logs.
  --remote HOST             SSH target. Default: pood1e@192.168.0.126
  --kubeconfig FILE         Remote kubeconfig path.
  --release NAME            Helm release / instance label. Default: nb-register
  --namespace NAME          Kubernetes namespace. Default: nb-register
  -h, --help                Show this help.

Environment overrides:
  REMOTE_HOST, REMOTE_KUBECONFIG, RELEASE, NAMESPACE, TAIL, SINCE,
  SINCE_TIME, FOLLOW, PREVIOUS, TIMESTAMPS, PREFIX, ALL_CONTAINERS,
  IGNORE_ERRORS, MAX_LOG_REQUESTS, REQUEST_TIMEOUT.
EOF
}

log() {
  printf '[logs] %s\n' "$*" >&2
}

die() {
  printf '[logs] error: %s\n' "$*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

join_by_comma() {
  local IFS=,
  printf '%s' "$*"
}

valid_service() {
  case "$1" in
    account-db|browser-reg|dashboard|gopay-app|gopay-payment|herosms-sms-service|orchestrator|outlook-imap-service|outlook-register-service|whatsapp-otp-relay|temporal|postgres)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

parse_args() {
  SERVICES=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f|--follow)
        FOLLOW=true
        shift
        ;;
      --tail)
        [[ $# -ge 2 ]] || die "--tail requires a value"
        TAIL=$2
        shift 2
        ;;
      --since)
        [[ $# -ge 2 ]] || die "--since requires a value"
        SINCE=$2
        shift 2
        ;;
      --since-time)
        [[ $# -ge 2 ]] || die "--since-time requires a value"
        SINCE_TIME=$2
        shift 2
        ;;
      --previous)
        PREVIOUS=true
        shift
        ;;
      --timestamps)
        TIMESTAMPS=true
        shift
        ;;
      --no-prefix)
        PREFIX=false
        shift
        ;;
      --all-containers)
        ALL_CONTAINERS=true
        shift
        ;;
      --list-pods)
        LIST_PODS=true
        shift
        ;;
      --remote)
        [[ $# -ge 2 ]] || die "--remote requires a value"
        REMOTE_HOST=$2
        shift 2
        ;;
      --kubeconfig)
        [[ $# -ge 2 ]] || die "--kubeconfig requires a value"
        REMOTE_KUBECONFIG=$2
        shift 2
        ;;
      --release)
        [[ $# -ge 2 ]] || die "--release requires a value"
        RELEASE=$2
        shift 2
        ;;
      --namespace)
        [[ $# -ge 2 ]] || die "--namespace requires a value"
        NAMESPACE=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        while [[ $# -gt 0 ]]; do
          SERVICES+=("$1")
          shift
        done
        ;;
      -*)
        die "unknown option: $1"
        ;;
      *)
        SERVICES+=("$1")
        shift
        ;;
    esac
  done

  [[ "$TAIL" =~ ^[0-9]+$ ]] || die "--tail must be a non-negative integer"

  if [[ ${#SERVICES[@]} -eq 0 ]]; then
    usage
    die "service list is required; use all to view every service"
  fi

  if [[ ${#SERVICES[@]} -eq 1 && ${SERVICES[0]} == "all" ]]; then
    SERVICES=("${ALL_SERVICES[@]}")
  fi

  for service in "${SERVICES[@]}"; do
    valid_service "$service" || die "unknown service: $service"
  done
}

run_remote_logs() {
  local services_csv
  services_csv=$(join_by_comma "${SERVICES[@]}")

  log "remote=$REMOTE_HOST namespace=$NAMESPACE release=$RELEASE services=${SERVICES[*]}"

  ssh -o ConnectTimeout=5 "$REMOTE_HOST" \
    "REMOTE_KUBECONFIG=$(shell_quote "$REMOTE_KUBECONFIG") NAMESPACE=$(shell_quote "$NAMESPACE") RELEASE=$(shell_quote "$RELEASE") SERVICES_CSV=$(shell_quote "$services_csv") TAIL=$(shell_quote "$TAIL") SINCE=$(shell_quote "$SINCE") SINCE_TIME=$(shell_quote "$SINCE_TIME") FOLLOW=$(shell_quote "$FOLLOW") PREVIOUS=$(shell_quote "$PREVIOUS") TIMESTAMPS=$(shell_quote "$TIMESTAMPS") PREFIX=$(shell_quote "$PREFIX") ALL_CONTAINERS=$(shell_quote "$ALL_CONTAINERS") IGNORE_ERRORS=$(shell_quote "$IGNORE_ERRORS") MAX_LOG_REQUESTS=$(shell_quote "$MAX_LOG_REQUESTS") REQUEST_TIMEOUT=$(shell_quote "$REQUEST_TIMEOUT") LIST_PODS=$(shell_quote "$LIST_PODS") bash -s" <<'REMOTE_SCRIPT'
set -Eeuo pipefail

die() {
  printf '[logs] error: %s\n' "$*" >&2
  exit 1
}

IFS=, read -r -a services <<<"$SERVICES_CSV"
if [[ ${#services[@]} -eq 0 || -z "${services[0]:-}" ]]; then
  die "empty service list"
fi

if [[ ${#services[@]} -eq 1 ]]; then
  selector="app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=${services[0]}"
else
  selector="app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component in (${SERVICES_CSV})"
fi

kubectl_base=(
  kubectl
  --kubeconfig "$REMOTE_KUBECONFIG"
  --request-timeout "$REQUEST_TIMEOUT"
  -n "$NAMESPACE"
)

if [[ "$LIST_PODS" == "true" ]]; then
  "${kubectl_base[@]}" get pods -l "$selector" \
    -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount,AGE:.metadata.creationTimestamp,NODE:.spec.nodeName
  exit 0
fi

pod_count=$("${kubectl_base[@]}" get pods -l "$selector" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ "$pod_count" == "0" ]]; then
  die "no pods matched selector: $selector"
fi

logs_args=(
  kubectl
  --kubeconfig "$REMOTE_KUBECONFIG"
  -n "$NAMESPACE"
  logs
  -l "$selector"
  --tail "$TAIL"
  --max-log-requests "$MAX_LOG_REQUESTS"
  --ignore-errors="$IGNORE_ERRORS"
  --prefix="$PREFIX"
  --timestamps="$TIMESTAMPS"
)

if [[ "$ALL_CONTAINERS" == "true" ]]; then
  logs_args+=(--all-containers=true)
fi
if [[ -n "$SINCE" ]]; then
  logs_args+=(--since "$SINCE")
fi
if [[ -n "$SINCE_TIME" ]]; then
  logs_args+=(--since-time "$SINCE_TIME")
fi
if [[ "$PREVIOUS" == "true" ]]; then
  logs_args+=(--previous)
fi
if [[ "$FOLLOW" == "true" ]]; then
  logs_args+=(-f)
fi

"${logs_args[@]}"
REMOTE_SCRIPT
}

main() {
  parse_args "$@"
  run_remote_logs
}

main "$@"
