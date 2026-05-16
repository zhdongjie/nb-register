#!/usr/bin/env bash
set -Eeuo pipefail

REMOTE_HOST=${REMOTE_HOST:-pood1e@192.168.0.126}
REMOTE_DIR=${REMOTE_DIR:-/tmp/nb-register-build-src}
REMOTE_KUBECONFIG=${REMOTE_KUBECONFIG:-/tmp/self-hosted-business-kubeconfigs/nb-register-business.yaml}
REMOTE_HELM=${REMOTE_HELM:-/home/pood1e/.local/bin/helm}
RELEASE=${RELEASE:-nb-register}
NAMESPACE=${NAMESPACE:-nb-register}
if [[ -z "${CHART_DIR+x}" ]]; then
  CHART_DIR=$REMOTE_DIR/iac/helm/nb-register
  CHART_DIR_DEFAULTED=true
else
  CHART_DIR_DEFAULTED=false
fi
VALUES_FILE=${VALUES_FILE:-/tmp/nb-register-deploy-values-live.yaml}
IMAGE_PREFIX=${IMAGE_PREFIX:-nb-register}
TAG=${TAG:-deploy-$(date +%Y%m%d%H%M%S)}
HELM_TIMEOUT=${HELM_TIMEOUT:-10m}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-5m}

IMPORT_METHOD=${IMPORT_METHOD:-auto}
VM_NAME=${VM_NAME:-nb-register-business-1}
IMPORT_HOST_IP=${IMPORT_HOST_IP:-192.168.122.1}
IMPORT_HTTP_BIND=${IMPORT_HTTP_BIND:-$IMPORT_HOST_IP}
IMPORT_HTTP_PORT=${IMPORT_HTTP_PORT:-31888}
IMPORT_TIMEOUT_SECONDS=${IMPORT_TIMEOUT_SECONDS:-600}

BUILD_CAMOUFOX_BASE=${BUILD_CAMOUFOX_BASE:-auto}
SKIP_SYNC=${SKIP_SYNC:-false}
SKIP_BUILD=${SKIP_BUILD:-false}
SKIP_IMPORT=${SKIP_IMPORT:-false}
SKIP_HELM=${SKIP_HELM:-false}
SKIP_VALIDATE=${SKIP_VALIDATE:-false}
KEEP_REMOTE_TAR=${KEEP_REMOTE_TAR:-false}

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
)

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy-remote.sh [options] <service...|all>

Examples:
  scripts/deploy-remote.sh gopay-app dashboard
  scripts/deploy-remote.sh orchestrator gopay-app gopay-payment
  scripts/deploy-remote.sh --tag deploy-test-1 dashboard
  scripts/deploy-remote.sh all

Options:
  --tag TAG                 Image tag to build and deploy.
  --remote HOST             SSH target. Default: pood1e@192.168.0.126
  --remote-dir DIR          Remote source/build directory.
  --chart-dir DIR           Remote Helm chart directory.
  --values FILE             Remote Helm values file used for install/upgrade.
  --release NAME            Helm release. Default: nb-register
  --namespace NAME          Kubernetes namespace. Default: nb-register
  --skip-sync               Do not rsync this workspace to the remote host.
  --skip-build              Do not docker build images.
  --skip-import             Do not import images into the k3s node.
  --skip-helm               Do not run Helm upgrade.
  --skip-validate           Do not run helm lint/template before upgrade.
  --build-camoufox-base MODE  auto|always|never. Default: auto.
  -h, --help                Show this help.

Environment overrides:
  REMOTE_KUBECONFIG, REMOTE_HELM, IMAGE_PREFIX, IMPORT_METHOD, VM_NAME,
  IMPORT_HOST_IP, IMPORT_HTTP_BIND, IMPORT_HTTP_PORT, HELM_TIMEOUT,
  ROLLOUT_TIMEOUT, KEEP_REMOTE_TAR.
EOF
}

log() {
  printf '[deploy] %s\n' "$*"
}

