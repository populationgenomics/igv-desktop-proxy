from http import HTTPStatus

import httpx
from fastapi import HTTPException

from server.services.rate_limit_store import RateLimitRedisClient
from server.services.rate_limiter import DownloadRateLimiter
from server.utils.constants import (
    AUTH_HEADER_PARTS,
    REQUEST_URL_PARTS,
)
from server.utils.helpers import get_byte_range


def validate_and_parse_path(full_path: str) -> tuple[str, str]:
    """Parse bucket/path and return bucket, path tuple."""
    path_segments = full_path.split('/', 1)

    if len(path_segments) < REQUEST_URL_PARTS:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST.value,
            detail='Invalid path format. Use /bucket/path',
        )

    return path_segments[0], path_segments[1]


def validate_auth(headers: dict) -> str:
    """Validate auth header in the request."""
    auth_header = headers.get('Authorization')

    if auth_header is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    user_token = auth_header.split(' ')

    if len(user_token) != AUTH_HEADER_PARTS:
        raise HTTPException(HTTPStatus.UNAUTHORIZED)

    return user_token[1].strip()


async def rate_limit_if_applicable(
    object_path: str,
    range_header: str | None,
    user_token: str,
    httpx_client: httpx.AsyncClient,
    redis_client: RateLimitRedisClient,
) -> DownloadRateLimiter | None:
    """Check whether this request should be rate-limited.

    For CRAM files:
        the initial requests usually fetch content from the CRAM index file.
        Requests to fetch index file data some time include range header ('range': 'bytes=0-999999'), some not.
        Requests for a specific region in the CRAM file (non-index) typically includes a request
        for the actual byte range and (occasionally) another for the first 512 KB.

    For BAM files:
        Similar to CRAM files. The initial requests usually fetch content from the BAM index file.
        Requests to fetch index file data some time include range header ('range': 'bytes=0-999999'), some not.
        Requests for a specific region in the BAM file (non-index) typically includes a request
        for the actual byte range and (occasionally) another for the first 512 KB.

    For BigWig files:
        https://genome.ucsc.edu/goldenpath/help/bigWig.html
        Already indexed file.
        Requests for a specific region in the BigWig file includes a range request
        (varied ranges-does not stick to 512 KB).
    """
    if range_header is None:  # range header specified for non-index files
        is_index_file = object_path.endswith(('.crai', '.bai', '.csi', '.tbi'))

        if is_index_file:
            return None

        # Reject the request if range is not specified as we can not rate-limit them otherwise
        raise HTTPException(HTTPStatus.BAD_REQUEST)

    request_bytes = get_byte_range(range_header)

    rate_limiter = DownloadRateLimiter(
        redis_client=redis_client,
        http_client=httpx_client,
        user_token=user_token,
        request_bytes=request_bytes,
    )

    is_allowed = await rate_limiter.check_user_limit()
    if not is_allowed:
        raise HTTPException(HTTPStatus.TOO_MANY_REQUESTS)

    return rate_limiter
