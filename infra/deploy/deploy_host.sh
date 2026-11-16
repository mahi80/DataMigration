#!/usr/bin/env bash
# Runs as root through AWS SSM / Azure Run Command on ONE preprovisioned VM.
# State lives on local ext4/xfs backed by encrypted EBS/managed disk, never NFS/SMB.
set -Eeuo pipefail
set +x
umask 077

die() { echo "Deployment stopped: $*" >&2; exit 1; }
image=""; provider=""; region=""; acr_name=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image="${2:?}"; shift 2 ;;
    --provider) provider="${2:?}"; shift 2 ;;
    --region) region="${2:?}"; shift 2 ;;
    --acr-name) acr_name="${2:?}"; shift 2 ;;
    *) die "Unknown deployment argument" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "Run through the approved root host-management channel"
[[ "$image" =~ ^[a-z0-9][a-z0-9./_-]+@sha256:[a-f0-9]{64}$ ]] || die "A digest-pinned registry image is required"
[[ "$provider" == aws || "$provider" == azure ]] || die "Provider must be aws or azure"
for binary in docker curl python3 flock findmnt stat; do command -v "$binary" >/dev/null || die "Missing host prerequisite: $binary"; done

deploy_root=/var/lib/datamigration
state_dir="$deploy_root/state"
env_file=/etc/datamigration/runtime.env
current=datamigration
previous=datamigration-previous
[[ -f "$env_file" ]] || die "Provision the protected runtime environment file first"
[[ "$(stat -c '%u' "$env_file")" == 0 ]] || die "Runtime environment file must be owned by root"
[[ "$(stat -c '%a' "$env_file")" == 600 ]] || die "Runtime environment file must have mode 0600"
install -d -m 0700 "$deploy_root"
install -d -m 0700 -o 10001 -g 10001 "$state_dir"
filesystem="$(findmnt -n -o FSTYPE --target "$state_dir")"
[[ "$filesystem" == ext4 || "$filesystem" == xfs ]] || die "SQLite state requires a local ext4/xfs disk; network filesystems are unsupported"
exec 9>"$deploy_root/deployment.lock"
flock -n 9 || die "Another deployment owns the host lock"

