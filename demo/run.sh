#!/usr/bin/env bash
# End-to-end APIReplay demo.
#
#   1. Start demo service v1, record traffic through the capture proxy.
#   2. Sanitize the pack (redacts the demo Authorization header).
#   3. Replay the sanitized pack against demo service v2.
#   4. Diff + report. v2 seeds 3 regressions; the report must show them.
#
# Usage: bash demo/run.sh   (exit 0 on success; prints the report path)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

V1_PORT=18080
V2_PORT=18081
PROXY_PORT=18888
export PYTHONPATH="$ROOT/src"

RAW_PACK="$(mktemp -d)/pack.raw.json"
PACK="demo/pack.json"
RESULTS="demo/results.json"
DIFF="demo/diff.json"
REPORT="demo/report.md"

SRV1_PID=""; PROXY_PID=""; SRV2_PID=""
cleanup() {
    for pid in $SRV1_PID $PROXY_PID $SRV2_PID; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

wait_for_http() {  # wait_for_http <port> <path>
    local port="$1" path="$2" i
    for i in $(seq 1 50); do
        if python3 -c "
import http.client, sys
c = http.client.HTTPConnection('127.0.0.1', $port, timeout=1)
try:
    c.request('GET', '$path'); sys.exit(0 if c.getresponse().status < 500 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            return 0
        fi
        sleep 0.2
    done
    echo "error: service on port $port did not come up" >&2
    return 1
}

echo "== 1. starting demo service v1 on :$V1_PORT"
python3 demo/server.py --version v1 --port "$V1_PORT" >/tmp/apireplay-v1.log 2>&1 &
SRV1_PID=$!
wait_for_http "$V1_PORT" /api/health

echo "== 2. starting capture proxy on :$PROXY_PORT -> v1"
python3 -m apireplay record --proxy --port "$PROXY_PORT" \
    --target "http://127.0.0.1:$V1_PORT" --out "$RAW_PACK" >/tmp/apireplay-proxy.log 2>&1 &
PROXY_PID=$!
sleep 1
if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "error: proxy failed to start; log:" >&2; cat /tmp/apireplay-proxy.log >&2
    exit 1
fi

echo "== 3. generating traffic through the proxy"
PROXY_PORT="$PROXY_PORT" TARGET_PORT="$V1_PORT" python3 demo/make_traffic.py

echo "== 4. stopping proxy (writes pack)"
kill -INT "$PROXY_PID"
wait "$PROXY_PID"
PROXY_PID=""
CAPTURED="$(python3 -c "import json; print(len(json.load(open('$RAW_PACK'))['requests']))")"
echo "captured $CAPTURED request(s)"
if [ "$CAPTURED" -lt 7 ]; then
    echo "error: expected >= 7 captured requests, got $CAPTURED" >&2
    exit 1
fi

echo "== 5. sanitizing pack"
python3 -m apireplay sanitize "$RAW_PACK" --out "$PACK"

echo "== 6. starting demo service v2 on :$V2_PORT"
python3 demo/server.py --version v2 --port "$V2_PORT" >/tmp/apireplay-v2.log 2>&1 &
SRV2_PID=$!
wait_for_http "$V2_PORT" /api/health

echo "== 7. replaying sanitized pack against v2"
python3 -m apireplay replay "$PACK" --target "http://127.0.0.1:$V2_PORT" \
    --workers 4 --timeout 10 --out "$RESULTS"

echo "== 8. diffing recorded (v1) vs replayed (v2)"
python3 -m apireplay diff "$PACK" "$RESULTS" --out "$DIFF"

echo "== 9. writing report"
python3 -m apireplay report "$DIFF" --format md --out "$REPORT"

echo ""
echo "Demo complete — report: $REPORT"
python3 -c "
import json
d = json.load(open('$DIFF'))
s = d['summary']
print(f\"verdicts: {s['passed']} passed, {s['failed']} failed of {s['total']}\")
assert s['failed'] >= 3, 'expected the 3 seeded v2 regressions to be caught'
print('OK: all 3 seeded regressions were caught')
"
