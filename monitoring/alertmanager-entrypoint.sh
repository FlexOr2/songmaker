#!/bin/sh
# Entrypoint of the alertmanager container (issue #333), replacing the
# image's own /bin/alertmanager ENTRYPOINT so the config can be assembled
# first. Runs on the image's busybox /bin/sh: no bash, no envsubst, no
# python — sed is the substitution tool that exists here.
#
# Alertmanager's config format has no ${VAR} substitution of its own, so
# the four non-secret values of the one alert channel are substituted into
# monitoring/alertmanager.yml.template at container start. The fifth,
# SMTP_PASSWORD, deliberately never passes through this script: Docker
# mounts it as a secret file and Alertmanager reads that file itself via
# smtp_auth_password_file (see the template), so the password is in no
# process argument, no `docker inspect` output, and no generated config.

set -eu

CONFIG_TEMPLATE=/etc/alertmanager/alertmanager.yml.template
GENERATED_CONFIG=/tmp/alertmanager.yml
SMTP_PASSWORD_FILE=/run/secrets/smtp_password

if [ ! -s "$SMTP_PASSWORD_FILE" ]; then
    echo "alertmanager: $SMTP_PASSWORD_FILE is missing or empty (SMTP_PASSWORD unset in .env) -- refusing to start with an incomplete SMTP config" >&2
    exit 1
fi

MISSING=""
for NAME in ALERT_EMAIL_TO SMTP_HOST SMTP_PORT SMTP_USER; do
    eval "VALUE=\${$NAME:-}"
    if [ -z "$VALUE" ]; then
        MISSING="$MISSING $NAME"
    fi
done
if [ -n "$MISSING" ]; then
    echo "alertmanager: missing$MISSING in .env -- refusing to start with an incomplete SMTP config" >&2
    exit 1
fi

# sed reads its replacement text as a mini-language: an unescaped `&`
# stands for the whole match, `\` starts an escape, and `|` would close
# the s|…|…| expression early. A value carrying any of those would
# otherwise be silently mangled into a config that only fails when the
# first real alert tries to send.
escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

sed \
    -e "s|__SMTP_HOST__|$(escape_sed_replacement "$SMTP_HOST")|g" \
    -e "s|__SMTP_PORT__|$(escape_sed_replacement "$SMTP_PORT")|g" \
    -e "s|__SMTP_USER__|$(escape_sed_replacement "$SMTP_USER")|g" \
    -e "s|__ALERT_EMAIL_TO__|$(escape_sed_replacement "$ALERT_EMAIL_TO")|g" \
    "$CONFIG_TEMPLATE" >"$GENERATED_CONFIG"

exec /bin/alertmanager --config.file="$GENERATED_CONFIG" --storage.path=/alertmanager