registry="${image%%/*}"
auth_dir="$(mktemp -d /run/datamigration-docker.XXXXXX)"
export DOCKER_CONFIG="$auth_dir"
cleanup_auth() { rm -rf -- "$auth_dir"; }
trap cleanup_auth EXIT
if [[ "$provider" == aws ]]; then
  [[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || die "Invalid AWS region"
  [[ "$registry" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$ ]] || die "Expected an ECR registry"
  [[ "$registry" == *".ecr.$region.amazonaws.com" || "$registry" == *".ecr.$region.amazonaws.com.cn" ]] || die "ECR region differs from the approved AWS region"
  command -v aws >/dev/null || die "AWS CLI is required on the EC2 host"
  aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry" >/dev/null
else
  [[ "$acr_name" =~ ^[a-zA-Z0-9]{5,50}$ ]] || die "Invalid ACR name"
  [[ "$registry" == "${acr_name,,}.azurecr.io" ]] || die "Image registry differs from approved ACR"
  command -v az >/dev/null || die "Azure CLI is required on the VM"
  export AZURE_CONFIG_DIR="$auth_dir/azure"
  az login --identity --output none
  az acr login --name "$acr_name" --output none
fi
docker pull "$image" >/dev/null

has_current=false
if docker container inspect "$current" >/dev/null 2>&1; then
  [[ "$(docker inspect -f '{{.State.Running}}' "$current")" == true ]] || die "Existing service is stopped; investigate before deployment"
  has_current=true
fi
maintenance_set=false
old_stopped=false
old_moved=false
opened_gate=false
promotion_committed=false

healthy() {
  curl --fail --silent --max-time 5 http://127.0.0.1:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" else 1)'
}
drained() {
  curl --fail --silent --max-time 5 http://127.0.0.1:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("draining") is True and type(d.get("active_runs")) is int and d["active_runs"]==0 else 1)'
}
accepting_work() {
  curl --fail --silent --max-time 5 http://127.0.0.1:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("draining") is False else 1)'
}
wait_healthy() {
  for attempt in $(seq 1 30); do
    if healthy; then return 0; fi
    sleep 2
  done
  return 1
}
on_exit() {
  result=$?
  trap - EXIT
  set +e
  if [[ "$result" -ne 0 && "$promotion_committed" == false ]]; then
    if [[ "$opened_gate" == true ]]; then
      # Once unpause is attempted, its outcome may be unknown and new work may
      # have started. Never stop this container automatically after that point.
      echo "Unpause outcome is uncertain; keep the current service and inspect its health/maintenance state." >&2
    else
      echo "Deployment failed; preserving state and recovering the prior service when available." >&2
      recovered=false
      if [[ "$old_moved" == true ]]; then
        docker rm -f "$current" >/dev/null 2>&1
        if docker rename "$previous" "$current" && docker start "$current" >/dev/null; then recovered=true; fi
      elif [[ "$old_stopped" == true ]]; then
        if docker start "$current" >/dev/null; then recovered=true; fi
      elif [[ "$has_current" == false && "$maintenance_set" == true ]]; then
        docker rm -f "$current" >/dev/null 2>&1
      elif [[ "$has_current" == true ]]; then
        recovered=true
      fi
      if [[ "$has_current" == true && "$maintenance_set" == true ]]; then
        if [[ "$recovered" == true ]] && wait_healthy; then
          docker exec "$current" datamigrate maintenance off --workspace /state >/dev/null
          echo "Previous service retained; data and checkpoints were not reverted." >&2
        else
          echo "Previous service did not recover; maintenance remains enabled. Inspect host logs." >&2
        fi
      fi
    fi
  fi
  cleanup_auth
  exit "$result"
}
trap on_exit EXIT

if docker container inspect "$previous" >/dev/null 2>&1; then
  [[ "$(docker inspect -f '{{.State.Running}}' "$previous")" == false ]] || die "Unexpected second service is running"
  docker rm "$previous" >/dev/null
fi

if [[ "$has_current" == true ]]; then
  accepting_work || die "Existing service is unhealthy or already in maintenance; investigate first"
  maintenance_set=true
  docker exec "$current" datamigrate maintenance on --workspace /state >/dev/null
  idle=false
  for attempt in $(seq 1 120); do
    if drained; then idle=true; break; fi
    sleep 5
  done
  if [[ "$idle" != true ]]; then
    echo "Active work did not drain in ten minutes; the running service will be kept." >&2
    false
  fi
else
  # Establish the gate before the first API container can accept work.
  docker run --rm --user 10001:10001 --env-file "$env_file" \
    -e MIGRATION_STATE_DIR=/state --mount "type=bind,src=$state_dir,dst=/state" \
    --entrypoint datamigrate "$image" maintenance on --workspace /state >/dev/null
  maintenance_set=true
fi

if [[ "$has_current" == true ]]; then
  old_stopped=true
  docker stop --time 30 "$current" >/dev/null
  docker rename "$current" "$previous"
  old_moved=true
fi
docker run --detach --name "$current" --restart unless-stopped --user 10001:10001 \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 256 --memory 2g --cpus 2 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --publish 127.0.0.1:8000:8000 --env-file "$env_file" -e MIGRATION_STATE_DIR=/state \
  --mount "type=bind,src=$state_dir,dst=/state" "$image" >/dev/null
wait_healthy
drained
python3 - "$image" "$deploy_root/deployment.json" <<'PY'
import datetime,json,pathlib,sys
path=pathlib.Path(sys.argv[2]); temporary=path.with_suffix(".pending.json")
temporary.write_text(json.dumps({"image":sys.argv[1],"prepared_at":datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2)+"\n")
temporary.replace(path)
PY
# This is the final operation that changes the service. An uncertain response
# must not trigger automatic rollback while new migrations may be starting.
opened_gate=true
docker exec "$current" datamigrate maintenance off --workspace /state >/dev/null
promotion_committed=true
echo "DATAMIGRATION_DEPLOY_OK=${image##*@}"
