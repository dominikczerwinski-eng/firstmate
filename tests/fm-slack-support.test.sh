#!/usr/bin/env bash
# Behavior tests for bin/fm-slack-support.sh.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-slack-support)
HOME_DIR="$TMP_ROOT/home"
REQ_LOG="$TMP_ROOT/slack-requests"
mkdir -p "$HOME_DIR"
SUPPORT="$ROOT/bin/fm-slack-support.sh"

test_missing_token_fails_closed() {
  local out rc=0
  out=$(env -u SLACK_BOT_TOKEN FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1) || rc=$?
  expect_code 1 "$rc" "poll without a token must fail closed"
  assert_contains "$out" "SLACK_BOT_TOKEN" "missing-token diagnostic names the env contract"
  assert_not_contains "$out" "Bearer" "missing-token diagnostic never prints an auth header"
  pass "fm-slack-support: missing token fails closed without pretending the bot exists"
}

test_status_needs_no_network() {
  local out
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "channel: #support" "status uses the default support channel"
  assert_contains "$out" "token: missing" "status reports token absence without contacting Slack"
  pass "fm-slack-support: status is local-only"
}

test_env_contract_and_help() {
  cat > "$HOME_DIR/.env" <<'EOF'
SLACK_BOT_TOKEN=secret-token-value
SLACK_SUPPORT_CHANNEL=fenedo-support
EOF
  local out
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "channel: #fenedo-support" "status reads the channel from .env"
  assert_contains "$out" "token: configured" "status detects a token without printing it"
  assert_not_contains "$out" "secret-token-value" "status never leaks the token"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" --help 2>&1)
  assert_contains "$out" "complete" "help documents the thread completion command"
  pass "fm-slack-support: .env contract and CLI help are safe"
}

