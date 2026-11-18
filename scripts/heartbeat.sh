#!/usr/bin/env bash

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

send_telegram() {
    local MESSAGE="$1"

    if [ -z "${STATUS_BOT_TOKEN:-}" ]; then
        echo "STATUS_BOT_TOKEN is not configured."
        return
    fi

    if [ -z "${OWNER_ID:-}" ]; then
        echo "OWNER_ID is not configured."
        return
    fi

    if curl -fsS \
        --max-time 15 \
        -X POST \
        "https://api.telegram.org/bot${STATUS_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${OWNER_ID}" \
        --data-urlencode "text=${MESSAGE}" \
        --data-urlencode "parse_mode=HTML" \
        >/dev/null; then
        echo "Startup notification sent to Telegram."
    else
        echo "WARNING: Unable to send the startup notification to Telegram." >&2
    fi
}

get_uptime() {
    uptime -p 2>/dev/null || echo "just started"
}

get_memory() {
    free -h 2>/dev/null | awk '/Mem:/ {print $3 " / " $2}' || echo "unknown"
}

get_cpu() {
    awk '{print $1}' /proc/loadavg 2>/dev/null || echo "unknown"
}

bot_status_plain() {
    local NAME="$1"
    local PID_FILE="$2"

    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "🟢 $NAME: RUNNING (PID $PID)"
            return
        fi
    fi

    echo "🔴 $NAME: STOPPED"
}

# Wait up to 10 seconds for endpoint files if not yet written
for i in {1..10}; do
    if [ -s /tmp/panel_url.txt ] && [ -s /tmp/ssh_cmd.txt ]; then
        break
    fi
    sleep 1
done

PANEL_URL="Cloudflare URL unavailable"
if [ -s /tmp/panel_url.txt ]; then
    PANEL_URL=$(cat /tmp/panel_url.txt)
fi

# The startup message advertises the SSH command with a port forward to the loopback-only 9Router
# dashboard. Local port 20129 avoids clashing with a 9Router already running on the client's 20128.
NINEROUTER_LOCAL_PORT=20129
SSH_CMD="ssh admin@localhost"
if [ -s /tmp/ssh_cmd.txt ]; then
    SSH_CMD=$(cat /tmp/ssh_cmd.txt)
fi
NINEROUTER_HTML=""
NINEROUTER_PLAIN=""
case "$SSH_CMD" in
    "ssh "*)
        SSH_CMD="ssh -L ${NINEROUTER_LOCAL_PORT}:127.0.0.1:20128 ${SSH_CMD#ssh }"
        NINEROUTER_URL="http://localhost:${NINEROUTER_LOCAL_PORT}/dashboard"
        NINEROUTER_HTML=$'\n'"9Router dashboard (while connected): <code>${NINEROUTER_URL}</code>"
        NINEROUTER_PLAIN=$'\n'"  🔀 9Router dashboard   : ${NINEROUTER_URL}"
        ;;
esac

NOW=$(TZ='Asia/Tehran' date '+%Y-%m-%d %H:%M:%S Tehran')
BOT_REGISTRY_PATH="${BOT_REGISTRY_PATH:-$HOME/bot-server/bot-registry.json}"
if ! STATUS_FLEET=$(python3 "$SCRIPT_DIR/bot_fleet_status.py" \
    --registry "$BOT_REGISTRY_PATH" --pid-directory /tmp); then
    STATUS_FLEET="🔴 Bot status unavailable (registry read failed)"
fi
STATUS_FLEET_PLAIN=$(printf '%s\n' "$STATUS_FLEET" | sed 's/^/    /')
CPU_INFO=$(get_cpu)
RAM_INFO=$(get_memory)
HERMES_STATUS="🔴 Hermes API: UNAVAILABLE"
if curl -fsS --max-time 3 http://127.0.0.1:8642/health >/dev/null 2>&1; then
    HERMES_STATUS="🟢 Hermes API: READY"
fi
HERMES_TELEGRAM_STATUS="⚪ Hermes Telegram: not configured"
if [ -s /tmp/hermes_bot.txt ]; then
    HERMES_TELEGRAM_STATUS="🟢 Hermes Telegram: $(cat /tmp/hermes_bot.txt)"
elif [ -n "${HERMES_TELEGRAM_BOT_TOKEN:-}" ]; then
    HERMES_TELEGRAM_STATUS="🔴 Hermes Telegram: FAILED (see Logs → Hermes Gateway)"
fi

SETUP_WARNING_HTML=""
SETUP_WARNING_PLAIN=""
SETUP_WARNINGS_FILE="${SETUP_WARNINGS_FILE:-/tmp/server-setup-warnings.txt}"
if [ -s "$SETUP_WARNINGS_FILE" ]; then
    SETUP_WARNINGS=$(cat "$SETUP_WARNINGS_FILE")
    SETUP_WARNINGS_ESCAPED=$(printf '%s' "$SETUP_WARNINGS" | sed \
        -e 's/&/\&amp;/g' \
        -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g')
    SETUP_WARNING_HTML="<b>⚠️ Setup warnings - inspect</b>
<pre>${SETUP_WARNINGS_ESCAPED}</pre>"
    SETUP_WARNING_PLAIN="  ⚠️ Setup warnings - inspect:
$(printf '%s\n' "$SETUP_WARNINGS" | sed 's/^/    /')"
fi

TELEGRAM_MSG="<b>🚀 BOT SERVER IS ONLINE</b>

<b>🌐 Web Control Panel</b>
<a href=\"${PANEL_URL}\">${PANEL_URL}</a>

<b>💻 SSH Access + 9Router Tunnel</b>
<code>${SSH_CMD}</code>${NINEROUTER_HTML}

<b>🤖 Bot Status</b>
${STATUS_FLEET}
${HERMES_STATUS}
${HERMES_TELEGRAM_STATUS}
${SETUP_WARNING_HTML}

<b>📊 System Resources</b>
CPU load: ${CPU_INFO}
RAM: ${RAM_INFO}
Started at: ${NOW}"

PLAIN_MSG="==================================================
  🚀 BOT SERVER IS ONLINE
  🌐 Web Control Panel   : ${PANEL_URL}
  💻 SSH + 9Router tunnel: ${SSH_CMD}${NINEROUTER_PLAIN}
  ------------------------------------------------
  🤖 Bot Status:
${STATUS_FLEET_PLAIN}
        ${HERMES_STATUS}
        ${HERMES_TELEGRAM_STATUS}
${SETUP_WARNING_PLAIN}
  ------------------------------------------------
  📊 System Resources:
    CPU load: ${CPU_INFO}
    RAM: ${RAM_INFO}
    Started: ${NOW}
=================================================="

echo "$PLAIN_MSG"
echo

send_telegram "$TELEGRAM_MSG"
