#!/usr/bin/env bash
# One-time setup for the manual end-to-end test: lets a local proxy run as the dev proxy service
# account and lists you in the dev access map. See tests/README.md.
set -euo pipefail

: "${DEV_PROJECT:?set DEV_PROJECT to the dev proxy project}"
: "${BUCKET:?set BUCKET to the bucket under test, e.g. cpg-fewgenomes-test}"
UNREADABLE_BUCKET="${UNREADABLE_BUCKET:-}"
EMAIL="${EMAIL:-$(gcloud config get-value account 2>/dev/null)}"
SA="igv-desktop-proxy-dev@${DEV_PROJECT}.iam.gserviceaccount.com"
SECRET=(--secret=igv-proxy-config --project="$DEV_PROJECT")

echo "==> Checking the proxy service account exists: $SA"
gcloud iam service-accounts describe "$SA" --project="$DEV_PROJECT" --format='value(email)'

echo "==> Allowing $EMAIL to impersonate it"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$DEV_PROJECT" \
    --member="user:$EMAIL" --role=roles/iam.serviceAccountTokenCreator >/dev/null

echo "==> Granting it read on gs://$BUCKET"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member="serviceAccount:$SA" --role=roles/storage.objectViewer >/dev/null

# The dev proxy reads this secret too, so merge into the current map rather than replace it.
echo "==> Adding $EMAIL -> [$BUCKET${UNREADABLE_BUCKET:+, $UNREADABLE_BUCKET}] to the dev access map"
previous=$(gcloud secrets versions describe latest "${SECRET[@]}" --format='value(name.basename())')
gcloud secrets versions access latest "${SECRET[@]}" \
    | jq --arg email "$EMAIL" --arg bucket "$BUCKET" --arg unreadable "$UNREADABLE_BUCKET" \
        '.users[$email] = ((.users[$email] // []) + ([$bucket, $unreadable] | map(select(. != ""))) | unique)' \
    | gcloud secrets versions add igv-proxy-config --project="$DEV_PROJECT" --data-file=- >/dev/null

echo "==> Waiting for impersonation to work (new IAM bindings can take a minute)"
for _ in $(seq 12); do
    gcloud auth print-access-token --impersonate-service-account="$SA" >/dev/null 2>&1 && break
    sleep 10
done
gcloud auth print-access-token --impersonate-service-account="$SA" >/dev/null

echo "==> Pointing application default credentials at the service account (opens a browser)"
gcloud auth application-default login --impersonate-service-account="$SA"

cat <<EOF

Setup done. When finished testing, restore the access map with:
  gcloud secrets versions access $previous --secret=igv-proxy-config --project=$DEV_PROJECT | gcloud secrets versions add igv-proxy-config --project=$DEV_PROJECT --data-file=-
EOF
