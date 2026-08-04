#!/bin/bash
# Deploy the built image to the live container on the Unraid server.
#
#   ./deploy-unraid.sh              deploy movie-filter:latest
#   ./deploy-unraid.sh --rollback   go back to the newest rollback-* tag
#   ./deploy-unraid.sh --rollback movie-filter:rollback-20260803-2038
#
# Companion to build-unraid.sh, which deliberately stops at "built". Building and
# replacing the container that people are actually using are separate decisions, and this
# is the second one. Nothing here builds; the image must already exist.
#
# What it does, in order: refuse if a filter run is in flight, stop the container, back up
# the database, bring it up from docker-compose.deploy.yml, then verify it came back on
# the GPU. Any of those failing leaves a printed rollback command.
#
# Three things about this server shape the script:
#
#   * A run in flight must not be interrupted. Runs are destructive - the original is
#     archived and the library copy replaced - and nothing resumes across a restart:
#     reap_orphans() fails an interrupted run outright. A *queued* run is safe, it is put
#     back on the queue, so only a running one blocks. --force overrides.
#   * The database is the run history and every review decision, and it is not
#     regenerable. It is backed up while the container is stopped, so the WAL is quiesced
#     rather than copied out from under a live writer.
#   * The current container may predate compose. If it was created with `docker run` it
#     carries no compose labels, so `docker compose up -d` would fail on the container
#     name rather than adopt it. That case is detected and the old container removed
#     first; after one deploy through this script, compose owns it and this stops
#     applying.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
started=$SECONDS

COMPOSE_FILE=docker-compose.deploy.yml
SERVICE=movie-filter
TAG="movie-filter:latest"
ROLLBACK=""
FORCE=0
BACKUP=1
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        # An optional argument: --rollback alone picks the newest rollback-* tag, or a
        # specific tag can be named. `--rollback --force` must not eat the next flag.
        --rollback)
            ROLLBACK="auto"
            if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then ROLLBACK="$2"; shift; fi ;;
        --force)     FORCE=1 ;;
        --no-backup) BACKUP=0 ;;
        -y|--yes)    ASSUME_YES=1 ;;
        -h|--help)   sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)           echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

short() { echo "${1:7:12}"; }   # sha256:abcdef... -> abcdef...
img_id() { docker images --no-trunc --format '{{.ID}}' "$1" 2>/dev/null | head -1; }

# --- preflight ----------------------------------------------------------------------
step "Preflight"
docker info >/dev/null 2>&1 || die "the Docker daemon is not responding"
docker compose version >/dev/null 2>&1 \
    || die "docker compose is missing; run /boot/config/docker-compose.sh to re-link it"
[ -f "$COMPOSE_FILE" ] || die "$COMPOSE_FILE not found (run this from the repo)"
docker compose -f "$COMPOSE_FILE" config --quiet || die "$COMPOSE_FILE does not parse"
ok "docker compose $(docker compose version --short)"

# Read the published port and the /data bind out of the compose file rather than
# hardcoding them: they are the file's business, and a copy here would silently rot the
# next time the port moves.
port=$(docker compose -f "$COMPOSE_FILE" config --format json \
        | jq -r '.services["'"$SERVICE"'"].ports[0].published // empty')
[ -n "$port" ] || die "could not read the published port from $COMPOSE_FILE"
data_dir=$(docker compose -f "$COMPOSE_FILE" config --format json \
        | jq -r '.services["'"$SERVICE"'"].volumes[]? | select(.target=="/data") | .source' | head -1)
ok "port $port, data $data_dir"

# --- pick the image ------------------------------------------------------------------
if [ -n "$ROLLBACK" ]; then
    step "Rollback"
    if [ "$ROLLBACK" = "auto" ]; then
        # Newest by creation time, not by tag string: the tag is only a timestamp by
        # convention and a hand-made one may not sort.
        ROLLBACK=$(docker images --filter 'reference=movie-filter:rollback-*' \
                    --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' \
                    | sort -r | head -1 | cut -f2)
        [ -n "$ROLLBACK" ] || die "no movie-filter:rollback-* tag to roll back to"
        ok "newest rollback tag: $ROLLBACK"
    fi
    docker image inspect "$ROLLBACK" >/dev/null 2>&1 || die "no such image: $ROLLBACK"
    ok "will re-point $TAG at $ROLLBACK"
fi

# The compose file pins movie-filter:latest, so rolling back means re-pointing that tag.
# That is deferred until after the confirmation prompt below - doing it here would leave
# :latest moved even when the answer is no, quietly changing what the *next* deploy ships.
# The image being replaced keeps whatever rollback tag build-unraid.sh gave it, so this is
# reversible in both directions.
if [ -n "$ROLLBACK" ]; then
    target_id=$(img_id "$ROLLBACK")
else
    target_id=$(img_id "$TAG")
    [ -n "$target_id" ] || die "$TAG does not exist; run ./build-unraid.sh first"
fi

# --- what is about to change ---------------------------------------------------------
step "Change"
current_id=""
if docker inspect "$SERVICE" >/dev/null 2>&1; then
    current_id=$(docker inspect "$SERVICE" --format '{{.Image}}')
fi
if [ -z "$current_id" ]; then
    ok "no container yet; this is a first deploy"
elif [ "$current_id" = "$target_id" ]; then
    warn "the running container is already on $(short "$target_id") - nothing will change"
    warn "(deploying anyway just restarts it)"
else
    ok "$(short "$current_id") -> $(short "$target_id")"
fi

# --- refuse to interrupt a run -------------------------------------------------------
step "Checking for active runs"
live=$(curl -s --max-time 5 "http://localhost:${port}/api/runs/live" 2>/dev/null || true)
if [ -z "$live" ]; then
    # Not fatal: a container that is already down or wedged is exactly when a deploy is
    # wanted. It does mean the guard could not run, which is worth saying out loud.
    warn "no answer from /api/runs/live - cannot tell whether a run is active"
else
    running=$(jq -r '.running // 0' <<<"$live")
    queued=$(jq -r '.queued // 0' <<<"$live")
    if [ "$running" -gt 0 ]; then
        warn "$running run(s) in progress:"
        jq -r '.runs[] | select(.status=="running")
               | "      \(.name) — \(.stage) \(.progress)%"' <<<"$live"
        warn ""
        warn "Restarting kills these mid-write. Nothing resumes: they will be marked"
        warn "failed and have to be re-run from the archive with \"Edit & re-run\"."
        [ "$FORCE" -eq 1 ] || die "a run is in progress (re-run with --force to deploy anyway)"
        warn "--force given; continuing anyway"
    else
        ok "no runs in progress"
    fi
    # Queued runs survive a restart - reap_orphans puts them back on the queue - so this
    # is information, not an obstacle.
    [ "${queued:-0}" -gt 0 ] && ok "$queued queued run(s); these are re-queued on restart"
fi

# --- confirm -------------------------------------------------------------------------
if [ "$ASSUME_YES" -eq 0 ]; then
    printf '\n    Deploy %s to the live container? [y/N] ' "$(short "$target_id")"
    read -r reply
    case "$reply" in [yY]*) ;; *) echo "    aborted"; exit 1 ;; esac
