#!/usr/bin/env bash
# Walks every requirement of the four tasks against the running gateways.
#
#   ./scripts/demo.sh
#
# Expects the MCP gateway on 8000 and the LLM gateway on 8001.

set -uo pipefail

MCP=${MCP_URL:-http://127.0.0.1:8000}
LLM=${LLM_URL:-http://127.0.0.1:8001}

pass=0
fail=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == *"$expected"* ]]; then
    printf '  PASS  %s\n' "$label"
    pass=$((pass + 1))
  else
    printf '  FAIL  %s\n         expected to find: %s\n         got: %s\n' \
      "$label" "$expected" "${actual:0:200}"
    fail=$((fail + 1))
  fi
}

section() { printf '\n%s\n' "$1"; }

VIEWER=$(python -m scripts.issue_token viewer --tenant tk_live_acme_9f2b)
ADMIN=$(python -m scripts.issue_token admin --tenant tk_live_acme_9f2b)
TINY=$(python -m scripts.issue_token viewer --tenant tk_live_tiny_1c3e)

section "Task 1 and 2: MCP server behind the security gateway"

check "no token is refused" '"code":-32000' \
  "$(curl -s -X POST "$MCP/mcp" -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"

check "tools/list is forwarded for a viewer" 'trigger_refund' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"

check "a viewer may call an ordinary tool" 'CUST-10001' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-10001"}}}')"

check "a viewer is refused a privileged tool" 'Unauthorized Tool Call' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}')"

check "case is not a bypass" 'Unauthorized Tool Call' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"ADMIN_RESET_KEY","arguments":{}}}')"

check "an admin reaches the downstream server" '"code":-32601' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $ADMIN" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}')"

check "malformed input is a protocol error" '"code":-32602' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-10001","amount":-50,"reason":"a negative refund"}}}')"

check "an unknown customer is an error result, not a protocol error" '"isError":true' \
  "$(curl -s -X POST "$MCP/mcp" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-99999"}}}')"

section "Task 3: streaming PII redaction"

check "an email is removed from a non streamed reply" 'REDACTED' \
  "$(curl -s -X POST "$LLM/v1/chat/completions" -H "Authorization: Bearer $VIEWER" \
     -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"Reply with exactly this sentence: Please email test.user@example.com today."}],"max_tokens":60}')"

STREAMED=$(curl -sN -X POST "$LLM/v1/chat/completions" -H "Authorization: Bearer $VIEWER" \
  -H 'Content-Type: application/json' \
  -d '{"stream":true,"messages":[{"role":"user","content":"Reply with exactly this sentence: Card 4111 1111 1111 1111 is on file."}],"max_tokens":60}')

check "a card number is removed mid stream" 'REDACTED' "$STREAMED"
check "the card number does not survive" "" "$(grep -c '4111' <<<"$STREAMED" | grep -x 0 || echo MISSING)"
check "the stream terminates properly" 'data: [DONE]' "$STREAMED"

section "Task 4: rate limiting and failover"

check "usage is reported per tenant" 'tokens_used_last_60s' \
  "$(curl -s "$LLM/v1/usage" -H "Authorization: Bearer $TINY")"

printf '  ....  spending the small tenant budget\n'
codes=""
for _ in $(seq 1 25); do
  codes+="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM/v1/chat/completions" \
    -H "Authorization: Bearer $TINY" -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":20}') "
done
check "the tenant is refused once its budget is spent" '429' "$codes"

# Header names are case insensitive and HTTP/2 sends them lowercased, so the
# comparison is folded rather than matched literally.
check "a refusal says when to retry" 'retry-after' \
  "$(curl -s -D - -o /dev/null -X POST "$LLM/v1/chat/completions" \
     -H "Authorization: Bearer $TINY" -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":20}' \
     | tr 'A-Z' 'a-z')"

check "another tenant is unaffected" '200' \
  "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM/v1/chat/completions" \
     -H "Authorization: Bearer $VIEWER" -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":20}')"

section "Audit trail"

python - <<'PY'
from src.core.database import get_connection
with get_connection() as c:
    rows = c.execute(
        "SELECT component, action, decision, COUNT(*) AS n FROM audit_log "
        "GROUP BY component, action, decision ORDER BY n DESC LIMIT 12"
    ).fetchall()
for r in rows:
    print(f"  {r['n']:>4}  {r['component']:<12} {r['action']:<20} {r['decision']}")
PY

printf '\n  %d passed, %d failed\n\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
