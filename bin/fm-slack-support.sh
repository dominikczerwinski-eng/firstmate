#!/usr/bin/env bash
# fm-slack-support.sh - poll and route Fenedo support reports from Slack.
#
# Usage:
#   fm-slack-support.sh poll
#   fm-slack-support.sh status
#   fm-slack-support.sh complete <message-ts> [<reply>]
#
# The Python client reads SLACK_BOT_TOKEN and the other SLACK_SUPPORT_* values
# from the effective home's gitignored .env, with environment variables taking
# precedence. It never prints or passes the token in argv. Polling uses Slack's
# conversations.list, users.list, conversations.history, and chat.postMessage
# Web API methods over standard-library HTTPS only; there is no paid API.
#
# A cosmetic report becomes a queued tasks-axi ship item with repo=fenedo-os and
# a durable firstmate check wake. A report that is product, legal, destructive,
# security-sensitive, financial, or ambiguous is held and gets only a durable
# wake. Once a queued fix lands, `complete` posts the short reply in the original
# Slack thread. The poller does not create a Slack app or invite a bot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_HOME="${FM_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  printf 'fm-slack-support: python3 is required\n' >&2
  exit 1
fi
exec "$PY" "$SCRIPT_DIR/fm-slack-support.py" --home "$FM_HOME" --root "${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}" "$@"
