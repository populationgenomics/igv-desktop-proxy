from http import HTTPStatus

import httpx
from fastapi import HTTPException

from server.services.rate_limiter import DownloadRateLimiter
from server.services.rate_limit_store import RateLimitRedisClient
from server.utils.constants import AUTH_HEADER_PARTS, CRAM_INDEX_FILE_EXTENSION, FIRST_BYTE_RANGE, REQUEST_URL_PARTS
from server.utils.generic_helper import get_byte_range


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
        Subsequent requests for a specific region typically include one request for the actual byte range and another
        for the first 512 KB. Rate limiting is therefore applied only when the request targets CRAM data
        for a specific region beyond the first 512 KB.
    """
    is_index_file = object_path.endswith(CRAM_INDEX_FILE_EXTENSION)

    if is_index_file:
        return None

    if range_header is None:  # range header specified for non-index files
        raise HTTPException(HTTPStatus.BAD_REQUEST)

    if range_header == FIRST_BYTE_RANGE:
        return None

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
