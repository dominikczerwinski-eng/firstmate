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

test_missing_token_fails_closed
test_status_needs_no_network
test_env_contract_and_help
