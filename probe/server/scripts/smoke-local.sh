#!/usr/bin/env bash
# Local smoke check with `wrangler dev --local` + curl (complements test/integration).
# Checks: AS metadata, protected-resource metadata, 401 + WWW-Authenticate on POST /mcp,
# 401 on /probe/report without token, 200 with token.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8787}"
BASE="http://localhost:${PORT}"
CREATED_DEV_VARS=0
if [ ! -f .dev.vars ]; then
  cat > .dev.vars <<'VARS'
GITHUB_CLIENT_ID=dummy
GITHUB_CLIENT_SECRET=dummy
IP_HMAC_KEY=local-dev-ip-hmac-key-0123456789abcdef
REPORT_TOKEN=local-report-token
PROBE_ALLOWED_GITHUB_IDS=
VARS
  CREATED_DEV_VARS=1
fi
REPORT_TOKEN="$(grep '^REPORT_TOKEN=' .dev.vars | cut -d= -f2-)"

npx wrangler d1 migrations apply gaohe-probe --local >/dev/null
LOG="$(mktemp)"
npx wrangler dev --local --port "$PORT" >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  [ "$CREATED_DEV_VARS" = 1 ] && rm -f .dev.vars
  rm -f "$LOG"
}
trap cleanup EXIT
# Never send localhost traffic through an HTTP(S) proxy from the environment.
c() { curl --noproxy '*' "$@" || true; }

for _ in $(seq 1 90); do
  curl --noproxy '*' -s -o /dev/null "$BASE/" && break
  sleep 1
done

if ! curl --noproxy '*' -s -o /dev/null "$BASE/"; then echo "FAIL wrangler dev did not start:"; cat "$LOG"; exit 1; fi
fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS $1 ($3)"; else echo "FAIL $1: expected $2, got $3"; fail=1; fi
}

code=$(c -s -o /tmp/as.json -w '%{http_code}' "$BASE/.well-known/oauth-authorization-server")
check "GET /.well-known/oauth-authorization-server" 200 "$code"
grep -q '"authorization_endpoint":"'"$BASE"'/authorize"' /tmp/as.json && echo "PASS AS metadata authorization_endpoint" || { echo "FAIL AS metadata body: $(cat /tmp/as.json)"; fail=1; }

code=$(c -s -o /tmp/prm.json -w '%{http_code}' "$BASE/.well-known/oauth-protected-resource/mcp")
check "GET /.well-known/oauth-protected-resource/mcp" 200 "$code"
grep -q '"resource":"'"$BASE"'/mcp"' /tmp/prm.json && echo "PASS PRM resource = $BASE/mcp" || { echo "FAIL PRM body: $(cat /tmp/prm.json)"; fail=1; }

code=$(c -s -o /dev/null -w '%{http_code}' "$BASE/.well-known/oauth-protected-resource")
echo "INFO GET /.well-known/oauth-protected-resource (root) → $code (1.2.4 serves PRM only at the path-inserted URL for a path-bearing resource)"

hdr=$(c -s -D - -o /dev/null -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
code=$(printf '%s' "$hdr" | head -1 | awk '{print $2}')
check "POST /mcp without token" 401 "$code"
www=$(printf '%s' "$hdr" | grep -i '^www-authenticate:' || true)
echo "$www" | grep -q 'resource_metadata=' && echo "PASS WWW-Authenticate: ${www#*: }" || { echo "FAIL missing WWW-Authenticate resource_metadata"; fail=1; }

code=$(c -s -o /dev/null -w '%{http_code}' "$BASE/probe/report")
check "GET /probe/report without token" 401 "$code"
code=$(c -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $REPORT_TOKEN" "$BASE/probe/report")
check "GET /probe/report with token" 200 "$code"

rm -f /tmp/as.json /tmp/prm.json
exit "$fail"
