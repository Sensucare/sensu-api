#!/usr/bin/env bash
# Rebuild and redeploy the sensu-api backend from source.
# Runs on the Sensu VPS as ssm-user. Uses sudo for docker.
#
# Usage:
#   ./redeploy.sh              build from source, deploy, verify
#   ./redeploy.sh --no-build   redeploy the existing sensu-api:local image
#   ./redeploy.sh --tag NAME   build with an explicit tag (default: v<timestamp>)
#   ./redeploy.sh --help

set -euo pipefail

SRC=/home/ssm-user/project/sensu-api
COMPOSE_DIR=/opt/sensu
IMAGE=sensu-api
SERVICE=api              # docker compose service key
CONTAINER=sensu-api      # container_name in compose
HEALTH_URL=https://api.sensu.com.mx/api/health

log()  { echo "[$(date +%H:%M:%S)] $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }

usage() {
  sed -n '2,10p' "$0"
}

BUILD=1
TAG="v$(date +%Y%m%d-%H%M%S)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    --tag)      TAG="${2:-}"; [[ -n "$TAG" ]] || die "--tag requires a value"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "Unknown argument: $1 (try --help)" ;;
  esac
done

# ───── Pre-flight ─────────────────────────────────────────────
[[ -d "$SRC"              ]] || die "Source dir not found: $SRC"
[[ -f "$SRC/Dockerfile"   ]] || die "Dockerfile missing in $SRC"
[[ -f "$SRC/.env"         ]] || die ".env missing in $SRC — refuse to deploy without it"
[[ -f "$COMPOSE_DIR/docker-compose.yml" ]] || die "Compose file missing: $COMPOSE_DIR/docker-compose.yml"
command -v docker >/dev/null || die "docker not installed"
sudo docker ps >/dev/null 2>&1 || die "docker daemon unreachable"

# Record current image id so rollback is a single docker tag + restart
PREV=$(sudo docker inspect "$CONTAINER" --format '{{.Image}}' 2>/dev/null || echo "")
if [[ -n "$PREV" ]]; then
  log "Current $CONTAINER image: $PREV"
  log "Rollback command:  sudo docker tag $PREV $IMAGE:local && (cd $COMPOSE_DIR && sudo docker compose up -d $SERVICE)"
fi

# ───── Build ──────────────────────────────────────────────────
if [[ $BUILD -eq 1 ]]; then
  log "Building $IMAGE:$TAG from $SRC"
  cd "$SRC"
  sudo docker build -t "$IMAGE:$TAG" -t "$IMAGE:local" .
  log "Build OK"
else
  log "Skipping build (--no-build); using existing $IMAGE:local"
  sudo docker image inspect "$IMAGE:local" >/dev/null 2>&1 \
    || die "$IMAGE:local does not exist; cannot --no-build"
fi

# ───── Deploy ─────────────────────────────────────────────────
log "docker compose up -d $SERVICE"
cd "$COMPOSE_DIR"
sudo docker compose up -d "$SERVICE"

# ───── Wait for container health ──────────────────────────────
log "Waiting for $CONTAINER to become healthy..."
for i in $(seq 1 30); do
  state=$(sudo docker inspect "$CONTAINER" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}nohealthcheck:{{.State.Status}}{{end}}' \
    2>/dev/null || echo missing)
  case "$state" in
    healthy)          log "Container healthy"; break ;;
    nohealthcheck:running) log "No healthcheck defined; container is running"; break ;;
    starting|nohealthcheck:created) ;;   # keep waiting
    *)                ;;
  esac
  if [[ $i -eq 30 ]]; then
    die "Container did not start cleanly (last state: $state). Check: sudo docker logs --tail 60 $CONTAINER"
  fi
  sleep 2
done

# ───── Reload nginx (upstream IP changes on recreate) ─────────
log "Reloading nginx so it picks up the new container IP"
sudo docker exec sensu-nginx nginx -s reload >/dev/null 2>&1 || log "  (nginx reload skipped — container may not exist)"
sleep 2

# ───── HTTP smoke test ────────────────────────────────────────
log "Smoke test: $HEALTH_URL"
code=$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" --max-time 10)
[[ "$code" == "200" ]] || die "HTTP $code (expected 200). Check: sudo docker logs --tail 60 $CONTAINER"
log "HTTP 200 OK"

# ───── Prune old versioned tags, keep 3 most recent ───────────
log "Pruning old $IMAGE:v* tags (keeping 3 most recent)"
sudo docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E "^$IMAGE:v[0-9]" \
  | sort -r \
  | awk 'NR>3 {print}' \
  | while read -r old; do
      log "  removing $old"
      sudo docker rmi "$old" >/dev/null 2>&1 || true
    done

log "Deploy complete. $IMAGE:$TAG is live."
