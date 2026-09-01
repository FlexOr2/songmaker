#!/bin/bash
# Pull-based auto-deploy for Songmaker (issue #298).
#
# A GitOps timer, not a webhook or a self-hosted CI runner: this script polls
# origin/main every couple of minutes (see songmaker-autodeploy.timer) and
# fast-forwards + redeploys the local checkout when main has moved. CI is
# green on every merge to main by process, so "origin/main moved" already
# means "this is deployable" — the script itself does not re-run tests.
#
# Safety rules below come from real incidents, not hypotheticals:
#   - A redeploy on 2026-08-30 18:31 mid-generation killed every in-flight
#     stream. `docker compose build` (image build only, no container
#     recreate) is now separated from `docker compose up -d --wait`
#     (container recreate). The active-jobs check runs once before the pull
#     and again right before the recreate step, so the only window a job can
#     start and still get killed is the few seconds between that second
#     check and the recreate — not the 8-15 minutes a cold-cache build can
#     take. A build-only tick that finds jobs active on the recheck defers
#     to the next tick, which finds the image already built and is back at
#     the recheck within seconds.
#   - Migrations occasionally abort loudly on lock_timeout (see the
#     c9d4a2f18e37 unique-slug-index migration). That is not fatal — the
#     next tick retries the same pull+deploy. A tick that could not deploy
#     despite main having moved (build/pull/compose failure, a deferred job
#     check, or a fail-closed DB check) increments a setback counter; only N
#     consecutive setbacks escalate to a prio=err journal line worth paging
#     on. A tick that deploys, or that finds nothing to deploy, resets it.
#   - This host's `.env` and checkout are also the operator's manual
#     workspace. A dirty tree, a diverged local main, or HEAD sitting on a
#     branch other than the deploy branch are left completely untouched —
#     the script only ever fast-forwards a clean checkout of the deploy
#     branch.
#
# Per CLAUDE.md: NEVER wrap `docker compose up --build --wait` in `timeout`.
# A cold-cache rebuild can take 8-15 minutes; the systemd unit has no
# TimeoutStartSec override for the same reason. That rule is about the
# image BUILD, not about the DB round-trip the active-jobs check makes —
# that one gets its own short `timeout` (see below) so a wedged DB
# connection can't hang a tick indefinitely.
#
# All journal lines are tagged "songmaker-autodeploy" (`journalctl -t
# songmaker-autodeploy`). The steady-state "nothing to do" tick (the common
# case, every ~2 minutes) logs at debug priority — an unfiltered `journalctl
# -t songmaker-autodeploy` still shows it (journald keeps everything by
# default), but any priority-restricted view (e.g. `journalctl -t
# songmaker-autodeploy -p info`, or a dashboard/alerting rule that only
# watches info-and-above) stays quiet, and only real events (deferred,
# refused, deployed, failed) surface there.
#
# REPO_ROOT is derived from where this script itself lives (same pattern as
# install-autostart.sh's SCRIPT_DIR/PROJECT_ROOT derivation), never hardcoded
# here — running it from a worktree operates on that worktree. The installer
# (scripts/install-autodeploy.sh) separately substitutes the matching
# WorkingDirectory/User/ExecStart into the systemd unit, exactly like
# install-autostart.sh does for songmaker.service.
#
# LOCK_FILE and FAILURE_COUNT_FILE default to the systemd-managed
# RuntimeDirectory/StateDirectory (see songmaker-autodeploy.service) so both
# survive reboots (state) or are cleaned automatically (lock) without this
# script owning that lifecycle. The /var/tmp path is only a fallback for a
# manual run outside systemd (e.g. testing from a shell), and the
# SONGMAKER_AUTODEPLOY_* env vars remain the explicit override for that case.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

LOG_TAG="songmaker-autodeploy"
DEPLOY_BRANCH="${SONGMAKER_AUTODEPLOY_BRANCH:-main}"
LOCK_FILE="${SONGMAKER_AUTODEPLOY_LOCK_FILE:-${RUNTIME_DIRECTORY:-/var/tmp}/songmaker-autodeploy.lock}"
FAILURE_COUNT_FILE="${SONGMAKER_AUTODEPLOY_FAILURE_COUNT_FILE:-${STATE_DIRECTORY:-/var/tmp}/songmaker-autodeploy.failcount}"
FAILURE_ALERT_THRESHOLD="${SONGMAKER_AUTODEPLOY_FAILURE_ALERT_THRESHOLD:-3}"
DB_CHECK_TIMEOUT_SECONDS="${SONGMAKER_AUTODEPLOY_DB_CHECK_TIMEOUT_SECONDS:-30}"
POSTGRES_USER="${POSTGRES_USER:-songmaker}"
POSTGRES_DB="${POSTGRES_DB:-songmaker}"

