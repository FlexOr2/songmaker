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
#     stream. Every destructive step is now gated on the jobs table being
#     idle first (see the active-jobs check).
#   - Migrations occasionally abort loudly on lock_timeout (see the
#     c9d4a2f18e37 unique-slug-index migration). That is not fatal — the
#     next tick retries the same pull+deploy. Only N consecutive failures
#     escalate to a prio=err journal line worth paging on.
#   - This host's `.env` and checkout are also the operator's manual
#     workspace. A dirty tree or a diverged local main is left completely
#     untouched — the script only ever fast-forwards a clean tree.
#
# Per CLAUDE.md: NEVER wrap `docker compose up --build --wait` in `timeout`.
# A cold-cache rebuild can take 8-15 minutes; the systemd unit has no
# TimeoutStartSec override for the same reason.
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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

LOG_TAG="songmaker-autodeploy"
DEPLOY_BRANCH="${SONGMAKER_AUTODEPLOY_BRANCH:-main}"
LOCK_FILE="${SONGMAKER_AUTODEPLOY_LOCK_FILE:-/var/tmp/songmaker-autodeploy.lock}"
FAILURE_COUNT_FILE="${SONGMAKER_AUTODEPLOY_FAILURE_COUNT_FILE:-/var/tmp/songmaker-autodeploy.failcount}"
FAILURE_ALERT_THRESHOLD="${SONGMAKER_AUTODEPLOY_FAILURE_ALERT_THRESHOLD:-3}"
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

read_failure_count() {
    if [[ -f "$FAILURE_COUNT_FILE" ]]; then
        cat "$FAILURE_COUNT_FILE"
    else
        printf '0'
    fi
}

record_success() {
    local deployed_sha="$1"
    printf '0' >"$FAILURE_COUNT_FILE"
    log_info "deploy succeeded, now running $deployed_sha"
}

record_failure() {
    local count
    count="$(read_failure_count)"
    count=$((count + 1))
    printf '%s' "$count" >"$FAILURE_COUNT_FILE"
    log_err "deploy failed (attempt $count consecutive) in $REPO_ROOT"
    if [[ "$count" -ge "$FAILURE_ALERT_THRESHOLD" ]]; then
        log_err "ALERT: $count consecutive auto-deploy failures in $REPO_ROOT — auto-deploy is stuck, needs human attention"
    fi
}

# --- 1. Refuse to overlap with another run in flight (a slow cold-cache
# build can outlive one 2-minute tick). Busy is the expected steady state
# under a running build, not an error: exit silently, no journal line.
exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

# --- 2. The common case: nothing changed. Cheap, and must stay silent.
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
    log_debug "already at origin/$DEPLOY_BRANCH ($LOCAL_HEAD) — nothing to deploy"
    exit 0
fi

# --- 3. main moved. Before touching anything, make sure it is safe to.
# The operator works in this checkout — a dirty tree or a diverged local
# main is left completely alone.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    log_err "working tree at $REPO_ROOT is dirty — refusing to deploy, not touching it (the operator may be working here)"
    exit 1
fi

if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$LOCAL_HEAD" "$REMOTE_HEAD"; then
    log_err "local HEAD ($LOCAL_HEAD) has diverged from origin/$DEPLOY_BRANCH ($REMOTE_HEAD) — not fast-forwardable, refusing to deploy, not touching the tree"
    exit 1
fi

# --- 4. Job guard, ahead of the first destructive step. A deploy mid-take
# kills the take (incident 2026-08-30 18:31). An unreachable DB fails
# closed: unknown job state is treated as "do not deploy", not as "assume
# idle".
if ! ACTIVE_JOB_OUTPUT="$(compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')" 2>&1)"; then
    log_err "cannot reach the database to check for active jobs — refusing to deploy (fail closed): $ACTIVE_JOB_OUTPUT"
    exit 1
fi

ACTIVE_JOB_COUNT="$(printf '%s' "$ACTIVE_JOB_OUTPUT" | tr -d '[:space:]')"
if ! [[ "$ACTIVE_JOB_COUNT" =~ ^[0-9]+$ ]]; then
    log_err "unexpected output from the active-jobs check — refusing to deploy (fail closed): $ACTIVE_JOB_OUTPUT"
    exit 1
fi

if [[ "$ACTIVE_JOB_COUNT" -gt 0 ]]; then
    log_info "deploy deferred, $ACTIVE_JOB_COUNT jobs active"
    exit 0
fi

# --- 5. Deploy. No timeout around compose (CLAUDE.md) — a cold-cache
# rebuild legitimately takes 8-15 minutes.
if ! PULL_OUTPUT="$(git -C "$REPO_ROOT" pull --ff-only origin "$DEPLOY_BRANCH" 2>&1)"; then
    log_err "git pull --ff-only failed in $REPO_ROOT despite passing the fast-forward check: $PULL_OUTPUT"
    record_failure
    exit 1
fi

if compose up -d --build --wait; then
    COMPOSE_EXIT_CODE=0
    record_success "$(git -C "$REPO_ROOT" rev-parse HEAD)"
else
    COMPOSE_EXIT_CODE=$?
    record_failure
fi

exit "$COMPOSE_EXIT_CODE"