die() {
  printf '[deploy] error: %s\n' "$*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

remote() {
  ssh -o ConnectTimeout=5 "$REMOTE_HOST" "$@"
}

valid_service() {
  case "$1" in
    account-db|browser-reg|dashboard|gopay-app|gopay-payment|herosms-sms-service|orchestrator|outlook-imap-service|outlook-register-service|whatsapp-otp-relay)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

needs_camoufox_base() {
  case "$1" in
    browser-reg|outlook-register-service)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

docker_context() {
  case "$1" in
    gopay-payment)
      printf '.'
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

dockerfile_path() {
  case "$1" in
    gopay-payment)
      printf 'gopay-payment/Dockerfile'
      ;;
    *)
      printf '%s/Dockerfile' "$1"
      ;;
  esac
}

image_ref() {
  printf '%s/%s:%s' "$IMAGE_PREFIX" "$1" "$TAG"
}

parse_args() {
  SERVICES=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tag)
        [[ $# -ge 2 ]] || die "--tag requires a value"
        TAG=$2
        shift 2
        ;;
      --remote)
        [[ $# -ge 2 ]] || die "--remote requires a value"
        REMOTE_HOST=$2
        shift 2
        ;;
      --remote-dir)
        [[ $# -ge 2 ]] || die "--remote-dir requires a value"
        REMOTE_DIR=$2
        if [[ "$CHART_DIR_DEFAULTED" == "true" ]]; then
          CHART_DIR=$REMOTE_DIR/iac/helm/nb-register
        fi
        shift 2
        ;;
      --chart-dir)
        [[ $# -ge 2 ]] || die "--chart-dir requires a value"
        CHART_DIR=$2
        CHART_DIR_DEFAULTED=false
        shift 2
        ;;
      --values)
        [[ $# -ge 2 ]] || die "--values requires a value"
        VALUES_FILE=$2
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
      --skip-sync)
        SKIP_SYNC=true
        shift
        ;;
      --skip-build)
        SKIP_BUILD=true
        shift
        ;;
      --skip-import)
        SKIP_IMPORT=true
        shift
        ;;
      --skip-helm)
        SKIP_HELM=true
        shift
        ;;
      --skip-validate)
        SKIP_VALIDATE=true
        shift
        ;;
      --build-camoufox-base)
        [[ $# -ge 2 ]] || die "--build-camoufox-base requires auto|always|never"
        BUILD_CAMOUFOX_BASE=$2
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

  case "$BUILD_CAMOUFOX_BASE" in
    auto|always|never) ;;
    *) die "--build-camoufox-base must be auto, always, or never" ;;
  esac

  case "$IMPORT_METHOD" in
    auto|sudo|qga) ;;
    *) die "IMPORT_METHOD must be auto, sudo, or qga" ;;
  esac

  case "$TAG" in
    ""|*[!A-Za-z0-9_.-]*)
      die "invalid Docker tag: $TAG"
      ;;
  esac

  if [[ ${#SERVICES[@]} -eq 0 ]]; then
    usage
    die "service list is required; use all to deploy every workload"
  fi

  if [[ ${#SERVICES[@]} -eq 1 && ${SERVICES[0]} == "all" ]]; then
    SERVICES=("${ALL_SERVICES[@]}")
  fi

  for service in "${SERVICES[@]}"; do
    valid_service "$service" || die "unknown service: $service"
  done
}

sync_source() {
  if [[ "$SKIP_SYNC" == "true" ]]; then
    log "skip sync"
    return
  fi

  log "sync source to $REMOTE_HOST:$REMOTE_DIR"
  remote "mkdir -p $(shell_quote "$REMOTE_DIR")"
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.codex/' \
    --exclude '.env' \
    --exclude '.temp' \
    --exclude '.tmp*/' \
    --exclude '.venv*/' \
    --exclude '**/__pycache__/' \
    --exclude '**/.pytest_cache/' \
    --exclude '**/node_modules/' \
    --exclude '**/dist/' \
    --exclude '**/build/' \
    --exclude '**/*.log' \
    --exclude 'gopay-payment/gopay-flow/config.json' \
    ./ "$REMOTE_HOST:$REMOTE_DIR/"
}

build_camoufox_base_if_needed() {
  if [[ "$SKIP_BUILD" == "true" ]]; then
    return
  fi

  local should_build=false
  if [[ "$BUILD_CAMOUFOX_BASE" == "always" ]]; then
    should_build=true
  elif [[ "$BUILD_CAMOUFOX_BASE" == "auto" ]]; then
    for service in "${SERVICES[@]}"; do
      if needs_camoufox_base "$service"; then
        should_build=true
        break
      fi
    done
  fi

  if [[ "$should_build" != "true" ]]; then
    return
  fi

  log "build nb-register-camoufox-base:latest"
  remote "cd $(shell_quote "$REMOTE_DIR") && docker build -t nb-register-camoufox-base:latest -f docker/camoufox-base/Dockerfile docker/camoufox-base"
}

build_images() {
  if [[ "$SKIP_BUILD" == "true" ]]; then
    log "skip build"
    return
  fi

  build_camoufox_base_if_needed

  local service context dockerfile image
  for service in "${SERVICES[@]}"; do
    context=$(docker_context "$service")
    dockerfile=$(dockerfile_path "$service")
    image=$(image_ref "$service")
    log "build $image"
    remote "cd $(shell_quote "$REMOTE_DIR") && docker build -t $(shell_quote "$image") -f $(shell_quote "$dockerfile") $(shell_quote "$context")"
  done
}

save_images() {
  REMOTE_TAR=/tmp/nb-register-images-${TAG}.tar
  if [[ "$SKIP_BUILD" == "true" || "$SKIP_IMPORT" == "true" ]]; then
    return
  fi

  local images=()
  local service
  for service in "${SERVICES[@]}"; do
    images+=("$(image_ref "$service")")
  done

  local quoted_images=""
  for image in "${images[@]}"; do
    quoted_images+=" $(shell_quote "$image")"
  done

  log "save image tar on remote: $REMOTE_TAR"
  remote "docker save -o $(shell_quote "$REMOTE_TAR")$quoted_images"
}

import_images_qga() {
  local tar_path=$1
  log "import image tar into k3s via qemu guest agent"
  ssh -o ConnectTimeout=5 "$REMOTE_HOST" \
    "TAR_PATH=$(shell_quote "$tar_path") VM_NAME=$(shell_quote "$VM_NAME") IMPORT_HOST_IP=$(shell_quote "$IMPORT_HOST_IP") IMPORT_HTTP_BIND=$(shell_quote "$IMPORT_HTTP_BIND") IMPORT_HTTP_PORT=$(shell_quote "$IMPORT_HTTP_PORT") IMPORT_TIMEOUT_SECONDS=$(shell_quote "$IMPORT_TIMEOUT_SECONDS") bash -s" <<'REMOTE_SCRIPT'
set -Eeuo pipefail

die() {
  printf '[deploy] error: %s\n' "$*" >&2
  exit 1
}

command -v virsh >/dev/null 2>&1 || die "virsh is not available on remote host"
command -v python3 >/dev/null 2>&1 || die "python3 is not available on remote host"

tar_dir=$(dirname "$TAR_PATH")
tar_name=$(basename "$TAR_PATH")
url="http://${IMPORT_HOST_IP}:${IMPORT_HTTP_PORT}/${tar_name}"
pidfile="/tmp/nb-register-import-http-${IMPORT_HTTP_PORT}.pid"
logfile="/tmp/nb-register-import-http-${IMPORT_HTTP_PORT}.log"

cleanup() {
  if [[ -f "$pidfile" ]]; then
    old_pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ -n "${old_pid:-}" ]]; then
      kill "$old_pid" >/dev/null 2>&1 || true
    fi
    rm -f "$pidfile"
  fi
}
trap cleanup EXIT

cleanup
(
  cd "$tar_dir"
  python3 -m http.server "$IMPORT_HTTP_PORT" --bind "$IMPORT_HTTP_BIND" >"$logfile" 2>&1 &
  echo $! >"$pidfile"
)
sleep 1
server_pid=$(cat "$pidfile")
if ! kill -0 "$server_pid" >/dev/null 2>&1; then
  cat "$logfile" >&2 || true
  die "failed to start temporary HTTP server on ${IMPORT_HTTP_BIND}:${IMPORT_HTTP_PORT}"
fi

guest_cmd="set -e; rm -f /tmp/${tar_name}; curl -fsSL -o /tmp/${tar_name} ${url}; k3s ctr -n k8s.io images import /tmp/${tar_name}; rm -f /tmp/${tar_name}"
payload=$(GUEST_CMD="$guest_cmd" python3 - <<'PY'
import json
import os

print(json.dumps({
    "execute": "guest-exec",
    "arguments": {
        "path": "/bin/sh",
        "arg": ["-lc", os.environ["GUEST_CMD"]],
        "capture-output": True,
    },
}))
PY
)

start_json=$(virsh qemu-agent-command "$VM_NAME" "$payload")
guest_pid=$(printf '%s' "$start_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["return"]["pid"])')

for _ in $(seq 1 "$IMPORT_TIMEOUT_SECONDS"); do
  status_payload=$(PID="$guest_pid" python3 - <<'PY'
import json
import os

print(json.dumps({
    "execute": "guest-exec-status",
    "arguments": {"pid": int(os.environ["PID"])},
}))
PY
)
  status_json=$(virsh qemu-agent-command "$VM_NAME" "$status_payload")
  exited=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print("1" if json.load(sys.stdin)["return"].get("exited") else "0")')
  if [[ "$exited" != "1" ]]; then
    sleep 1
    continue
  fi

  STATUS_JSON="$status_json" python3 - <<'PY'
import base64
import json
import os
import sys

status = json.loads(os.environ["STATUS_JSON"])["return"]
for key, stream in (("out-data", sys.stdout), ("err-data", sys.stderr)):
    value = status.get(key)
    if value:
        stream.write(base64.b64decode(value).decode("utf-8", "replace"))
PY
  exit_code=$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["return"].get("exitcode", 1))')
  if [[ "$exit_code" != "0" ]]; then
    die "guest image import failed with exit code $exit_code"
  fi
  exit 0
done

die "guest image import timed out after ${IMPORT_TIMEOUT_SECONDS}s"
REMOTE_SCRIPT
}

import_images() {
  if [[ "$SKIP_IMPORT" == "true" ]]; then
    log "skip import"
    return
  fi

  if [[ "$SKIP_BUILD" == "true" ]]; then
    die "--skip-build cannot be combined with image import unless images were already saved by this script"
  fi

  case "$IMPORT_METHOD" in
    sudo)
      log "import image tar into k3s via sudo k3s ctr"
      remote "sudo -n k3s ctr -n k8s.io images import $(shell_quote "$REMOTE_TAR")"
      ;;
    qga)
      import_images_qga "$REMOTE_TAR"
      ;;
    auto)
      log "try sudo k3s ctr image import"
      if remote "sudo -n k3s ctr -n k8s.io images import $(shell_quote "$REMOTE_TAR")"; then
        return
      fi
      log "sudo import unavailable; fallback to qemu guest agent"
      import_images_qga "$REMOTE_TAR"
      ;;
  esac
}

write_overlay() {
  OVERLAY_FILE=/tmp/nb-register-deploy-overlay-${TAG}.yaml
  local tmp
  tmp=$(mktemp)
  {
    printf 'workloads:\n'
    local service
    for service in "${SERVICES[@]}"; do
      printf '  %s:\n' "$service"
      printf '    image:\n'
      printf '      repository: %s/%s\n' "$IMAGE_PREFIX" "$service"
      printf '      tag: %s\n' "$TAG"
    done
  } >"$tmp"

  log "write Helm overlay: $OVERLAY_FILE"
  ssh -o ConnectTimeout=5 "$REMOTE_HOST" "cat > $(shell_quote "$OVERLAY_FILE")" <"$tmp"
  rm -f "$tmp"
}

helm_values_flags() {
  if remote "test -f $(shell_quote "$VALUES_FILE")"; then
    printf '%s' "-f $(shell_quote "$VALUES_FILE") -f $(shell_quote "$OVERLAY_FILE")"
  else
    printf '%s' "--reset-values -f $(shell_quote "$OVERLAY_FILE")"
  fi
}

helm_render_values_flags() {
  if remote "test -f $(shell_quote "$VALUES_FILE")"; then
    printf '%s' "-f $(shell_quote "$VALUES_FILE") -f $(shell_quote "$OVERLAY_FILE")"
  else
    printf '%s' "-f $(shell_quote "$OVERLAY_FILE")"
  fi
}

persist_live_values() {
  if [[ "$SKIP_HELM" == "true" ]]; then
    return
  fi
  if ! remote "test -f $(shell_quote "$VALUES_FILE")"; then
    return
  fi

  local services_csv
  services_csv=$(IFS=,; printf '%s' "${SERVICES[*]}")
  log "persist image tags into Helm values: $VALUES_FILE"
  remote "VALUES_FILE=$(shell_quote "$VALUES_FILE") IMAGE_PREFIX=$(shell_quote "$IMAGE_PREFIX") TAG=$(shell_quote "$TAG") DEPLOY_SERVICES=$(shell_quote "$services_csv") python3 - <<'PY'
import os
import shutil
import time

import yaml

path = os.environ['VALUES_FILE']
image_prefix = os.environ['IMAGE_PREFIX'].rstrip('/')
tag = os.environ['TAG']
services = [item for item in os.environ['DEPLOY_SERVICES'].split(',') if item]

with open(path, 'r', encoding='utf-8') as handle:
    values = yaml.safe_load(handle) or {}

workloads = values.setdefault('workloads', {})

for service in services:
    image = workloads.setdefault(service, {}).setdefault('image', {})
    image['repository'] = f'{image_prefix}/{service}'
    image['tag'] = tag

backup = f'{path}.bak-{int(time.time())}'
shutil.copy2(path, backup)
tmp = f'{path}.tmp'
with open(tmp, 'w', encoding='utf-8') as handle:
    yaml.safe_dump(values, handle, sort_keys=False, allow_unicode=True)
os.replace(tmp, path)
print(f'updated {path}; backup {backup}')
PY"
}

validate_chart() {
  if [[ "$SKIP_VALIDATE" == "true" || "$SKIP_HELM" == "true" ]]; then
    return
  fi

  local flags rendered
  flags=$(helm_render_values_flags)
  rendered="/tmp/${RELEASE}-${TAG}.rendered.yaml"
  log "helm lint/template"
  remote "KUBECONFIG=$(shell_quote "$REMOTE_KUBECONFIG") $(shell_quote "$REMOTE_HELM") lint $(shell_quote "$CHART_DIR") $flags"
  remote "KUBECONFIG=$(shell_quote "$REMOTE_KUBECONFIG") $(shell_quote "$REMOTE_HELM") template $(shell_quote "$RELEASE") $(shell_quote "$CHART_DIR") --namespace $(shell_quote "$NAMESPACE") $flags > $(shell_quote "$rendered")"
}

helm_upgrade() {
  if [[ "$SKIP_HELM" == "true" ]]; then
    log "skip helm"
    return
  fi

  local flags
  flags=$(helm_values_flags)
  log "helm upgrade release=$RELEASE namespace=$NAMESPACE tag=$TAG"
  remote "KUBECONFIG=$(shell_quote "$REMOTE_KUBECONFIG") $(shell_quote "$REMOTE_HELM") upgrade --install $(shell_quote "$RELEASE") $(shell_quote "$CHART_DIR") --namespace $(shell_quote "$NAMESPACE") --create-namespace --server-side=false --rollback-on-failure --wait=watcher --wait-for-jobs --timeout $(shell_quote "$HELM_TIMEOUT") $flags"
}

verify_rollout() {
  if [[ "$SKIP_HELM" == "true" ]]; then
    return
  fi

  local service deploy_name
  for service in "${SERVICES[@]}"; do
    deploy_name="${RELEASE}-${service}"
    log "verify rollout $deploy_name"
    remote "kubectl --kubeconfig $(shell_quote "$REMOTE_KUBECONFIG") -n $(shell_quote "$NAMESPACE") rollout status deploy/$(shell_quote "$deploy_name") --timeout=$(shell_quote "$ROLLOUT_TIMEOUT")"
  done

  log "current images"
  remote "kubectl --kubeconfig $(shell_quote "$REMOTE_KUBECONFIG") -n $(shell_quote "$NAMESPACE") get deploy -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image,READY:.status.readyReplicas,UPDATED:.status.updatedReplicas,AVAILABLE:.status.availableReplicas"
}

cleanup_remote_tar() {
  if [[ "${REMOTE_TAR:-}" == "" || "$KEEP_REMOTE_TAR" == "true" || "$SKIP_BUILD" == "true" || "$SKIP_IMPORT" == "true" ]]; then
    return
  fi
  remote "rm -f $(shell_quote "$REMOTE_TAR")" || true
}

main() {
  parse_args "$@"

  log "services: ${SERVICES[*]}"
  log "tag: $TAG"
  sync_source
  build_images
  save_images
  import_images
  write_overlay
  validate_chart
  helm_upgrade
  verify_rollout
  persist_live_values
  cleanup_remote_tar
  log "done"
}

main "$@"
