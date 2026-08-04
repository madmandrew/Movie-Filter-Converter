#!/bin/bash
# Build the Docker image on the Unraid server itself.
#
#   ./build-unraid.sh              build, smoke-test, leave the running container alone
#   ./build-unraid.sh --skip-test  build only
#   ./build-unraid.sh --gpu        smoke-test with --gpus all (see the caveat below)
#
# This is the server-side counterpart to build.ps1, which stays the Windows path and is
# still the right script on the dev laptop. The difference is not just the shell:
#
#   * No tarball. build.ps1 ends with `docker save | gzip` because the image has to be
#     carried to a machine with no source tree. Here the source tree *is* on the server
#     (/mnt/user/appdata/Movie-Filter-Converter), so the image is built straight into the
#     daemon that will run it and save/load is pure waste - ~3.5 GB written twice.
#   * A disk-space preflight, which the laptop does not need. See below.
#   * A rollback tag, which the tarball made unnecessary on the laptop: over there the
#     previous image is still sitting in a .tar.gz. Here, overwriting :latest with a bad
#     build would leave nothing to go back to.
#
# It deliberately does NOT deploy. Building and restarting the live container are separate
# decisions, and the second one is made by hand:
#
#   docker compose -f docker-compose.deploy.yml up -d
#   curl -s localhost:8181/api/health     # want {"gpu_ok":true,"device":"cuda",...}

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
started=$SECONDS

TAG="movie-filter:latest"
TEST_PORT=18000
SKIP_TEST=0
SMOKE_GPU=0
ROLLBACK=1
FORCE=0

# Docker's storage lives in a *fixed-size* 20 GB btrfs loopback image
# (/mnt/user/system/docker/docker.img), not on a normal filesystem that can grow. A build
# that runs it out of space fails halfway and leaves partial layers behind, so the free
# space is checked before anything is written rather than discovered mid-`pip install`.
#
# How much is needed swings by two orders of magnitude depending on one thing: whether
# requirements.txt changed. The apt and pip layers are cached against the history of any
# existing movie-filter image, so a code-only change rebuilds nothing but the two COPY
# layers and needs a few MB. Touch requirements.txt and pip re-runs, which is ~3.5 GB of
# new layers plus transient space while wheels unpack. Hence two thresholds rather than
# one: below CRITICAL nothing can work, and between the two it depends on the change.
CRITICAL_FREE_GIB=2
COMFORTABLE_FREE_GIB=6

# The Whisper weights the smoke test needs are already cached in the live /data volume.
# Mounted read-only so a smoke container can never write to the deployed data directory;
# without it the container re-downloads tiny.en from HuggingFace on every run.
MODEL_CACHE=/mnt/user/appdata/movie-filter/models

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-test)   SKIP_TEST=1 ;;
        --gpu)         SMOKE_GPU=1 ;;
        --no-rollback) ROLLBACK=0 ;;
        --force)       FORCE=1 ;;
        --tag)         TAG="$2"; shift ;;
        --port)        TEST_PORT="$2"; shift ;;
        -h|--help)     sed -n '2,24p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)             echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
