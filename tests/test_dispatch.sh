#!/bin/sh
# Tests for statusline-dispatch: mode selection, feed writing, and the
# run-ccstatusline-exactly-once invariant (regression for the double-run bug).
# Runs in an isolated HOME with a stubbed npx; requires jq.
set -e

DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP"
mkdir -p "$TMP/.claude" "$TMP/.config/ccstatusline" "$TMP/bin"

cat > "$TMP/.config/ccstatusline/settings.json" <<'EOF'
{"lines":[[{"id":"1","type":"model"},{"id":"2","type":"context-length"}],[{"id":"3","type":"reset-timer"},{"id":"4","type":"weekly-usage"},{"id":"5","type":"weekly-reset-timer"}]]}
EOF

cat > "$TMP/bin/npx" <<'EOF'
#!/bin/sh
cat > /dev/null
echo "CCSTATUSLINE RAN HOME=$HOME"
EOF
chmod +x "$TMP/bin/npx"
export PATH="$TMP/bin:$PATH"

PAYLOAD='{"session_id":"test-sess","model":{"display_name":"Test 1"},"context_window":{"used_percentage":42,"context_window_size":1000000,"current_usage":{"input_tokens":100,"cache_read_input_tokens":419000,"cache_creation_input_tokens":900,"output_tokens":100}},"cost":{"total_cost_usd":1.5},"rate_limits":{"five_hour":{"used_percentage":21,"resets_at":1900000000},"seven_day":{"used_percentage":11,"resets_at":1900050000}}}'

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- full mode: ccstatusline exactly once, real HOME, feed written -----------
OUT=$(printf '%s' "$PAYLOAD" | "$DIR/statusline-dispatch")
[ "$(printf '%s\n' "$OUT" | grep -c 'CCSTATUSLINE RAN')" = "1" ] || fail "full: ccstatusline must run exactly once"
printf '%s' "$OUT" | grep -q "HOME=$TMP\$" || fail "full: must use real HOME"
[ -f "$TMP/.claude/usage-feed/test-sess.json" ] || fail "full: feed not written"
jq -e '.rate_limits.five_hour.used_percentage == 21 and .captured_at > 0' \
  "$TMP/.claude/usage-feed/test-sess.json" > /dev/null || fail "full: feed content wrong"
# no ~/.claude.json to read: stamp an empty login rather than guessing one
jq -e '.account_uuid == ""' "$TMP/.claude/usage-feed/test-sess.json" > /dev/null \
  || fail "full: account_uuid must be empty when the login is unknown"

# --- feed carries the live login, so the monitor can scope to it -------------
echo '{"oauthAccount":{"accountUuid":"uuid-abc"}}' > "$TMP/.claude.json"
printf '%s' "$PAYLOAD" | "$DIR/statusline-dispatch" > /dev/null
jq -e '.account_uuid == "uuid-abc"' "$TMP/.claude/usage-feed/test-sess.json" > /dev/null \
  || fail "full: feed must stamp the active account uuid"

# --- lite mode: exactly once (double-run regression), shadow HOME, no network widgets
touch "$TMP/.claude/statusline.lite"
OUT=$(printf '%s' "$PAYLOAD" | "$DIR/statusline-dispatch")
[ "$(printf '%s\n' "$OUT" | grep -c 'CCSTATUSLINE RAN')" = "1" ] || fail "lite: ccstatusline must run exactly once"
printf '%s' "$OUT" | grep -q "ccstatusline-lite" || fail "lite: must use shadow HOME"
jq -e '[.lines[][] | .type] | (index("reset-timer") == null) and (index("weekly-usage") == null) and (index("weekly-reset-timer") == null) and (index("model") != null)' \
  "$TMP/.claude/ccstatusline-lite/.config/ccstatusline/settings.json" > /dev/null \
  || fail "lite: generated config must drop network widgets and keep the rest"

# --- native mode: renders from payload, never runs ccstatusline --------------
touch "$TMP/.claude/statusline.native"
OUT=$(printf '%s' "$PAYLOAD" | "$DIR/statusline-dispatch")
printf '%s' "$OUT" | grep -q "CCSTATUSLINE RAN" && fail "native: must not run ccstatusline"
printf '%s' "$OUT" | grep -q "Test 1" || fail "native: missing model name"
printf '%s' "$OUT" | grep -q "42%" || fail "native: missing context percent"
printf '%s' "$OUT" | grep -q '420k/1M' || fail "native: missing token counts"
printf '%s' "$OUT" | grep -q '\$1\.50' || fail "native: missing formatted cost"

# --- off wins over every other flag, still writes the feed -------------------
touch "$TMP/.claude/statusline.off"
rm -f "$TMP/.claude/usage-feed/test-sess.json"
OUT=$(printf '%s' "$PAYLOAD" | "$DIR/statusline-dispatch")
[ -z "$OUT" ] || fail "off: must produce no output"
[ -f "$TMP/.claude/usage-feed/test-sess.json" ] || fail "off: must still write the feed"

# --- payload without rate_limits: feed written with null, no crash -----------
rm -f "$TMP/.claude/statusline.off" "$TMP/.claude/statusline.native" "$TMP/.claude/statusline.lite"
MINIMAL='{"session_id":"bare","model":{"display_name":"X"}}'
OUT=$(printf '%s' "$MINIMAL" | "$DIR/statusline-dispatch")
[ "$(printf '%s\n' "$OUT" | grep -c 'CCSTATUSLINE RAN')" = "1" ] || fail "minimal payload: statusline must still render"
jq -e '.rate_limits == null' "$TMP/.claude/usage-feed/bare.json" > /dev/null || fail "minimal payload: feed should record null rate_limits"

# --- malicious session_id must not traverse paths ----------------------------
EVIL='{"session_id":"../../evil","rate_limits":{"five_hour":{"used_percentage":1,"resets_at":1900000000}}}'
printf '%s' "$EVIL" | "$DIR/statusline-dispatch" > /dev/null
[ ! -e "$TMP/.claude/evil.json" ] && [ ! -e "$TMP/evil.json" ] && [ ! -e "$TMP/.claude/usage-feed/../../evil.json" ] \
  || fail "path traversal: evil.json written outside feed dir"
find "$TMP" -name "*evil*" 2>/dev/null | grep -q . && fail "path traversal: evil file created somewhere" || true

# --- statusline-toggle mode transitions --------------------------------------
[ "$("$DIR/statusline-toggle" status)" = "statusline: full" ] || fail "toggle: expected full"
"$DIR/statusline-toggle" lite > /dev/null
[ "$("$DIR/statusline-toggle" status)" = "statusline: lite" ] || fail "toggle: expected lite"
"$DIR/statusline-toggle" native > /dev/null
[ "$("$DIR/statusline-toggle" status)" = "statusline: native" ] || fail "toggle: native must replace lite"
[ ! -f "$TMP/.claude/statusline.lite" ] || fail "toggle: lite flag should be cleared by native"
"$DIR/statusline-toggle" off > /dev/null
[ "$("$DIR/statusline-toggle" status)" = "statusline: off" ] || fail "toggle: expected off"
"$DIR/statusline-toggle" full > /dev/null
[ "$("$DIR/statusline-toggle" status)" = "statusline: full" ] || fail "toggle: expected full again"

echo "statusline-dispatch tests passed"