log() {
    local level="$1"
    shift
    logger -t "$LOG_TAG" -p "user.$level" -- "$*"
}
log_debug() { log debug "$*"; }
log_info() { log info "$*"; }
log_err() { log err "$*"; }

compose() {
    (cd "$REPO_ROOT" && docker compose "$@")
}

# --- Active-jobs guard, called once before the pull and once again right
# before the container recreate (see the header). Bounded by a short
# `timeout` — a wedged DB connection must not hang a tick, unlike the image
# build below, which is allowed to run as long as it needs.
active_job_count() {
    local output
    if ! output="$(cd "$REPO_ROOT" && timeout "$DB_CHECK_TIMEOUT_SECONDS" docker compose exec -T postgres psql \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')" 2>&1)"; then
        printf '%s' "$output"
        return 1
    fi
    local count
    count="$(printf '%s' "$output" | tr -d '[:space:]')"
    if ! [[ "$count" =~ ^[0-9]+$ ]]; then
        printf '%s' "$output"
        return 1
    fi
    printf '%s' "$count"
}

read_failure_count() {
    if [[ ! -f "$FAILURE_COUNT_FILE" ]]; then
        printf '0'
        return
    fi
    local count
    count="$(cat "$FAILURE_COUNT_FILE")"
    if ! [[ "$count" =~ ^[0-9]+$ ]]; then
        log_err "failure count file $FAILURE_COUNT_FILE has corrupt content ('$count') — resetting to 0"
        printf '0' >"$FAILURE_COUNT_FILE"
        printf '0'
        return
    fi
    printf '%s' "$count"
}

reset_setback_count() {
    printf '0' >"$FAILURE_COUNT_FILE"
}

record_success() {
    local deployed_sha="$1"
    reset_setback_count
    log_info "deploy succeeded, now running $deployed_sha"
}

# Every tick that could not deploy despite $DEPLOY_BRANCH having moved (or,
# for the branch/dirty/diverged guards, that refused to even look) bumps
# this counter — a deferral because jobs are active is not itself a crisis,
# but N of them in a row without ever landing is exactly the "stuck" signal
# the ALERT line exists for. Only success and "nothing to deploy" reset it.
record_setback() {
    local reason="$1"
    local count
    count="$(read_failure_count)"
    count=$((count + 1))
    printf '%s' "$count" >"$FAILURE_COUNT_FILE"
    log_debug "setback count now $count (this tick: $reason)"
    if [[ "$count" -ge "$FAILURE_ALERT_THRESHOLD" ]]; then
        log_err "ALERT: $count ticks in a row deferred/failed, reason: $reason — auto-deploy is stuck, needs human attention"
    fi
}

# --- 1. Refuse to overlap with another run in flight (a slow cold-cache
# build can outlive one 2-minute tick). Busy is the expected steady state
# under a running build, not an error — logged at debug so an unfiltered
# journal still shows it, but no info/err noise.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log_debug "another run is already in flight, skipping this tick"
    exit 0
fi

# --- 2. HEAD must be the deploy branch before anything else is inspected —
# a detached HEAD or an operator experiment branch must never get fast-
# forwarded onto origin/$DEPLOY_BRANCH.
if ! CURRENT_BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>&1)"; then
    log_err "HEAD at $REPO_ROOT is not on a branch (detached?) — refusing to deploy, not touching the tree: $CURRENT_BRANCH"
    record_setback "HEAD not on a branch"
    exit 1
fi

if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
    log_err "HEAD at $REPO_ROOT is on branch '$CURRENT_BRANCH', not the deploy branch '$DEPLOY_BRANCH' — refusing to deploy, not touching the tree"
    record_setback "HEAD on branch $CURRENT_BRANCH instead of $DEPLOY_BRANCH"
    exit 1
fi

# --- 3. The common case: nothing changed. Cheap, and must stay silent.
if ! LOCAL_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>&1)"; then
    log_err "cannot determine local HEAD in $REPO_ROOT: $LOCAL_HEAD"
    exit 1
fi

if ! FETCH_OUTPUT="$(git -C "$REPO_ROOT" fetch origin "$DEPLOY_BRANCH" --quiet 2>&1)"; then
    log_err "git fetch origin $DEPLOY_BRANCH failed in $REPO_ROOT: $FETCH_OUTPUT"
    exit 1