warn() { printf '    \033[33m%s\033[0m\n' "$1"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

# --- docker engine ------------------------------------------------------------------
step "Checking Docker"
docker info >/dev/null 2>&1 || die "the Docker daemon is not responding"
ok "engine ready"

# --- disk space ---------------------------------------------------------------------
step "Checking space in docker.img"
avail_gib=$(( $(df -Pk /var/lib/docker | awk 'NR==2 {print $4}') / 1024 / 1024 ))
if [ "$avail_gib" -lt "$COMFORTABLE_FREE_GIB" ]; then
    warn "only ${avail_gib} GiB free in /var/lib/docker (of 20 GiB total)."
    warn "Fine for a code-only change; not enough if requirements.txt changed and pip re-runs."
    warn ""
    warn "Old movie-filter tags are the usual culprit, but most of them share their layers"
    warn "with :latest and cost almost nothing. Check UNIQUE SIZE, not SIZE:"
    warn "  docker system df -v | head -20"
    warn "then remove the ones that actually own layers, plus stale build cache:"
    warn "  docker rmi <tag>"
    warn "  docker builder prune -f"
    if [ "$avail_gib" -lt "$CRITICAL_FREE_GIB" ]; then
        [ "$FORCE" -eq 1 ] || die "under ${CRITICAL_FREE_GIB} GiB free (re-run with --force to try anyway)"
        warn "--force given; continuing anyway"
    fi
else
    ok "${avail_gib} GiB free"
fi

# --- warn on uncommitted work --------------------------------------------------------
# The build reads the working tree, not HEAD, so what gets baked in can differ from what
# is pushed. Not an error - building to test an in-progress change is normal here - but
# worth seeing before an image is tagged with it.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    step "Source"
    ok "$(git log --oneline -1)"
    dirty=$(git status --porcelain | grep -v '\.tar\.gz' || true)
    if [ -n "$dirty" ]; then
        warn "working tree has uncommitted changes; the image will contain them:"
        echo "$dirty" | head -10 | while read -r line; do warn "  $line"; done
    fi
fi

# --- rollback tag --------------------------------------------------------------------
# Point a dated tag at whatever :latest is now, before the build overwrites it.
#
# The timestamp carries the time, not just the date, matching the rollback-20260803-2038
# tag made by hand on the server. Date alone collides on the second build of a day, and
# whichever way that collision is resolved is wrong half the time: overwrite and the last
# known-good image loses its tag, keep it and the tag no longer means "the image this
# build replaced". Per-build tags cost nothing - they share every layer.
if [ "$ROLLBACK" -eq 1 ]; then
    step "Tagging rollback point"
    old_id=$(docker images --no-trunc --format '{{.ID}}' "$TAG" | head -1)
    if [ -z "$old_id" ]; then
        ok "no existing $TAG to preserve (first build)"
    else
        rb="movie-filter:rollback-$(date +%Y%m%d-%H%M)"
        docker tag "$TAG" "$rb"
        # The ID is printed too, so the old image is recoverable even if the tag is lost.
        ok "$rb -> ${old_id:7:12}"
    fi
fi

# --- build ---------------------------------------------------------------------------
step "Building $TAG"
docker build -t "$TAG" . || die "docker build failed"
ok "built ($(docker images "$TAG" --format '{{.Size}}' | head -1))"

# --- smoke test ----------------------------------------------------------------------
# Catches an image that builds but cannot serve - a bad import or a missing dependency -
# before it is deployed over a working container. /api/health loads a real Whisper model
# rather than just importing, so this exercises ctranslate2 and the CUDA library path,
# not only FastAPI's startup.
if [ "$SKIP_TEST" -eq 0 ]; then
    step "Smoke test"
    name=movie-filter-smoke
    docker rm -f "$name" >/dev/null 2>&1 || true
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

    if ss -ltn 2>/dev/null | grep -q ":${TEST_PORT}\b"; then
        die "port $TEST_PORT is already in use; pass --port <n>"
    fi

    run_args=(-d --name "$name" -p "${TEST_PORT}:8000")
    if [ -d "$MODEL_CACHE" ]; then
        # Read-only, and only /data/models - the live filter.db is never in scope, and the
        # container writes its own throwaway DB into its ephemeral layer instead.
        run_args+=(-v "${MODEL_CACHE}:/data/models:ro")
    else
        warn "no model cache at $MODEL_CACHE; the test will download tiny.en (~75 MB)"
    fi
    if [ "$SMOKE_GPU" -eq 1 ]; then
        # Off by default: the GTX 1070 has 8 GB and the live container may be mid-run on
        # it. A second Whisper model loading alongside a real job can push it into OOM,
        # which would fail the *running* job, not just this test. CPU is enough to prove
        # the image serves; the GPU is proven for real by /api/health after deploying.
        run_args+=(--gpus all)
    fi

    docker run "${run_args[@]}" "$TAG" >/dev/null || die "could not start the smoke container"

    health=""
    for _ in $(seq 1 30); do
        health=$(curl -s --max-time 5 "http://localhost:${TEST_PORT}/api/health" 2>/dev/null || true)
        [ -n "$health" ] && break
        sleep 4
    done
    if [ -z "$health" ]; then
        docker logs --tail 30 "$name" || true
        die "the container never answered /api/health on port $TEST_PORT"
    fi

    if command -v jq >/dev/null 2>&1; then
        ok "health: device=$(jq -r .device <<<"$health") gpu_ok=$(jq -r .gpu_ok <<<"$health")"
    else
        ok "health: $health"
    fi
    [ "$SMOKE_GPU" -eq 1 ] || ok "(gpu_ok=false is expected here - no --gpus flag)"

    docker rm -f "$name" >/dev/null 2>&1 || true
    trap - EXIT
fi

# --- next steps ----------------------------------------------------------------------
printf '\n\033[32mDone in %d min %d sec.\033[0m\n' $(( (SECONDS - started) / 60 )) $(( (SECONDS - started) % 60 ))
cat <<'EOF'

The running container is still on the old image. To deploy:

  docker compose -f docker-compose.deploy.yml up -d
  curl -s localhost:8181/api/health     # want {"gpu_ok":true,"device":"cuda",...}

Use docker-compose.deploy.yml, not docker-compose.yml: the latter publishes 8080, which
qBittorrent already holds on this host, and carries a `build:` section this script has
already done the work of.

To roll back:

  docker tag movie-filter:rollback-<date> movie-filter:latest
  docker compose -f docker-compose.deploy.yml up -d
EOF
