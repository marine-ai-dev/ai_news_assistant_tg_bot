#!/usr/bin/env bash
#
# Deploy or update the AI News assistant on the server.
#
# The order below is the whole point of this script, and it is not negotiable:
#
#   1. stop the long-running processes
#   2. update the code and dependencies
#   3. apply migrations   <- must succeed
#   4. run the health check <- must succeed
#   5. only then start the processes again
#
# If step 3 or 4 fails the services stay down. That is the correct outcome: a review
# bot running against a schema it does not understand, or a scheduler that cannot read
# its own queue, is worse than a bot that is simply off. The failure is loud, the
# system is untouched, and the previous state is still on disk.
#
# Migrations are deliberately a separate, explicit step. Neither long-running process
# migrates on startup — a process that changes the schema as a side effect of being
# restarted is a process that can change the schema at three in the morning because
# something crashed.
#
# Usage:  sudo -u ainews deploy/update.sh
#         sudo -u ainews deploy/update.sh --no-restart   (update and verify only)

set -Eeuo pipefail

APP_DIR="${AI_NEWS_APP_DIR:-/opt/ai-news/app}"
VENV="${APP_DIR}/.venv"
AI_NEWS="${VENV}/bin/ai-news"
SERVICES=(ai-news-bot.service ai-news-scheduler.service)
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m!!! %s\033[0m\n' "$*" >&2; exit 1; }

trap 'die "failed on line $LINENO. Services were NOT started; nothing was published."' ERR

# --- 0. sanity -------------------------------------------------------------

cd "$APP_DIR" || die "no application directory at $APP_DIR"
[[ -x "$AI_NEWS" ]] || die "no ai-news executable at $AI_NEWS — is the venv created?"

# The environment file holds the bot token. If it is readable by anyone else, stop
# and say so rather than quietly continuing with a leaked credential.
ENV_FILE="${AI_NEWS_ENV_FILE:-/etc/ai-news/env}"
if [[ -f "$ENV_FILE" ]]; then
    perms=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo "")
    [[ "$perms" =~ ^[0-9]?600$ ]] || warn "$ENV_FILE is mode ${perms:-unknown}; it holds the bot token and should be 600"
fi

# --- 1. stop ---------------------------------------------------------------

log "Stopping services"
for unit in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        # SIGTERM: both processes stop at a safe boundary rather than mid-send.
        sudo systemctl stop "$unit"
        echo "    stopped $unit"
    else
        echo "    $unit was not running"
    fi
done

# --- 2. update -------------------------------------------------------------

log "Updating code"
git fetch --quiet origin
BEFORE=$(git rev-parse --short HEAD)
git merge --ff-only origin/main
AFTER=$(git rev-parse --short HEAD)
echo "    ${BEFORE} -> ${AFTER}"

log "Updating dependencies"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -e .

# --- 3. migrate ------------------------------------------------------------
#
# A backup first. Migrations here are additive and tested against a copy before they
# ever reach a server, but a database holding the record of what was published to a
# real channel deserves a copy that predates the change.

log "Backing up the database"
DB="${AI_NEWS_DATA_DIR:-/opt/ai-news/data}/ai_news.sqlite3"
if [[ -f "$DB" ]]; then
    BACKUP="${DB%.sqlite3}-$(date +%Y%m%d-%H%M%S).backup.sqlite3"
    # .backup rather than cp: it is safe while the WAL exists and produces one file.
    sqlite3 "$DB" ".backup '${BACKUP}'"
    echo "    ${BACKUP}"
    # Keep the last ten; a server disk is not an archive.
    ls -1t "${DB%.sqlite3}"-*.backup.sqlite3 2>/dev/null | tail -n +11 | xargs -r rm --
else
    warn "no database yet — 'ai-news db init' will create it below"
    "$AI_NEWS" db init
fi

log "Applying migrations"
"$AI_NEWS" db migrate || die "migration failed. Services stay down; the backup above is the state before this attempt."

# --- 4. verify -------------------------------------------------------------

log "Health check"
"$AI_NEWS" doctor || die "doctor reported a problem. Services stay down — fix the failing check and run this again."

# Read-only, and only when a token is configured. It contacts Telegram but sends
# nothing: getMe, getChat, getChatMember.
if grep -qs '^AI_NEWS_TELEGRAM_BOT_TOKEN=.\+' "$ENV_FILE"; then
    log "Telegram check (read-only)"
    "$AI_NEWS" telegram doctor || die "the Telegram setup is not usable. Services stay down."
fi

# --- 5. start --------------------------------------------------------------

if (( RESTART == 0 )); then
    log "Updated and verified. Services left stopped (--no-restart)."
    exit 0
fi

log "Starting services"
for unit in "${SERVICES[@]}"; do
    sudo systemctl start "$unit"
    echo "    started $unit"
done

sleep 3
FAILED=0
for unit in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$unit"; then
        echo "    ok  $unit"
    else
        warn "NOT RUNNING: $unit  —  journalctl -u $unit -n 50"
        FAILED=1
    fi
done
(( FAILED == 0 )) || die "a service did not stay up."

log "Done — ${AFTER}"
echo "    journalctl -fu ai-news-bot -u ai-news-scheduler"
echo
echo "    Nothing was approved and nothing was published by this script."
echo "    The scheduler publishes only what you approved and queued yourself."
