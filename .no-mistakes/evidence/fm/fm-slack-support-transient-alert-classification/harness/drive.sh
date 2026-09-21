#!/usr/bin/env bash
# Drive bin/fm-slack-support.sh as an operator would, against a local
# stand-in Slack endpoint, and print the raw CLI transcript.
#   drive.sh target   -> the branch under test
#   drive.sh base     -> the pre-fix implementation (ef32bcc)
set -u
IMPL=${1:-target}
EV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WT=/Users/dominik/.no-mistakes/worktrees/f0e74a9a109f/01M31SPJ6FVJM0ZFZNGQG40H4S
RUN=$(mktemp -d "${TMPDIR:-/tmp}/fm-slack-live.XXXXXX")
HOME_DIR="$RUN/home"; mkdir -p "$HOME_DIR"
MODE="$RUN/mode"; PORT="$RUN/port"; REQ="$RUN/requests.log"; POST="$RUN/posts.log"; FX="$RUN/fixtures.json"
: > "$REQ"; : > "$POST"
printf 'healthy\n' > "$MODE"
printf '{}\n' > "$FX"

cleanup() { [ -n "${FIXTURE_PID:-}" ] && kill "$FIXTURE_PID" 2>/dev/null; rm -rf "$RUN"; }
trap cleanup EXIT

FAKE_SLACK_MAX_SECONDS=300 python3 "$EV/harness/fake_slack.py" "$MODE" "$PORT" "$REQ" "$POST" "$FX" &
FIXTURE_PID=$!
for _ in $(seq 1 100); do [ -s "$PORT" ] && break; sleep 0.05; done
[ -s "$PORT" ] || { echo "fixture failed to start"; exit 1; }

cat > "$HOME_DIR/.env" <<EOF
SLACK_BOT_TOKEN=live-test-token
SLACK_API_URL=http://127.0.0.1:$(cat "$PORT")
SLACK_SUPPORT_TIMEOUT=5
EOF

if [ "$IMPL" = base ]; then
  run() { python3 "$EV/harness/baseline/fm-slack-support.py" --home "$HOME_DIR" --root "$WT" "$@" 2>&1; }
else
  run() { FM_HOME="$HOME_DIR" "$WT/bin/fm-slack-support.sh" "$@" 2>&1; }
fi

set_mode() { printf '%s\n' "$1" > "$MODE"; }
set_fixtures() { printf '%s' "$1" > "$FX"; }
hr() { printf '\n================ %s ================\n' "$1"; }
step() { printf '\n--- $ fm-slack-support.sh %s   [slack: %s]\n' "$*" "$(cat "$MODE")"; run "$@"; printf '[exit %s]\n' "$?"; }

echo "implementation under test: $IMPL"
echo

hr "warm-up: one healthy poll so DM capability is known good"
step poll
step status

hr "S1  transient network timeout during the DM capability probe"
set_mode probe_timeout
step poll
step status

hr "S2  genuine loss of the im:* scopes"
set_mode probe_missing_scope
step poll
step status

hr "S3a  a reporter DMs a bug while that DM is rate-limited (HTTP 429)"
set_mode healthy
step poll   # clear the standing scope alarm first
set_fixtures '{"D-martyna":[{"ts":"1700000100.0001","user":"U-martyna","text":"Oferta nie generuje sie, blad 500 przy zapisie pomiaru"}]}'
set_mode dm_429
: > "$POST"
step poll
step status
printf '\n[requests this poll]\n'; grep -c 'conversations.history D-kasia' "$REQ" >/dev/null && grep 'conversations.history' "$REQ" | tail -5
printf '[replies posted to the reporter]\n'; cat "$POST"

hr "S3b  next poll after the rate limit clears: the deferred report is delivered"
set_mode healthy
: > "$POST"
step poll
step status
printf '\n[replies posted to the reporter]\n'; cat "$POST"
printf '[queued fenedo-os backlog item]\n'; ls "$HOME_DIR/data/tasks" 2>/dev/null | head; find "$HOME_DIR/data" -name '*slack-support*' 2>/dev/null | head

hr "S4  truncated DM response in a poll that already ingested a channel report"
set_fixtures '{"C-support":[{"ts":"1700000200.0001","user":"U-martyna","text":"Pipedrive sync nie dziala, wisi na logowaniu"}],"D-martyna":[],"D-kasia":[]}'
set_mode dm_truncated
: > "$POST"
step poll
step status
printf '\n[replies posted during the truncated poll]\n'; cat "$POST"
set_mode healthy
: > "$POST"
step poll
printf '[replies posted on the following poll - must not re-ack the same channel report]\n'; cat "$POST"; echo "(post count: $(wc -l < "$POST" | tr -d ' '))"

hr "S5  one DM is permanently unreadable (invalid_arguments)"
set_fixtures '{"D-kasia":[]}'
set_mode dm_invalid_args
: > "$REQ"
step poll
step status
printf '\n[conversations.history calls this poll]\n'; grep 'conversations.history' "$REQ"

hr "final persisted state"
python3 -c "import json,sys;s=json.load(open('$HOME_DIR/state/slack-support/state.json'));print(json.dumps({k:s.get(k) for k in ('dm_enabled','dm_error','latest_ts','im_cursors','channel_name')},indent=2,ensure_ascii=False))" 2>/dev/null \
  || { echo "(state file layout)"; find "$HOME_DIR/state" -maxdepth 2 -type f | head; }
