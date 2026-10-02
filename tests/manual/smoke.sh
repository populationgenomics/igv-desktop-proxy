#!/usr/bin/env bash
# End-to-end checks against a locally running proxy and its Redis. See tests/README.md.
set -uo pipefail

: "${BUCKET:?set BUCKET to the bucket under test, e.g. cpg-fewgenomes-test}"
: "${CRAM:?set CRAM to an object path inside BUCKET, without the bucket, e.g. cram/<sample>.cram}"
PROXY_URL="${PROXY_URL:-http://localhost:8080}"
REDIS_CONTAINER="${REDIS_CONTAINER:-igv-dsk-proxy-test}"
UNLISTED_BUCKET="${UNLISTED_BUCKET:-cpg-not-in-the-access-map}"
UNREADABLE_BUCKET="${UNREADABLE_BUCKET:-}"  # in the access map, but the proxy SA has no IAM on it

CHUNK=524288                # bytes in one ranged request below
CAP=1073741824              # DOWNLOAD_CAP_BYTES; an absent budget is a full one
RANGE="Range: bytes=0-$((CHUNK - 1))"

TOKEN=$(gcloud auth print-access-token)
AUTH="Authorization: Bearer $TOKEN"
TOKEN_KEY="token_hash_v2:$(printf %s "$TOKEN" | shasum -a 256 | cut -d' ' -f1)"

failures=0

rc() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

check() {  # <description> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        printf '  PASS  %s\n' "$1"
    else
        printf '  FAIL  %s: expected %s, got %s\n' "$1" "$2" "$3"
        failures=$((failures + 1))
    fi
}

budget() { local v; v=$(rc HGET "sub:$SUB" remaining_bytes); echo "${v:-$CAP}"; }
stats() { local v; v=$(rc GET "dl_stats:$SUB:$BUCKET:$(date -u +%F)"); echo "${v:-0}"; }

echo 'Authentication'
check 'health check' 200 "$(status "$PROXY_URL/health")"
check 'no Authorization header -> 401' 401 "$(status -I "$PROXY_URL/$BUCKET/$CRAM")"
check 'invalid token -> 401' 401 "$(status -I -H 'Authorization: Bearer not-a-real-token' "$PROXY_URL/$BUCKET/$CRAM")"

echo 'Main path'
check 'HEAD -> 200' 200 "$(status -I -H "$AUTH" "$PROXY_URL/$BUCKET/$CRAM")"

SUB=$(rc GET "$TOKEN_KEY" | jq -r '.sub // empty' 2>/dev/null)
if [[ -z "$SUB" ]]; then
    echo "  FAIL  no identity cached under $TOKEN_KEY; the remaining checks need it"
    exit 1
fi
ttl=$(rc TTL "$TOKEN_KEY")
check 'identity cached with TTL <= 3600' yes "$( ((ttl > 0 && ttl <= 3600)) && echo yes || echo "no (TTL $ttl)")"

before=$(budget)
check 'index GET -> 200' 200 "$(status -H "$AUTH" "$PROXY_URL/$BUCKET/$CRAM.crai")"
check 'index GET is not metered' "$before" "$(budget)"

before=$(budget)
before_stats=$(stats)
check 'ranged GET -> 206' 206 "$(status -H "$AUTH" -H "$RANGE" "$PROXY_URL/$BUCKET/$CRAM")"
check 'ranged GET is metered' $((before - CHUNK)) "$(budget)"
check 'ranged GET is recorded in dl_stats' $((before_stats + CHUNK)) "$(stats)"

echo 'Upstream errors'
before=$(budget)
check 'missing object -> 404' 404 "$(status -H "$AUTH" -H "$RANGE" "$PROXY_URL/$BUCKET/$CRAM.does-not-exist")"
check 'range past the end -> 416' 416 \
    "$(status -H "$AUTH" -H 'Range: bytes=1099511627776-1099511627777' "$PROXY_URL/$BUCKET/$CRAM")"
check 'failed requests are refunded' "$before" "$(budget)"

echo 'Authorization'
before=$(budget)
check 'bucket not in the access map -> 403' 403 \
    "$(status -H "$AUTH" -H "$RANGE" "$PROXY_URL/$UNLISTED_BUCKET/$CRAM")"
check 'HEAD on a bucket not in the access map -> 403' 403 \
    "$(status -I -H "$AUTH" "$PROXY_URL/$UNLISTED_BUCKET/$CRAM")"
check 'refused requests are not metered' "$before" "$(budget)"

if [[ -n "$UNREADABLE_BUCKET" ]]; then
    # A 502 rather than a 200 shows the proxy reads GCS as its own SA, not with the caller's token.
    check 'bucket the proxy SA cannot read -> 502' 502 \
        "$(status -H "$AUTH" -H "$RANGE" "$PROXY_URL/$UNREADABLE_BUCKET/$CRAM")"
else
    echo '  SKIP  bucket the proxy SA cannot read (set UNREADABLE_BUCKET)'
fi

echo
if ((failures)); then
    echo "$failures check(s) failed. The proxy log says why."
    exit 1
fi
echo 'All checks passed.'