fi

if ! REMOTE_HEAD="$(git -C "$REPO_ROOT" rev-parse "origin/$DEPLOY_BRANCH" 2>&1)"; then
    log_err "cannot resolve origin/$DEPLOY_BRANCH in $REPO_ROOT: $REMOTE_HEAD"
    exit 1
fi

if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
    reset_setback_count
    log_debug "already at origin/$DEPLOY_BRANCH ($LOCAL_HEAD) — nothing to deploy"
    exit 0
fi

# --- 4. main moved. Before touching anything, make sure it is safe to.
# The operator works in this checkout — a dirty tree or a diverged local
# main is left completely alone.
if ! STATUS_OUTPUT="$(git -C "$REPO_ROOT" status --porcelain 2>&1)"; then
    log_err "cannot determine working tree status in $REPO_ROOT: $STATUS_OUTPUT"
    record_setback "git status failed"
    exit 1
fi

if [[ -n "$STATUS_OUTPUT" ]]; then
    log_err "working tree at $REPO_ROOT is dirty — refusing to deploy, not touching it (the operator may be working here)"
    record_setback "working tree dirty"
    exit 1
fi

if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$LOCAL_HEAD" "$REMOTE_HEAD"; then
    log_err "local HEAD ($LOCAL_HEAD) has diverged from origin/$DEPLOY_BRANCH ($REMOTE_HEAD) — not fast-forwardable, refusing to deploy, not touching the tree"
    record_setback "local HEAD diverged from origin/$DEPLOY_BRANCH"
    exit 1
fi

# --- 5. Job guard, ahead of the first step that touches the checkout. An
# unreachable DB fails closed: unknown job state is treated as "do not
# deploy", not as "assume idle".
if ! ACTIVE_JOB_COUNT="$(active_job_count)"; then
    log_err "cannot reach the database to check for active jobs — refusing to deploy (fail closed): $ACTIVE_JOB_COUNT"
    record_setback "database unreachable before pull"
    exit 1
fi

if [[ "$ACTIVE_JOB_COUNT" -gt 0 ]]; then
    log_info "deploy deferred, $ACTIVE_JOB_COUNT jobs active"
    record_setback "$ACTIVE_JOB_COUNT jobs active before pull"
    exit 0
fi

# --- 6. Pull, then build. No timeout around the build (CLAUDE.md) — a
# cold-cache rebuild legitimately takes 8-15 minutes. Building does not
# recreate any running container, so it cannot kill an in-flight job by
# itself — the recheck in step 7 is what guards the actual recreate.
if ! PULL_OUTPUT="$(git -C "$REPO_ROOT" pull --ff-only origin "$DEPLOY_BRANCH" 2>&1)"; then
    log_err "git pull --ff-only failed in $REPO_ROOT despite passing the fast-forward check: $PULL_OUTPUT"
    record_setback "git pull failed"
    exit 1
fi

if ! BUILD_OUTPUT="$(compose build 2>&1)"; then
    log_err "docker compose build failed in $REPO_ROOT: $BUILD_OUTPUT"
    record_setback "compose build failed"
    exit 1
fi

# --- 7. Recheck immediately before the recreate — the only step that can
# kill an in-flight job. This shrinks the unsafe window from the build's
# 8-15 minutes down to the seconds between this check and `compose up`. A
# deferral here finds the image already built on the next tick, so the
# recheck lands within seconds rather than after another full build.
if ! ACTIVE_JOB_COUNT="$(active_job_count)"; then
    log_err "cannot reach the database to recheck for active jobs after build — refusing to recreate containers (fail closed): $ACTIVE_JOB_COUNT"
    record_setback "database unreachable after build"
    exit 1
fi

if [[ "$ACTIVE_JOB_COUNT" -gt 0 ]]; then
    log_info "deploy deferred after build, $ACTIVE_JOB_COUNT jobs active"
    record_setback "$ACTIVE_JOB_COUNT jobs active after build"
    exit 0
fi

if compose up -d --wait; then
    COMPOSE_EXIT_CODE=0
    record_success "$(git -C "$REPO_ROOT" rev-parse HEAD)"
else
    COMPOSE_EXIT_CODE=$?
    log_err "docker compose up -d --wait failed in $REPO_ROOT (exit $COMPOSE_EXIT_CODE)"
    record_setback "compose up failed"
fi

exit "$COMPOSE_EXIT_CODE"
