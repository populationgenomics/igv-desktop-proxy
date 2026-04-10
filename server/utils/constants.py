# Application constants

GCS_BASE_URL = 'storage-download.googleapis.com'
CPG_HOSTED_DOMAIN = 'populationgenomics.org.au'

REQUEST_URL_PARTS = 2
AUTH_HEADER_PARTS = 2
HTTPX_CLIENT_TIMEOUT = 5  # httpx default timeout 5 secs

# rate limiting caps
DOWNLOAD_CAP_BYTES = 1073741824  # bytes (1GB download cap enforced)
CAPPED_TIME_WINDOW_SECS = 3600  # 1-hour
FIRST_BYTE_RANGE = 'bytes=0-511999'

# rate limit retry
LOCK_POLL_ATTEMPTS = 10
LOCK_POLL_SLEEP_S = 0.1

CRAM_INDEX_FILE_EXTENSION = '.crai'
BAM_INDEX_FILE_EXTENSION = '.bai'
SUB_PREFIX = 'sub'
LOCK_PREFIX = 'lock'
TOKEN_HASH_PREFIX = 'token_hash'  # noqa: S105

STATS_PREFIX = 'dl_stats'
STATS_KEY_TTL_SECS = 176400  # 49 hours (safety net TTL)

REDIS_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': '6379',
}

FASTAPI_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': 8080,
}
