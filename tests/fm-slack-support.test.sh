#!/usr/bin/env bash
# Behavior tests for bin/fm-slack-support.sh.
set -u

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-slack-support)
HOME_DIR="$TMP_ROOT/home"
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

start_slack_fixture() {
  local mode_file=$1 port_file=$2
  python3 - "$mode_file" "$port_file" <<'PY' &
import json
import pathlib
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

mode_file = pathlib.Path(sys.argv[1])
port_file = pathlib.Path(sys.argv[2])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        method = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        mode = mode_file.read_text(encoding="utf-8").strip()
        if method == "conversations.list" and query.get("types") == ["im"]:
            if mode == "transient":
                self.connection.close()
                return
            if mode == "hard":
                body = {"ok": False, "error": "missing_scope"}
            else:
                body = {"ok": True, "channels": []}
        elif method == "conversations.list":
            body = {"ok": True, "channels": [{"id": "C-support", "name": "support"}]}
        elif method == "users.list":
            body = {"ok": True, "members": []}
        elif method == "conversations.history":
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
threading.Event().wait()
PY
  SLACK_FIXTURE_PID=$!
  for _ in $(seq 1 50); do
    [ -s "$port_file" ] && return 0
    sleep 0.02
  done
  return 1
}

test_dm_poll_classifies_transient_and_hard_failures() {
  local mode_file="$TMP_ROOT/slack-mode" port_file="$TMP_ROOT/slack-port"
  printf 'transient\n' > "$mode_file"
  start_slack_fixture "$mode_file" "$port_file" || fail "Slack fixture failed to start"
  trap 'kill "$SLACK_FIXTURE_PID" 2>/dev/null || true' RETURN
  cat > "$HOME_DIR/.env" <<EOF
SLACK_BOT_TOKEN=test-token
SLACK_API_URL=http://127.0.0.1:$(cat "$port_file")
SLACK_SUPPORT_TIMEOUT=5
EOF

  local out
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1)
  assert_not_contains "$out" "dm-disabled" "transient DM timeout stays silent"

  printf 'healthy\n' > "$mode_file"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1)
  assert_not_contains "$out" "dm-disabled" "the next healthy poll resumes normally"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "healthy poll recalculates DM capability after timeout"

  printf 'hard\n' > "$mode_file"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1)
  assert_contains "$out" "dm-disabled" "Slack permission refusal still emits hard-disable"

  printf 'healthy\n' > "$mode_file"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" poll 2>&1)
  assert_not_contains "$out" "dm-disabled" "healthy poll does not retain hard-disable output"
  out=$(FM_HOME="$HOME_DIR" "$SUPPORT" status 2>&1)
  assert_contains "$out" "dm_enabled: yes" "healthy poll recalculates DM capability"
  pass "fm-slack-support: transient DM failures are distinct from authorization failures"
}

test_missing_token_fails_closed
test_status_needs_no_network
test_env_contract_and_help
test_reporter_first_name_matching
test_dm_poll_classifies_transient_and_hard_failures