test_reporter_first_name_matching() {
  local out
  out=$(python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("support", "bin/fm-slack-support.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.reporter_matches({"name": "martyna.nowak", "real_name": "Martyna Nowak"}, {"martyna"})
assert module.reporter_matches({"real_name": "Martyna Nowak"}, {"martyna"})
assert module.reporter_matches({"name": "kasia", "real_name": "Kasia"}, {"kasia"})
assert not module.reporter_matches({"name": "marta", "real_name": "Marta"}, {"martyna"})
print("ok")
PY
)
assert_contains "$out" "ok" "reporter matching accepts configured first names and Slack handles"
pass "fm-slack-support: reporter allowlist handles display names such as Martyna Nowak"
}

SLACK_FIXTURE_PID=""

stop_slack_fixture() {
  [ -n "$SLACK_FIXTURE_PID" ] || return 0
  kill "$SLACK_FIXTURE_PID" 2>/dev/null || true
  wait "$SLACK_FIXTURE_PID" 2>/dev/null || true
  SLACK_FIXTURE_PID=""
}

# fail() exits, so fixture teardown cannot ride on a RETURN trap. lib.sh's
# signal traps exit, which runs this EXIT trap in turn.
trap 'stop_slack_fixture; fm_test_cleanup' EXIT

start_slack_fixture() {
  local mode_file=$1 port_file=$2 request_log=$3
  python3 - "$mode_file" "$port_file" "$request_log" <<'PY' &
import json
import os
import pathlib
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

mode_file = pathlib.Path(sys.argv[1])
port_file = pathlib.Path(sys.argv[2])
request_log = pathlib.Path(sys.argv[3])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        method = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        mode = mode_file.read_text(encoding="utf-8").strip()
        # Serialized request trace: the contract this suite asserts against.
        with request_log.open("a", encoding="utf-8") as handle:
            handle.write(method + " " + (query.get("channel") or [""])[0] + "\n")
        if method == "conversations.list" and query.get("types") == ["im"]:
            if mode == "transient":
                self.connection.close()
                return
            if mode == "http503":
                self.send_error(503, "slack unavailable")
                return
            if mode == "hard":
                body = {"ok": False, "error": "missing_scope"}
            elif mode == "probe_ratelimited":
                body = {"ok": False, "error": "ratelimited"}
            elif mode.startswith("dm_fetch_"):
                body = {"ok": True, "channels": [{"id": "D-im1"}, {"id": "D-im2"}]}
            else:
                body = {"ok": True, "channels": []}
        elif method == "conversations.list":
            body = {"ok": True, "channels": [{"id": "C-support", "name": "support"}]}
        elif method == "users.list":
            body = {"ok": True, "members": []}
        elif method == "conversations.history":
            if query.get("channel") == ["D-im1"] and mode == "dm_fetch_429":
                self.send_error(429, "slow down")
                return
            if query.get("channel") == ["D-im1"] and mode == "dm_fetch_truncated":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                self.wfile.write(b'{"ok": true, "messages": []')
                self.close_connection = True
                return
            failures = {
                "dm_fetch_ratelimited": "ratelimited",
                "dm_fetch_hard": "invalid_arguments",
                "dm_fetch_scope": "missing_scope",
            }
            if query.get("channel") == ["D-im1"] and mode in failures:
                body = {"ok": False, "error": failures[mode]}
            else:
                body = {"ok": True, "messages": []}
        elif method == "auth.test":
            body = {"ok": True, "user_id": "U-bot"}
        else:
            body = {"ok": False, "error": "unexpected_method"}
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
port_file.write_text(str(server.server_address[1]), encoding="utf-8")
threading.Thread(target=server.serve_forever, daemon=True).start()
# Self-bound so an escaped fixture cannot outlive its suite.
threading.Event().wait(float(os.environ.get("FM_TEST_STUB_MAX_BLOCK_SECONDS", "120")))
PY
  SLACK_FIXTURE_PID=$!
  for _ in $(seq 1 50); do
    [ -s "$port_file" ] && return 0
    sleep 0.02
  done
  return 1
}

# Poll once, require a clean exit, and leave the output in POLL_OUT. The
# assertion must not run inside a command substitution: fail() exits, and in a
# subshell that exit would be swallowed and the suite would keep going.
POLL_OUT=""
slack_poll() {  # <label>
  local rc=0
  : > "$REQ_LOG"
  POLL_OUT=$(FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1) || rc=$?
  expect_code 0 "$rc" "$1"
}

test_dm_poll_classifies_transient_and_hard_failures() {
  local mode_file="$TMP_ROOT/slack-mode" port_file="$TMP_ROOT/slack-port"
  printf 'transient\n' > "$mode_file"
  start_slack_fixture "$mode_file" "$port_file" "$REQ_LOG" || fail "Slack fixture failed to start"
  cat > "$HOME_DIR/.env" <<EOF
SLACK_BOT_TOKEN=test-token
SLACK_API_URL=http://127.0.0.1:$(cat "$port_file")
SLACK_SUPPORT_TIMEOUT=5
EOF

  local out
  slack_poll "a transient DM timeout must not fail the poll"
  out="$POLL_OUT"
  assert_contains "$out" "no new Slack support reports" "transient DM timeout still completes the poll quietly"
  assert_not_contains "$out" "dm-disabled" "transient DM timeout stays silent"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_not_contains "$out" "dm_enabled: no" "a timeout must not persist a durable DM disable"
  assert_not_contains "$out" "dm_error" "a timeout must not persist a durable DM error"

  printf 'http503\n' > "$mode_file"
  slack_poll "a retryable Slack 503 must not fail the poll"
  out="$POLL_OUT"
  assert_contains "$out" "no new Slack support reports" "a retryable Slack 503 still completes the poll quietly"
  assert_not_contains "$out" "dm-disabled" "a retryable Slack 503 is not a DM disable"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_not_contains "$out" "dm_enabled: no" "a retryable 503 must not persist a durable DM disable"
  assert_not_contains "$out" "dm_error" "a retryable 503 must not persist a durable DM error"

  printf 'healthy\n' > "$mode_file"
  slack_poll "a healthy poll succeeds after a timeout"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "the next healthy poll resumes normally"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "healthy poll recalculates DM capability after timeout"

  printf 'transient\n' > "$mode_file"
  slack_poll "a timeout after a healthy poll must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "a timeout after a healthy poll stays silent"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "a timeout leaves the last known DM capability intact"

  printf 'hard\n' > "$mode_file"
  slack_poll "a scope refusal reports through poll output, not a crash"
  out="$POLL_OUT"
  assert_contains "$out" "dm-disabled" "Slack permission refusal still emits hard-disable"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: no" "a scope refusal persists the durable DM disable"
  assert_contains "$out" "dm_error" "a scope refusal persists the durable DM error"

  printf 'transient\n' > "$mode_file"
  slack_poll "a timeout after a scope refusal must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "a timeout does not re-emit the standing scope alarm"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: no" "a timeout does not clear a real DM permission alarm"

  printf 'healthy\n' > "$mode_file"
  slack_poll "a healthy poll succeeds after a scope refusal"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "healthy poll does not retain hard-disable output"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "healthy poll recalculates DM capability"

  printf 'probe_ratelimited\n' > "$mode_file"
  slack_poll "a rate-limited DM capability probe must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "a rate-limited probe is not a permission loss"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "a rate-limited probe keeps the last known DM capability"
  assert_not_contains "$out" "dm_error" "a rate-limited probe persists no DM error"

  printf 'dm_fetch_429\n' > "$mode_file"
  slack_poll "a rate-limited DM history fetch must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "a rate-limited DM history fetch is not a permission loss"
  assert_contains "$out" "dm-retry:" "a deferred DM fetch still reaches the operator"
  assert_grep "conversations.history D-im2" "$REQ_LOG" "a deferred DM does not abandon the remaining DMs"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "a rate-limited DM history fetch keeps the DM capability"
  assert_not_contains "$out" "dm_error" "a rate-limited DM history fetch persists no DM error"

  printf 'dm_fetch_ratelimited\n' > "$mode_file"
  slack_poll "a ratelimited DM history body must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "an ok:false ratelimited body is not a permission loss"
  assert_contains "$out" "dm-retry:" "an ok:false ratelimited body defers with an operator line"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "an ok:false ratelimited body keeps the DM capability"

  printf 'dm_fetch_truncated\n' > "$mode_file"
  slack_poll "a truncated DM history response must not fail the poll"
  out="$POLL_OUT"
  assert_not_contains "$out" "dm-disabled" "a truncated response is not a permission loss"
  assert_contains "$out" "dm-retry:" "a truncated response defers with an operator line"
  assert_grep "conversations.history D-im2" "$REQ_LOG" "a truncated response does not abandon the remaining DMs"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "a truncated response keeps the DM capability"

  printf 'dm_fetch_hard\n' > "$mode_file"
  slack_poll "a rejected DM history fetch reports through poll output, not a crash"
  out="$POLL_OUT"
  assert_contains "$out" "dm-fetch-failed D-im1" "an unreadable DM is reported per conversation"
  assert_not_contains "$out" "dm-disabled" "one unreadable DM is not a lost DM capability"
  assert_grep "conversations.history D-im2" "$REQ_LOG" "an unreadable DM does not abandon the remaining DMs"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "one unreadable DM leaves the probed DM capability intact"

  printf 'dm_fetch_scope\n' > "$mode_file"
  slack_poll "a DM history scope refusal reports through poll output, not a crash"
  out="$POLL_OUT"
  assert_contains "$out" "dm-disabled" "a DM history scope refusal still alarms"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: no" "a DM history scope refusal persists the DM disable"
  stop_slack_fixture
  pass "fm-slack-support: transient DM failures are distinct from authorization failures"
}

test_missing_token_fails_closed
test_status_needs_no_network
test_env_contract_and_help
test_reporter_first_name_matching
test_dm_poll_classifies_transient_and_hard_failures