fi

# Past the point of no return, so the rollback retag happens now rather than at parse time.
if [ -n "$ROLLBACK" ]; then
    docker tag "$ROLLBACK" "$TAG"
    ok "$TAG -> $ROLLBACK"
fi

# --- stop ----------------------------------------------------------------------------
step "Stopping"
compose_owned=0
if [ -n "$current_id" ]; then
    if docker inspect "$SERVICE" --format '{{index .Config.Labels "com.docker.compose.project"}}' \
        2>/dev/null | grep -q .; then
        compose_owned=1
    fi
fi
if [ "$compose_owned" -eq 1 ]; then
    docker compose -f "$COMPOSE_FILE" stop || die "could not stop the service"
    ok "stopped (compose-managed)"
elif [ -n "$current_id" ]; then
    # Created by `docker run`, so compose will not adopt it - it would just collide on the
    # container name. Removing it is safe: every piece of state is in the /data bind, and
    # the container's own writable layer holds nothing but logs.
    warn "the existing container was not created by compose; removing it so compose can take over"
    docker rm -f "$SERVICE" >/dev/null || die "could not remove the existing container"
    ok "removed"
else
    ok "nothing running"
fi

# --- back up the database -------------------------------------------------------------
# Done here, with the container down, so the -wal and -shm files are not being written
# while they are copied. All three are taken: a filter.db copied without its WAL loses
# whatever had not been checkpointed.
if [ "$BACKUP" -eq 1 ] && [ -n "$data_dir" ] && [ -f "$data_dir/filter.db" ]; then
    step "Backing up the database"
    stamp=$(date +%Y%m%d-%H%M)
    for ext in "" "-wal" "-shm"; do
        [ -f "$data_dir/filter.db$ext" ] \
            && cp -p "$data_dir/filter.db$ext" "$data_dir/filter.db.pre-${stamp}.bak$ext"
    done
    ok "filter.db.pre-${stamp}.bak ($(du -h "$data_dir/filter.db" | cut -f1))"
fi

# --- up --------------------------------------------------------------------------------
step "Starting"
docker compose -f "$COMPOSE_FILE" up -d || die "docker compose up failed"
ok "up"

# --- verify -----------------------------------------------------------------------------
# The deploy is not done when the container starts - it is done when Whisper is proved to
# be on the GPU. A container that silently fell back to CPU looks perfectly healthy and
# runs 10-20x slower, which is the failure this check exists to catch.
step "Verifying"
health=""
for _ in $(seq 1 30); do
    health=$(curl -s --max-time 5 "http://localhost:${port}/api/health" 2>/dev/null || true)
    [ -n "$health" ] && break
    sleep 4
done

if [ -z "$health" ]; then
    docker compose -f "$COMPOSE_FILE" logs --tail 30 || true
    printf '\n\033[31mThe container never answered /api/health on port %s.\033[0m\n' "$port" >&2
    printf 'Roll back with:\n  ./deploy-unraid.sh --rollback\n' >&2
    exit 1
fi

device=$(jq -r '.device // "unknown"' <<<"$health")
gpu_ok=$(jq -r '.gpu_ok // false' <<<"$health")
ok "device=$device gpu_ok=$gpu_ok"
if [ "$gpu_ok" != "true" ]; then
    warn ""
    warn "Whisper did NOT get the GPU. The app works but runs 10-20x slower."
    warn "detail: $(jq -r '.detail // "-"' <<<"$health")"
    warn "Check the Nvidia-Driver plugin is loaded (nvidia-smi) and that the deploy file's"
    warn "GPU reservation survived: docker inspect $SERVICE --format '{{.HostConfig.DeviceRequests}}'"
fi

# Surface what the restart did to any in-flight work. reap_orphans logs both numbers at
# startup, and they are easy to miss in the container log.
docker compose -f "$COMPOSE_FILE" logs 2>/dev/null | grep '\[startup\]' | tail -3 \
    | while read -r line; do ok "${line#*| }"; done || true

printf '\n\033[32mDeployed in %d min %d sec.\033[0m\n' \
    $(( (SECONDS - started) / 60 )) $(( (SECONDS - started) % 60 ))
cat <<EOF

  http://192.168.50.31:${port}

If it misbehaves, roll back to the previous image:

  ./deploy-unraid.sh --rollback
EOF
