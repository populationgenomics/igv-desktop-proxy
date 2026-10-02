# Application constants

GCS_BASE_URL = 'storage-download.googleapis.com'

REQUEST_URL_PARTS = 2
AUTH_HEADER_PARTS = 2
HTTPX_CLIENT_TIMEOUT = 5  # httpx default timeout 5 secs

# rate limiting caps
DOWNLOAD_CAP_BYTES = 1073741824  # bytes (1GB download cap enforced)
CAPPED_TIME_WINDOW_SECS = 3600  # 1-hour

# rate limit retry
LOCK_POLL_ATTEMPTS = 10
LOCK_POLL_SLEEP_S = 0.1

SUB_PREFIX = 'sub'
LOCK_PREFIX = 'lock'
# Was 'token_hash' (bare `sub`) before SET-1250. Now JSON {sub, email}; the new prefix keeps the
# two formats apart during a rolling deploy.
TOKEN_HASH_PREFIX = 'token_hash_v2'  # noqa: S105

# identity verification
TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
TOKEN_CACHE_MAX_TTL_SECS = 3600  # never cache an identity longer than the access token's own lifetime

# required deployment configuration (Cloud Run env vars)
IGV_PROXY_CONFIG_PROJECT_ENV = 'IGV_PROXY_CONFIG_PROJECT'

# access map (igv-proxy-config secret)
IGV_PROXY_CONFIG_SECRET_ID = 'igv-proxy-config'  # noqa: S105  # fixed by SET-1249, not a deployment choice
ACCESS_LIST_TTL_SECS = 300  # revocation latency; a redeploy is the emergency fast path
ACCESS_LIST_LOAD_ATTEMPTS = 3  # bounded retry for the eager load at startup

# proxy service account credentials
GCS_READ_SCOPE = 'https://www.googleapis.com/auth/devstorage.read_only'
PROXY_TOKEN_REFRESH_MARGIN_SECS = 300

# index files are served whole, so they carry no Range header and cannot be metered
INDEX_FILE_SUFFIXES = ('.crai', '.bai', '.csi', '.tbi')

STATS_PREFIX = 'dl_stats'
STATS_KEY_TTL_SECS = 176400  # 49 hours (safety net TTL)

REDIS_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': 6379,
}

FASTAPI_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': 8080,
}
