# Application constants
GCS_BASE_URL = 'storage-download.googleapis.com'
REQUEST_URL_PARTS = 2
CLIENT_TIMEOUT = 5  # httpx default timeout 5 secs

HOSTED_DOMAIN = 'populationgenomics.org.au'

# rate limiting caps
DOWNLOAD_CAP_FOR_TIME_WINDOW = 1073741824  # bytes (1GB download cap enforced)

CRAM_INDEX_FILE_EXTENSION = '.crai'

REDIS_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': '6379',
}

FASTAPI_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': 8080,
}
