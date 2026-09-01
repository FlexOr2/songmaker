#!/bin/bash
# scripts/alert.sh — the one channel every host-level alert source (issue
# #333) uses to reach the operator: a single email. Prometheus alerts go
# through Alertmanager's own SMTP client instead (monitoring/
# alertmanager.yml.template — same .env account, same inbox, see its
# comment for why); this script is for systemd's OnFailure= path, where
# there is no such engine and a plain synchronous send is all one failed
# unit needs.
#
# Usage: scripts/alert.sh "<subject>" "<body>"
#
# Configuration comes exclusively from .env at the project root (resolved
# from this script's own location, same SCRIPT_DIR/REPO_ROOT pattern as
# auto-deploy.sh — running a copy from a worktree reads that worktree's
# own .env): ALERT_EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER,
# SMTP_PASSWORD. Missing or empty configuration exits non-zero with a
# named reason on stderr instead of silently skipping the send — this
# script never reports success unless curl itself reported success.
#
# curl's built-in SMTP client (--ssl-reqd smtp://...) sends the mail
# without installing sendmail/msmtp/ssmtp: curl already ships on this host
# (verified: `curl --version` lists smtp/smtps among its protocols) and is
# already a project dependency elsewhere, so this avoids adding a second
# mail-transport package to keep patched for a single outbound message a
# few times a month. SMTP_HOST/SMTP_PORT are expected to be a STARTTLS
# endpoint (Gmail's documented app-password setup: smtp.gmail.com:587,
# which is what --ssl-reqd upgrades to TLS on connect) — an implicit-TLS
# endpoint (port 465) would need smtps:// instead, not needed here.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $(basename "$0") <subject> <body>" >&2
    exit 1
fi

SUBJECT="$1"
BODY="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${SONGMAKER_ALERT_ENV_FILE:-$REPO_ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "alert.sh: no .env at $ENV_FILE — cannot load SMTP config, not sending" >&2
    exit 1
fi

# .env is this project's own trusted, operator-owned config file (same one
# Pydantic Settings loads for the app itself) — sourcing it is the robust
# way to get its exact quoting/escaping right, not a hand-rolled parser
# that would mishandle a quoted value differently than the app does.
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

MISSING=()
[[ -z "${ALERT_EMAIL_TO:-}" ]] && MISSING+=(ALERT_EMAIL_TO)
[[ -z "${SMTP_HOST:-}" ]] && MISSING+=(SMTP_HOST)
[[ -z "${SMTP_PORT:-}" ]] && MISSING+=(SMTP_PORT)
[[ -z "${SMTP_USER:-}" ]] && MISSING+=(SMTP_USER)
[[ -z "${SMTP_PASSWORD:-}" ]] && MISSING+=(SMTP_PASSWORD)

if ((${#MISSING[@]} > 0)); then
    echo "alert.sh: missing ${MISSING[*]} in $ENV_FILE — refusing to pretend this sent" >&2
    exit 1
fi

MESSAGE="Subject: ${SUBJECT}
From: ${SMTP_USER}
To: ${ALERT_EMAIL_TO}

${BODY}
"

if ! CURL_OUTPUT="$(curl --silent --show-error --fail \
    --ssl-reqd \
    --url "smtp://${SMTP_HOST}:${SMTP_PORT}" \
    --mail-from "$SMTP_USER" \
    --mail-rcpt "$ALERT_EMAIL_TO" \
    --user "${SMTP_USER}:${SMTP_PASSWORD}" \
    --upload-file - <<<"$MESSAGE" 2>&1)"; then
    # Defense in depth: curl does not echo back credentials on a protocol
    # error, but scrub the password from whatever it did print before this
    # line reaches the journal, in case a future curl version ever changes
    # what an error message includes.
    CURL_OUTPUT="${CURL_OUTPUT//$SMTP_PASSWORD/[REDACTED]}"
    echo "alert.sh: SMTP send to $SMTP_HOST:$SMTP_PORT failed: $CURL_OUTPUT" >&2
    exit 1
fi

echo "alert.sh: sent \"$SUBJECT\" to $ALERT_EMAIL_TO"
