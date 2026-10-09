# IGV desktop Proxy Testing

This directory contains the test suite for the IGV desktop proxy server.

## Execution

Run the tests using the `uv` environment:

```bash
uv run pytest tests/
```

## Manual end-to-end test

The unit tests mock Google. This checks the real wiring: userinfo, the `igv-proxy-config` secret,
the proxy service account's bucket IAM and GCS itself. It runs the proxy locally as the dev proxy
service account, so you need `gcloud`, `docker` and `jq`, and permission to grant IAM on that
service account and the buckets under test.

### 1. Set up (once)

```bash
export DEV_PROJECT=<dev proxy project>          # the PULUMI_CONFIG_GCP_PROJECT secret
export BUCKETS="cpg-fewgenomes-test app-test-data-bucket"   # optional; this is the default
export BUCKET=cpg-fewgenomes-test               # the one of BUCKETS that smoke.sh checks
export CRAM=cram/<sample>.cram                   # path inside BUCKET, no bucket prefix; .crai beside it
export UNREADABLE_BUCKET=<bucket you can read but the proxy SA cannot>   # optional

bash tests/manual/setup.sh
```

This lets you impersonate the dev proxy service account, grants it read on each of `BUCKETS`, adds
you to the dev access map for them, and points your application default credentials at the service
account. It prints a command to restore the access map afterwards. Keep it.

### 2. Start the proxy

```bash
docker run --name igv-dsk-proxy-test -d -p 6379:6379 redis:7.2
IGV_PROXY_CONFIG_PROJECT=$DEV_PROJECT uv run uvicorn --port 8080 --host localhost server.main:app
```

### 3. Run the checks

In a second terminal, with the same variables exported:

```bash
bash tests/manual/smoke.sh
```

Every line should read `PASS`. To check another bucket, rerun with `BUCKET` and `CRAM` pointing
at it, e.g. `BUCKET=app-test-data-bucket CRAM=<path> bash tests/manual/smoke.sh`. Then load the
CRAM through IGV Desktop (see the main [README](../README.md#testing-the-igv-desktop-python-proxy-locally)) and check reads appear.

### 4. Clean up

```bash
<restore command printed by setup.sh>
for bucket in ${BUCKETS:-cpg-fewgenomes-test app-test-data-bucket}; do
    gcloud storage buckets remove-iam-policy-binding gs://$bucket \
        --member=serviceAccount:igv-desktop-proxy-dev@$DEV_PROJECT.iam.gserviceaccount.com --role=roles/storage.objectViewer
done
gcloud auth application-default login           # back to your own credentials
docker rm -f igv-dsk-proxy-test
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `gcloud` hangs | DNS. `dig +short oauth2.googleapis.com` should answer instantly. |
| `iam.serviceAccounts.getAccessToken denied` | You lack Token Creator on the service account. Ask a dev project admin. |
| `403` | You or the bucket are not in the access map the proxy loaded. The proxy logs `Refused <email> access to <bucket>`. It reloads the map every 5 minutes; restart it to reload now. |
| `502` | The proxy service account cannot read the bucket, or `CRAM` includes the bucket name. |
