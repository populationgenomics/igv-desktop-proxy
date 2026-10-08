# Application constants
import re
from http import HTTPStatus

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

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
# v2 caches JSON {sub, email}; a new prefix keeps it apart from the old bare-`sub` entries
TOKEN_HASH_PREFIX = 'token_hash_v2'  # noqa: S105

# identity verification
USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'
TOKEN_CACHE_TTL_SECS = 3600  # an access token's maximum lifetime

# required deployment configuration (Cloud Run env vars)
IGV_PROXY_CONFIG_PROJECT_ENV = 'IGV_PROXY_CONFIG_PROJECT'

# access map (igv-proxy-config secret)
IGV_PROXY_CONFIG_SECRET_ID = 'igv-proxy-config'  # noqa: S105  # fixed by SET-1249, not a deployment choice
ACCESS_LIST_TTL_SECS = 300  # revocation latency; a redeploy is the emergency fast path
ACCESS_LIST_LOAD_ATTEMPTS = 3  # bounded retry for the eager load at startup
ACCESS_LIST_LOAD_BACKOFF_INITIAL_SECS = 1
ACCESS_LIST_LOAD_BACKOFF_MAX_SECS = 8
ACCESS_LIST_RETRY_BACKOFF_SECS = 30  # after a failed refresh, so requests don't each retry Secret Manager
# Refresh failures survivable while a previous map is held (GoogleAuthError isn't a GoogleAPIError)
REFRESH_ERRORS = (GoogleAPIError, GoogleAuthError, ValueError, OSError)

# Generic on purpose: callers are external collaborators, who can't see how access is granted
FORBIDDEN_DETAIL = 'Access to this data is not authorised. Please reach out to your CPG data contact to request access.'

# proxy service account credentials
GCS_READ_SCOPE = 'https://www.googleapis.com/auth/devstorage.read_only'
PROXY_TOKEN_REFRESH_MARGIN_SECS = 300

# Upstream statuses passed through to the user; anything else means the proxy's own access broke, so 502
PASS_THROUGH_STATUSES: dict[int, str] = {
    HTTPStatus.NOT_FOUND: 'Object not found.',
    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE: 'Requested range not satisfiable.',
}
UPSTREAM_FAILED_DETAIL = 'Upstream storage request failed.'

# A single closed range, the only form whose byte count is known before the response arrives.
CLOSED_BYTE_RANGE = re.compile(r'bytes=([0-9]+)-([0-9]+)', re.IGNORECASE)

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
